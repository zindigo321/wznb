"""Run inherited scheduling, immutable evidence, and independent rechecks."""
from .common import *
from .io_contract import export_tables, load_tables, read_standard_tasks
from .scheduler import make_candidates, schedule, select_fault, replay_state, future_candidates
from .tle_adapter import read_and_audit, Geometry, build_windows
from .validator import IndependentValidator


def tree_hashes(directory):
    return {p.relative_to(directory).as_posix():digest(p) for p in sorted(Path(directory).rglob('*')) if p.is_file()}


def versions():
    return dict(python=platform.python_version(), platform=platform.platform(),
                packages={k:importlib.metadata.version(k) for k in ['numpy','skyfield','sgp4','PyYAML','jplephem']})


def cached_windows(config, records, stations, day, sample, out, clock):
    identity=dict(schema_version=1, input_hashes=sample['input_hashes'], selected=sample['selected'],
                  stations=stations, start_utc=utc_at(day,0), horizon_seconds=config['horizon_seconds'],
                  filters=['geometric_elevation_10_to_85_degrees','no_refraction'],
                  sampling={k:config[k] for k in ['coarse_step_seconds','fine_step_seconds','elevation_degrees']},
                  versions=versions(), adapter_sha256=digest(Path(__file__).with_name('tle_adapter.py')))
    key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    cache=Path(config['cache_dir']).resolve()/key
    if cache.exists():
        manifest=json.loads((cache/'manifest.json').read_text(encoding='utf-8'))
        if manifest['identity']!=identity or digest(cache/'windows.json')!=manifest['windows_sha256']:
            raise ValueError('cache identity/content hash mismatch')
        windows=json.loads((cache/'windows.json').read_text(encoding='utf-8'))
        errors=json.loads((cache/'propagation_errors.json').read_text(encoding='utf-8'))
        if digest(cache/'propagation_errors.json')!=manifest['errors_sha256']:
            raise ValueError('cache propagation error evidence hash mismatch')
        csv_write(out/'windows.csv',windows,WINDOW_FIELDS)
        json_write(out/'propagation_errors.json',errors)
        hit=True
    else:
        geometry=Geometry(records,stations,day,clock)
        windows=build_windows(geometry,records,stations,config,out,clock)
        errors=geometry.errors
        cache.mkdir(parents=True,exist_ok=False)
        json_write(cache/'windows.json',windows)
        json_write(cache/'propagation_errors.json',errors)
        json_write(cache/'manifest.json',dict(identity=identity,windows_sha256=digest(cache/'windows.json'),
                                            errors_sha256=digest(cache/'propagation_errors.json')))
        hit=False
    json_write(out/'cache.json',dict(key=key,hit=hit,identity=identity,path=str(cache)))
    return windows, errors


def save_plan(out, name, tasks, report, total, executable):
    csv_write(out/f'schedule_{name}.csv',tasks,TASK_FIELDS)  # legacy E0 byte comparison
    csv_write(out/f'tasks_{name}.csv',[dict(t,target_id=t['satnum']) for t in tasks], ['target_id']+TASK_FIELDS)
    json_write(out/'checks'/f'{name}.json',report)
    return {**{k:report[k] for k in ('status','task_score','points_int','coverage','by_priority_coverage','violation_count','violations','evaluation_scope')},
            'coverage_all':dict(numerator=report['coverage'],denominator=total,rate=report['coverage']/total if total else None),
            'coverage_executable':dict(numerator=report['coverage'],denominator=executable,rate=report['coverage']/executable if executable else None)}


