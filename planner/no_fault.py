"""Native no-fault production/evaluation/recheck. Input authority is always reopened from files."""
import copy,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
from collections import Counter
from .atomic_io import atomic_write_text
from .common import Clock,BudgetExpired,TASK_FIELDS,csv_write,json_write
from .io_contract import read_complete_tasks
from .no_fault_source import load_source,strict_json,file_identity,reject_links,safe_relative,MEMBERS
from .no_fault_rules import decision,status_at,require
from .path_safety import guard_writes,tree_snapshot
from .validator import IndependentValidator

def now():
    return datetime.now(timezone.utc).isoformat()

def changes(before, after):
    def physical(r):
        return (r['station_id'],r['start_utc'],r['end_utc'],r['duration_s'])
    a={r['satnum']:physical(r) for r in before}
    b={r['satnum']:physical(r) for r in after}
    rows=[dict(target_id=n,change='added' if n not in a else 'deleted' if n not in b else
               'retained' if a[n]==b[n] else 'moved_or_duration_changed') for n in sorted(set(a)|set(b))]
    return dict(counts=dict(Counter(r['change'] for r in rows)),
                net_adjusted_target_count=sum(r['change']!='retained' for r in rows),rows=rows)

def checked_output(out, protected):
    out=reject_links(out)
    guard_writes([out],protected)
    if out.exists():
        raise ValueError('output must be a new directory')
    return out

def verify_tasks(source, t, rows, guard=True):
    return IndependentValidator.check_no_fault(source['context'],source['initial'],source['candidates'],t,rows,require_guard=guard)

def choose_from_files(source_dir, expected_sha256, decision_s, proposal):
    """No ticket/PASS/context parameter exists. Membership always uses the sealed original pool."""
    source=load_source(source_dir,expected_sha256)
    decision(decision_s,source['context']['horizon'])
    retained=[dict(r,status=status_at(r,decision_s)) for r in source['initial']]
    jr=verify_tasks(source,decision_s,retained)['J']
    jp=verify_tasks(source,decision_s,proposal,False)['J']
    if tuple(jp)>tuple(jr):
        branch='ACCEPT_CANDIDATE_STRICTLY_BETTER';origin='raw_proposal'
        selected=proposal
    elif tuple(jp)<tuple(jr):
        branch='KEEP_RETAINED_LOWER_CANDIDATE';origin='retained_original';selected=retained
    else:
        branch='KEEP_RETAINED_TIE';origin='retained_original';selected=retained
    return copy.deepcopy(selected),dict(branch=branch,selected_source=origin,J_retained=jr,J_proposal=jp,
        J_selected=jp if tuple(jp)>tuple(jr) else jr,physical_changes=changes(source['initial'],selected))

def write_tasks(path, rows, fields):
    values=[dict(r,**({'target_id':r['satnum']} if 'target_id' in fields else {})) for r in rows]
    csv_write(path,values,fields)

def artifact_files(root):
    result={}
    for p in sorted(Path(root).rglob('*')):
        reject_links(p)
        if p.is_file() and p.relative_to(root).as_posix()!='evidence_manifest.json':
            result[p.relative_to(root).as_posix()]=file_identity(p)
    return result

def seal_run(out):
    json_write(out/'evidence_manifest.json',dict(schema_version=1,kind='no_fault_result_manifest',files=artifact_files(out)))

