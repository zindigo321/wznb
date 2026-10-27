"""Portable window-mode planning, independent evaluation and read-only recheck."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
import planner
from planner.atomic_io import atomic_write_text
from planner.common import json_write
from planner.config import normalize
from planner.path_safety import guard_writes, tree_snapshot
from planner.process_guard import run_guarded
from planner.runner import versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable, help='Interpreter for child commands; defaults to the launching Python')
    parser.add_argument('--budget-seconds', type=float, default=120, help='Whole workflow, at most 300 seconds')
    sub = parser.add_subparsers(dest='mode', required=True)
    for name in ('synthetic', 'windows', 'evaluate', 'recheck'):
        p = sub.add_parser(name); p.add_argument('--out', required=True)
        if name != 'recheck':
            p.add_argument('--method', choices=['baseline','improved','both'], default='baseline')
        if name in ('windows','evaluate'):
            p.add_argument('--input-dir', required=True); p.add_argument('--config', required=True)
        if name == 'evaluate':
            p.add_argument('--tasks', required=True); p.add_argument('--initial-plan'); p.add_argument('--event'); p.add_argument('--interrupted')
        if name in ('synthetic','windows'): p.add_argument('--fault-event', help='Explicit event for planning, distinct from evaluate --event')
        if name == 'recheck': p.add_argument('--source', required=True); p.add_argument('--source-sha256')
    from planner.no_fault_cli import decimal_decision
    for name in ('no-fault','evaluate-no-fault'):
        p = sub.add_parser(name)
        p.add_argument('--source',required=True); p.add_argument('--source-sha256',required=True)
        p.add_argument('--decision-s',type=decimal_decision,required=True); p.add_argument('--out',required=True)
        if name=='no-fault': p.add_argument('--method',choices=['baseline','improved'],required=True)
        else: p.add_argument('--tasks',required=True)
    a = parser.parse_args()
    if a.mode in ('no-fault','evaluate-no-fault') or (a.mode=='recheck' and
            (a.source_sha256 is not None or (Path(a.source)/'run.json').exists())):
        from planner.no_fault_cli import workflow
        return workflow(a)
    try:
        if not 1 < a.budget_seconds <= 300: raise ValueError('workflow budget must be >1 and <=300 seconds')
        out = Path(a.out).resolve()
        config_path = ROOT/'configs/synthetic.yaml' if a.mode=='synthetic' else Path(a.source).resolve()/'effective_config.json' if a.mode=='recheck' else Path(a.config).resolve()
        source = Path(a.source).resolve() if a.mode=='recheck' else None
        directory = ROOT/'examples/synthetic' if a.mode=='synthetic' else source/'standard_input' if source else Path(a.input_dir).resolve()
        protected = [directory, config_path] + ([source] if source else [])
        extras = {}
        for name in ('tasks','initial_plan','event','interrupted','fault_event'):
            value = getattr(a,name,None)
            if value:
                extras[name] = Path(value).resolve(); protected.append(extras[name])
        guard_writes([out], protected)
        if out.exists(): raise ValueError('workflow output must be a new directory')
        if a.mode=='evaluate' and any(k in extras for k in ('initial_plan','event','interrupted')) and not all(k in extras for k in ('initial_plan','event','interrupted')):
            raise ValueError('fault evaluation requires initial-plan, event AND interrupted together')
        config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
        if config.get('input_mode') != 'windows': raise ValueError('portable workflow requires window-mode config; TLE replay uses the original documented entry')
        if config.get('reuse_source') or config.get('frozen_sample'):
            raise ValueError('window config must not carry frozen TLE/reuse paths')
        if a.mode in ('evaluate','recheck'):
            # Historical injection is planning-only; keep the source config untouched.
            # Evaluation uses explicit evidence; recheck uses the sealed source evidence.
            config.pop('fault_event_path',None)
        elif config.get('fault_event_path'):
            raise ValueError('window planning config must not carry fault paths; pass --fault-event explicitly')
        config.update(input_dir=str(directory), cache_dir=str(out/'cache'), method=getattr(a,'method','baseline'))
        config = normalize(config)
        # A full tree snapshot also covers empty directories and aliases.
        protected_before = {str(p): tree_snapshot(p) for p in protected}
        out.mkdir(parents=True, exist_ok=False)
        prepared = out/'config.yaml'
        atomic_write_text(prepared, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        receipt = dict(status='RUNNING', package_root=str(ROOT), planner_import=str(Path(planner.__file__).resolve()),
                       python=sys.executable, child_python=a.python, versions=versions(), mode=a.mode,
                       input_directory=str(directory), commands=[], evaluation_scope='supplied_windows_only',
                       external_team3_confirmation='PENDING_EXTERNAL')
        json_write(out/'workflow.json',receipt)
        started = time.perf_counter(); code = 0
        def run(arguments, destination):
            remaining = a.budget_seconds - (time.perf_counter()-started)
            if remaining <= 1: return 3
            command = [a.python,'-X','utf8',str(ROOT/'smoke_test.py'),'--config',str(prepared),
                       '--out',str(destination),'--budget-seconds',str(remaining),*arguments]
            result = run_guarded(command, timeout=remaining, log_path=out/f'command_{len(receipt["commands"])+1:02d}.log', cwd=ROOT)
            result.update(command=command,output=str(destination))
            receipt['commands'].append(result);json_write(out/'workflow.json',receipt)
            return result['exit_code'] if result['exit_code'] in (0,2,3) else 2
        try:
            if a.mode == 'recheck':
                code = run(['--check-only','--source',str(source)],out/'recheck')
            elif a.mode == 'evaluate':
                args = ['--evaluate',str(extras['tasks'])]
                for key in ('initial_plan','event','interrupted'):
                    if key in extras: args += ['--'+key.replace('_','-'),str(extras[key])]
                code = run(args,out/'evaluation')
            else:
                run_out = out/'run'
                args = ['--method',a.method]
                if 'fault_event' in extras: args += ['--fault-event',str(extras['fault_event'])]
                code = run(args,run_out)
                if code == 0:
                    metrics = json.loads((run_out/'metrics.json').read_text(encoding='utf-8'))
                    receipt['metrics'] = metrics
                    source_before = tree_snapshot(run_out)
                    methods = ['baseline','improved'] if a.method=='both' else [a.method]
                    for method in methods:
                        code = run(['--evaluate',str(run_out/f'tasks_{method}.csv')],out/f'evaluate_{method}')
                        if code: break
                        if metrics['fault'].get('effective'):
                            name = 'baseline_replanned' if method=='baseline' else 'replanned'
                            code = run(['--evaluate',str(run_out/f'tasks_{name}.csv'),'--initial-plan',str(run_out/f'tasks_{method}.csv'),
                                        '--event',str(run_out/'fault_event.json'),'--interrupted',str(run_out/f'interrupted_{method}.csv')],out/f'evaluate_fault_{method}')
                            if code: break
                    if code == 0: code = run(['--check-only','--source',str(run_out)],out/'recheck')
                    receipt['run_source_tree_before'] = source_before
                    receipt['run_source_tree_after'] = tree_snapshot(run_out)
                    receipt['run_source_unchanged'] = source_before == receipt['run_source_tree_after']
                    if not receipt['run_source_unchanged']: code = 2
        except (OSError,ValueError,KeyError,TypeError) as error:
            code=3 if isinstance(error,FileNotFoundError) else 2;receipt['reason']=str(error)
        finally:
            protected_after = {str(p): tree_snapshot(p) for p in protected}
            receipt.update(protected_before=protected_before,protected_after=protected_after,
                           protected_unchanged=protected_before==protected_after,seconds=time.perf_counter()-started)
            if not receipt['protected_unchanged']: code=2
            receipt.update(exit_code=code,status='PASS' if code==0 else 'BLOCKED' if code==3 else 'FAIL')
            json_write(out/'workflow.json',receipt)
        print(json.dumps(dict(status=receipt['status'],exit_code=code,receipt=str(out/'workflow.json')),ensure_ascii=False))
        return code
    except (FileNotFoundError,ValueError,OSError,KeyError,TypeError,yaml.YAMLError) as error:
        code=3 if isinstance(error,FileNotFoundError) else 2
        print(('BLOCKED: ' if code==3 else 'FAIL: ')+str(error),file=sys.stderr);return code


if __name__ == '__main__':
    raise SystemExit(main())