def run(config, out, clock):
    run_started=time.perf_counter()
    from .experiment_inputs import prepare_frozen, check_candidates, load_fixed_event, fault_impact, changes_by_target
    reuse=None
    start=clock.begin('read_audit_and_freeze')
    if config.get('reuse_source'):
        records,stations,day,sample,metadata,windows,validator,errors,reuse=prepare_frozen(config,out,clock)
        clock.end('read_audit_and_freeze',start,mode='sealed_source_reuse_including_TLE_validator_setup')
        start=clock.begin('window_computation_or_cache')
        clock.end('window_computation_or_cache',start,mode='reused_sealed_windows_no_window_propagation')
    elif config['input_mode']=='tle':
        config.setdefault('frozen_sample',str(out/'frozen_sample.json'))
        records,stations,day,sample,audit=read_and_audit(config,out,clock)
        metadata=dict(data_version='official_fourth_problem_'+hashlib.sha256(json.dumps(sample['input_hashes'],sort_keys=True).encode()).hexdigest(),
                      filters_applied=['geometric_elevation_10_to_85_degrees','no_refraction'],
                      geometry_limitations='no continuous-time or window-search completeness proof; optical filters not verified')
        json_write(out/'effective_config.json',config)
        clock.end('read_audit_and_freeze',start)
        start=clock.begin('window_computation_or_cache')
        windows, errors=cached_windows(config,records,stations,day,sample,out,clock)
        clock.end('window_computation_or_cache',start,windows=len(windows))
        validator=IndependentValidator(config,day,sample,windows,clock)
    else:
        records,stations,windows,day,metadata=load_tables(config['input_dir'],config)
        hashes=tree_hashes(Path(config['input_dir']))
        json_write(out/'input_hashes.json',hashes)
        sample=dict(selected={p:[r['satnum'] for r in records if r['priority']==p] for p in ('high','medium','low')},
                    source='all supplied targets; no sampling',input_hashes=hashes)
        json_write(out/'frozen_sample.json',sample)
        errors=[]
        csv_write(out/'windows.csv',windows,WINDOW_FIELDS)
        validator=IndependentValidator.from_tables(config,day,records,stations,windows,clock)
        clock.end('read_audit_and_freeze',start)
        start=clock.begin('window_computation_or_cache')
        clock.end('window_computation_or_cache',start,mode='provided_windows_no_propagation')
        json_write(out/'cache.json',dict(hit=False,mode='provided_windows_no_propagation',cold_window_computation=False))
    export_tables(out/'standard_input',records,stations,windows,day,config['horizon_seconds'],metadata)
    json_write(out/'evaluation_context.json',dict(records=records,stations=stations,day=utc_at(day,0),sample=sample))
    fixed_event=None
    if config.get('fault_event_path'):
        fixed_event,source_event=load_fixed_event(config['fault_event_path'],config['station_ids'],day,config['horizon_seconds'])
        json_write(out/'event_provenance.json',dict(source_path=config['fault_event_path'],source_sha256=digest(config['fault_event_path']),
                   source_event_annotations=source_event,physical_event=fixed_event,
                   note='source task IDs describe source plan only; actual impacts are recomputed per method'))
    start=clock.begin('candidate_generation')
    candidates=make_candidates(windows,records,config,day,out,clock)
    if reuse is not None: check_candidates(candidates,config,out,reuse)
    executable=len({c['satnum'] for c in candidates})
    clock.end('candidate_generation',start,candidates=len(candidates))
    methods=['baseline','improved'] if config['method']=='both' else [config['method']]
    plans, reports, metrics = {}, {}, {}
    for method in methods:
        start=clock.begin('scheduling_'+method)
        plans[method]=schedule(candidates,method,clock=clock)
        clock.end('scheduling_'+method,start)
        json_write(out/f'initial_frozen_{method}.json',plans[method])
    start=clock.begin('validation_initial')
    for method,tasks in plans.items():
        reports[method]=validator.check(tasks,label=method)
        metrics[method]=save_plan(out,method,tasks,reports[method],len(records),executable)
    clock.end('validation_initial',start)
    event=fixed_event or select_fault(plans.get('improved',plans[methods[0]]),day,config['horizon_seconds'],config['fault_seconds'])
    json_write(out/'fault_event.json',event)
    impacts={method:fault_impact(initial,event) for method,initial in plans.items()} if event.get('effective') else {}
    json_write(out/'fault_impacts.json',impacts)
    replans, histories = {}, {}
    if event.get('effective'):
        for method,initial in plans.items():
            initial=json.loads((out/f'initial_frozen_{method}.json').read_text(encoding='utf-8'))
            start=clock.begin('replanning_'+method)
            completed,locked,interrupted,cancelled=replay_state(initial,event,day)
            fixed=completed+locked
            new=schedule(future_candidates(candidates,fixed,event),method,fixed=fixed,interrupted=interrupted,clock=clock)
            histories[method]=dict(completed=completed,locked=locked,interrupted=interrupted,cancelled_future=cancelled)
            replans[method]=new
            json_write(out/f'history_{method}.json',histories[method])
            json_write(out/f'changes_{method}.json',changes_by_target(initial,new))
            csv_write(out/f'interrupted_{method}.csv',[dict(r,target_id=r['satnum']) for r in interrupted],
                      ['target_id']+TASK_FIELDS+['planned_end_s','planned_duration_s','planned_end_utc'])
            clock.end('replanning_'+method,start)
        start=clock.begin('validation_replanned')
        for method,new in replans.items():
            name='replanned' if method=='improved' else 'baseline_replanned'
            immutable_initial=json.loads((out/f'initial_frozen_{method}.json').read_text(encoding='utf-8'))
            report=validator.check(new,histories[method]['interrupted'],event,label=name,original_plan=immutable_initial)
            metrics[name]=save_plan(out,name,new,report,len(records),executable)
        clock.end('validation_replanned',start)
    tests=None
    if config['input_mode']=='tle' and len(plans[methods[-1]])>=2:
        from .legacy_checks import counterexample_tests
        start=clock.begin('legacy_negative_checks')
        tests=counterexample_tests(validator,plans[methods[-1]],day,event,out,clock)
        clock.end('legacy_negative_checks',start)
    pruning=json.loads((out/'candidate_pruning.json').read_text(encoding='utf-8'))
    best={}
    values={r['satnum']:{'high':5,'medium':3,'low':2}[r['priority']] for r in records}
    for c in candidates:
        point=values[c['satnum']]*(3 if c['duration_s']==120 else 5)
        best[c['satnum']]=max(best.get(c['satnum'],0),point)
    upper_bound=sum(best.values())
    source_unchanged=True
    if reuse is not None:
        after=tree_hashes(Path(config['reuse_source']))
        raw_after={p.name:dict(sha256=digest(p),bytes=p.stat().st_size) for p in Path(config['input_dir']).iterdir() if p.is_file()}
        source_unchanged=after==reuse['source_hashes_before'] and raw_after==reuse['original_input_hashes']
        reuse.update(source_hashes_after=after,source_unchanged=source_unchanged,raw_inputs_unchanged=raw_after==reuse['original_input_hashes'])
        json_write(out/'input_reuse.json',reuse)
    passed=all(m['status']=='PASS' for m in metrics.values()) and not errors and (tests is None or tests['all_required_triggered'])
    passed=passed and source_unchanged
    result=dict(status='PASS' if passed else 'FAIL',actual_execution=True,seed=config['seed'],target_count=len(records),station_count=len(stations),
                horizon_seconds=config['horizon_seconds'],window_count=len(windows),candidate_count=len(candidates),
                raw_candidate_count=pruning['raw_grid_candidates'],executable_target_count=executable,
                data_version=metadata['data_version'],plans=metrics,fault=event,fault_impacts=impacts,propagation_error_count=len(errors),
                candidate_upper_bound=dict(points_int=upper_bound,task_score=upper_bound/5,definition='sum of best points per target in initial candidates; ignores resource conflicts'),
                fault_demo_status='PASS' if event.get('effective') and any(v['counts']['affected_future'] for v in impacts.values()) and passed else 'BLOCKED',
                optional_evaluator=dict(status='BLOCKED',reason='legacy rule SHA256 and earliest_midpoint_prioritize_future contract mismatch; isolated'),
                runtime=dict(seconds=time.perf_counter()-run_started,stages=clock.stages),
                limitations=['smoke assumptions, not official scoring','optical conditions not independently verified'])
    json_write(out/'metrics.json',result)
    return 0 if passed else 2