def produce(source_dir, expected_sha256, decision_s, method, out, remaining_seconds):
    require(method in ('baseline','improved'),'one baseline or improved method required')
    source=load_source(source_dir,expected_sha256)
    decision(decision_s,source['context']['horizon'])
    out=checked_output(out,[source['root']])
    before=tree_snapshot(source['root'])
    retained_raw=[dict(r,status=status_at({'start_s':int(r['start_s']),'end_s':int(r['end_s'])},decision_s)) for r in source['initial_raw']]
    retained=[dict(r,status=status_at(r,decision_s)) for r in source['initial']]
    retained_check=verify_tasks(source,decision_s,retained)
    fixed=[r for r in retained if r['status']=='completed']+[r for r in retained if r['status']=='locked']
    ids={r['satnum'] for r in fixed}
    future=[dict(r,status='new_success') for r in source['candidates'] if r['start_s']>=decision_s and r['satnum'] not in ids]
    if remaining_seconds() < 32:
        raise BudgetExpired('full 30-second solver block plus finalization unavailable')
    out.mkdir(parents=True,exist_ok=False)
    solver_calls=0;clock=None;started=now();start=time.perf_counter()
    try:
        (out/'source/standard_input').mkdir(parents=True)
        from .atomic_io import atomic_text_writer
        for name in (*MEMBERS,'source_manifest.json'):
            data=(source['root']/name).read_bytes()
            # Preserve source bytes and row order before final admission.
            with atomic_text_writer(out/'source'/name,newline='') as stream:
                stream.write(data.decode('utf-8'))
        csv_write(out/'retained.csv',retained_raw,source['initial_fields'])
        json_write(out/'retained_check.json',retained_check)
        local=copy.deepcopy((future,fixed));local_before=copy.deepcopy(local)
        solver_dir=out/'solver';solver_dir.mkdir()
        json_write(out/'solver_claim.json',dict(state='pending',solver_calls=0,
            solver_calls_range=[0,1],clock_budget_seconds=30))
        from .scheduler import schedule
        from .no_fault_clock import NativeSolverClock
        clock=NativeSolverClock(solver_dir)
        started=now();start=time.perf_counter()
        # No variable preparation or I/O is allowed from this check through schedule.
        if remaining_seconds() < 32:
            raise BudgetExpired('full 30-second solver block plus finalization unavailable at call boundary')
        clock.activate()
        solver_calls=1
        proposal=schedule(local[0],method,fixed=local[1],interrupted=[],clock=clock)
        seconds=time.perf_counter()-start;ended=now()
        json_write(out/'solver_claim.json',dict(solver_calls=solver_calls,clock_budget_seconds=30))
        # RETURNED is diagnostic only; business checks must still complete for PASS.
        json_write(out/'solver_execution.json',dict(status='RETURNED',solver_calls=solver_calls,
            clock_budget_seconds=30,started_at=started,ended_at=ended,seconds=seconds))
    except Exception as error:
        json_write(out/'solver_execution.json',dict(status='BLOCKED' if isinstance(error,BudgetExpired) else 'FAIL',
            solver_calls=solver_calls,clock_budget_seconds=30,reason=str(error),exception_type=type(error).__name__,
            raw_exit_code=3 if isinstance(error,BudgetExpired) else 1,started_at=started,ended_at=now(),seconds=time.perf_counter()-start))
        raise
    finally:
        if clock is not None:
            clock.logfile.close()
    require(local==local_before,'scheduler mutated input')
    write_tasks(out/'raw_proposal.csv',proposal,source['initial_fields'])
    # Legality is checked before invoking selection. Invalid proposals are failures, never KEEP success.
    proposal_check=verify_tasks(source,decision_s,proposal,False)
    json_write(out/'proposal_check.json',proposal_check)
    selected,receipt=choose_from_files(out/'source',expected_sha256,decision_s,proposal)
    src=out/('raw_proposal.csv' if receipt['selected_source']=='raw_proposal' else 'retained.csv')
    from .atomic_io import atomic_text_writer
    with atomic_text_writer(out/'selected.csv',newline='') as stream:
        stream.write(src.read_bytes().decode('utf-8'))
    selected_check=verify_tasks(source,decision_s,selected)
    json_write(out/'selected_check.json',selected_check)
    require(before==tree_snapshot(source['root']),'source changed during production')
    execution=dict(status='PASS',solver_calls=1,clock_budget_seconds=30,raw_exit_code=0,timed_out=False,
        started_at=started,ended_at=ended,seconds=seconds,deep_inputs_unchanged=True,
        scheduler_module='planner.scheduler',scheduler_sha256=file_identity(Path(__file__).with_name('scheduler.py'))['sha256'],
        internal_improved_fallback='NOT_OBSERVABLE')
    json_write(out/'solver_execution.json',execution)
    receipt.update(method=method+'+guard',decision_s=decision_s,
        decision_utc=(source['context']['day']+timedelta(seconds=decision_s)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        source_sha256=expected_sha256.lower(),initial=source['manifest']['members']['initial_plan.csv'],
        candidates=source['manifest']['members']['candidates.csv'],
        plans={n:file_identity(out/f'{n}.csv') for n in ('retained','raw_proposal','selected')},
        solver=execution,reason='Exact authority integer-points then coverage; strict improvement only; whole-tuple ties preserve original.')
    json_write(out/'branch_receipt.json',receipt)
    json_write(out/'run.json',dict(schema_version=1,kind='native_no_fault_run',mode='no-fault',
        source='source',source_sha256=expected_sha256.lower(),decision_s=decision_s,method=method,
        plans=dict(retained='retained.csv',raw_proposal='raw_proposal.csv',selected='selected.csv')))
    seal_run(out)
    return 0

def evaluate(source_dir, expected_sha256, decision_s, tasks_path, out):
    source=load_source(source_dir,expected_sha256)
    decision(decision_s,source['context']['horizon'])
    tasks_path=reject_links(tasks_path)
    fields,raw,tasks=read_complete_tasks(tasks_path)
    out=checked_output(out,[source['root'],tasks_path])
    before=tree_snapshot(source['root']);task_before=file_identity(tasks_path)
    report=verify_tasks(source,decision_s,tasks)
    # On a tie, verify raw values/order, not only normalized numeric equivalence.
    if report['J']==report['J_retained']:
        expected=[dict(r,status=status_at({'start_s':int(r['start_s']),'end_s':int(r['end_s'])},decision_s)) for r in source['initial_raw']]
        require(fields==source['initial_fields'] and raw==expected,'equal J must preserve original raw fields/order')
    require(before==tree_snapshot(source['root']) and task_before==file_identity(tasks_path),'evaluation inputs changed')
    out.mkdir(parents=True,exist_ok=False)
    report.update(source_sha256=expected_sha256.lower(),tasks=task_before,source_unchanged=True,tasks_unchanged=True)
    json_write(out/'evaluation.json',report)
    return 0

def inspect_run(run_dir, expected_sha256):
    root=reject_links(run_dir)
    manifest=strict_json(root/'evidence_manifest.json')
    require(set(manifest)=={'schema_version','kind','files'} and type(manifest['schema_version']) is int and
            manifest['schema_version']==1 and manifest['kind']=='no_fault_result_manifest','result manifest schema')
    require(isinstance(manifest['files'],dict),'result files mapping')
    for name,entry in manifest['files'].items():
        safe_relative(name);reject_links(root/name)
        require(set(entry)=={'bytes','sha256'} and file_identity(root/name)==entry,'result file hash mismatch: '+name)
    require(artifact_files(root)==manifest['files'],'result manifest file set/hash mismatch')
    run=strict_json(root/'run.json')
    require(set(run)=={'schema_version','kind','mode','source','source_sha256','decision_s','method','plans'} and
            type(run['schema_version']) is int and run['schema_version']==1 and run['kind']=='native_no_fault_run' and run['mode']=='no-fault','run schema')
    require(run['source']=='source' and run['plans']==dict(retained='retained.csv',raw_proposal='raw_proposal.csv',selected='selected.csv'),'run relative references')
    require(run['source_sha256']==expected_sha256.lower(),'recheck expected original source SHA differs')
    require(run['method'] in ('baseline','improved'),'run single method')
    source=load_source(root/'source',expected_sha256)
    decision(run['decision_s'],source['context']['horizon'])
    return root,run,source

def recheck(run_dir, expected_sha256, out):
    root,run,source=inspect_run(run_dir,expected_sha256)
    out=checked_output(out,[root])
    before=tree_snapshot(root);t=run['decision_s']
    reports={};raws={};plans={}
    for name in ('retained','raw_proposal','selected'):
        fields,raw,tasks=read_complete_tasks(root/f'{name}.csv')
        reports[name]=verify_tasks(source,t,tasks,name!='raw_proposal')
        raws[name]=(fields,raw);plans[name]=tasks
    expected=[dict(r,status=status_at({'start_s':int(r['start_s']),'end_s':int(r['end_s'])},t)) for r in source['initial_raw']]
    require(raws['retained']==(source['initial_fields'],expected),'retained is not exact original+t')
    jr,jp=tuple(reports['retained']['J']),tuple(reports['raw_proposal']['J'])
    branch='ACCEPT_CANDIDATE_STRICTLY_BETTER' if jp>jr else 'KEEP_RETAINED_LOWER_CANDIDATE' if jp<jr else 'KEEP_RETAINED_TIE'
    selected_source='raw_proposal' if jp>jr else 'retained_original'
    name='raw_proposal' if jp>jr else 'retained'
    require(raws['selected']==raws[name] and (root/'selected.csv').read_bytes()==(root/f'{name}.csv').read_bytes(),'selected source mismatch')
    receipt=strict_json(root/'branch_receipt.json')
    require(receipt['branch']==branch and receipt['selected_source']==selected_source and receipt['method']==run['method']+'+guard' and
            receipt['source_sha256']==expected_sha256.lower() and receipt['decision_s']==t,'branch/identity mismatch')
    from datetime import timedelta
    require(receipt['decision_utc']==(source['context']['day']+timedelta(seconds=t)).strftime('%Y-%m-%dT%H:%M:%SZ'),'decision UTC mismatch')
    for name in ('retained','raw_proposal','selected'):
        require(receipt['plans'][name]==file_identity(root/f'{name}.csv'),'receipt plan hash')
    for name,field in [('retained','J_retained'),('raw_proposal','J_proposal'),('selected','J_selected')]:
        require(receipt[field]==reports[name]['J'],'receipt objective mismatch')
    require(receipt['initial']==source['manifest']['members']['initial_plan.csv'] and receipt['candidates']==source['manifest']['members']['candidates.csv'],'receipt source members')
    require(receipt['physical_changes']==changes(source['initial'],plans['selected']),'physical change receipt mismatch')
    ex=strict_json(root/'solver_execution.json')
    require(ex==receipt['solver'] and ex['solver_calls']==1 and ex['clock_budget_seconds']==30 and
            ex['raw_exit_code']==0 and ex['status']=='PASS' and not ex['timed_out'] and ex['deep_inputs_unchanged'],'solver receipt mismatch')
    require(strict_json(root/'solver_claim.json')==dict(solver_calls=1,clock_budget_seconds=30),'solver claim mismatch')
    for name,filename in [('retained','retained_check.json'),('raw_proposal','proposal_check.json'),('selected','selected_check.json')]:
        require(strict_json(root/filename)==reports[name],'saved independent check mismatch')
    require(before==tree_snapshot(root),'recheck changed source')
    out.mkdir(parents=True,exist_ok=False)
    json_write(out/'recheck.json',dict(status='PASS',schema_version=1,mode='no-fault',source_sha256=expected_sha256.lower(),
        source_unchanged=True,solver_calls=0,source_snapshot='source',reports=reports,branch=branch,
        physical_changes=receipt['physical_changes'],business_reads='Only safe relative members of this run and its source snapshot'))
    return 0