def recheck(source, out, clock, source_sha256=None):
    # Native schema dispatch precedes legacy config/fault-specific field reads.
    if (Path(source)/'run.json').exists():
        from .no_fault import recheck as native_recheck
        return native_recheck(source, source_sha256, out)

    before=tree_hashes(source)
    manifest=json.loads((source/'evidence_manifest.json').read_text(encoding='utf-8'))
    altered=[name for name,h in manifest.items() if before.get(name)!=h]
    altered+=sorted(set(before)-set(manifest)-{'evidence_manifest.json'})
    if altered: raise ValueError('source evidence differs from sealed manifest: '+','.join(altered))
    config=json.loads((source/'effective_config.json').read_text(encoding='utf-8'))
    context=json.loads((source/'evaluation_context.json').read_text(encoding='utf-8'))
    from .io_contract import parse_utc
    day=parse_utc(context['day'])
    _,_,windows,_,_=load_tables(source/'standard_input',dict(config,start_utc=context['day']))
    if config['input_mode']=='tle':
        actual={p.name:dict(sha256=digest(p),bytes=p.stat().st_size) for p in Path(config['input_dir']).iterdir() if p.is_file()}
        if actual!=context['sample']['input_hashes']: raise ValueError('original raw input hashes changed')
        validator=IndependentValidator(config,day,context['sample'],windows,clock)
    else:
        validator=IndependentValidator.from_tables(config,day,context['records'],context['stations'],windows,clock)
    event=json.loads((source/'fault_event.json').read_text(encoding='utf-8'))
    reports={}
    start=clock.begin('independent_recheck')
    for name in json.loads((source/'metrics.json').read_text(encoding='utf-8'))['plans']:
        method='baseline' if name.startswith('baseline') else 'improved'
        dynamic='replanned' in name
        history=json.loads((source/f'history_{method}.json').read_text(encoding='utf-8')) if dynamic else None
        reports[name]=validator.check(csv_tasks(source/f'schedule_{name}.csv'),
                     interrupted=history['interrupted'] if dynamic else (),fault=event if dynamic else None,
                     original_plan=csv_tasks(source/f'schedule_{method}.csv') if dynamic else None,label=name)
        json_write(out/'checks'/f'{name}.json',reports[name])
    clock.end('independent_recheck',start)
    after=tree_hashes(source)
    unchanged=before==after
    passed=unchanged and all(r['status']=='PASS' for r in reports.values())
    json_write(out/'source_hashes_before.json',before)
    json_write(out/'source_hashes_after.json',after)
    json_write(out/'recheck.json',dict(status='PASS' if passed else 'FAIL',source=str(source),source_unchanged=unchanged,
               source_file_count=len(before),reports=reports))
    return 0 if passed else 2


def evaluate(config,out,clock,tasks_path,initial_path=None,event_path=None,interrupted_path=None):
    records,stations,windows,day,meta=load_tables(config['input_dir'],config)
    validator=IndependentValidator.from_tables(config,day,records,stations,windows,clock)
    tasks=read_standard_tasks(tasks_path,records,windows,day)
    original=read_standard_tasks(initial_path,records,windows,day) if initial_path else None
    event=json.loads(Path(event_path).read_text(encoding='utf-8')) if event_path else None
    fragments=read_standard_tasks(interrupted_path,records,windows,day) if interrupted_path else ()
    if original is not None:
        report=validator.check(original,label='original_plan')
        json_write(out/'original_plan_check.json',report)
        if report['status']!='PASS': raise ValueError('original plan is invalid')
    report=validator.check(tasks,fragments,event,original_plan=original)
    json_write(out/'evaluation.json',report)
    return 0 if report['status']=='PASS' else 2


def run_no_fault(source, expected_sha256, decision_s, method, out, remaining_seconds):
    from .no_fault import produce
    return produce(source, expected_sha256, decision_s, method, out, remaining_seconds)

def evaluate_no_fault(source, expected_sha256, decision_s, tasks, out):
    from .no_fault import evaluate
    return evaluate(source, expected_sha256, decision_s, tasks, out)

def recheck_no_fault(source, expected_sha256, out):
    from .no_fault import recheck as native_recheck
    return native_recheck(source, expected_sha256, out)
