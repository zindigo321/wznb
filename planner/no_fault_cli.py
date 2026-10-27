"""Supervised native no-fault workflow; no implicit TLE/default-config fallback."""
import os,sys,time,traceback,json,math
from pathlib import Path
from .common import json_write
from .no_fault_source import load_source,strict_json,reject_links
from .no_fault_rules import decision
from .no_fault import checked_output,inspect_run
from .path_safety import tree_snapshot
from .process_guard import run_guarded

ROOT=Path(__file__).resolve().parents[1]

def decimal_decision(value):
    import argparse,re
    if not isinstance(value,str) or re.fullmatch('[0-9]+',value) is None:
        raise argparse.ArgumentTypeError('decision-s requires a decimal integer')
    return int(value)

def mapped_code(raw_code, timed_out=False):
    return 3 if timed_out or raw_code==3 else 0 if raw_code==0 else 2

def workflow(args):
    started=time.perf_counter()
    parent_start=time.monotonic()
    receipt=None;out=None
    for k in ('PYTHONDONTWRITEBYTECODE','PYTHONUTF8'):
        os.environ[k]='1'
    for k in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[k]='1'
    try:
        if not math.isfinite(args.budget_seconds) or not 1 < args.budget_seconds <= 300:
            raise ValueError('workflow budget must be finite, >1 and <=300 seconds')
        deadline=parent_start+args.budget_seconds
        expected=args.source_sha256
        source_path=reject_links(args.source)
        protected=[source_path]
        if args.mode=='recheck':
            _,_,source=inspect_run(source_path,expected)
        else:
            source=load_source(source_path,expected)
            decision(args.decision_s,source['context']['horizon'])
            if args.mode=='evaluate-no-fault':
                protected.append(reject_links(args.tasks))
        out=checked_output(args.out,protected)
        before={str(p):tree_snapshot(p) for p in protected}
        out.mkdir(parents=True,exist_ok=False)
        receipt=dict(schema_version=1,mode=args.mode,status='RUNNING',source_sha256=expected.lower(),
            package_root=str(ROOT),planner_import=str(Path(__file__).with_name('__init__.py')),
            commands=[],solver_calls=0,original_raw_return_codes=[],protected_before=before,
            parent_started_monotonic=parent_start,deadline_monotonic=deadline,
            clock_basis=vars(time.get_clock_info('monotonic')),
            source_identity_meaning='Original source_manifest.json external expected SHA256; never result evidence-manifest hash')
        json_write(out/'workflow.json',receipt)
        def run(mode, source, destination, extra=()):
            remaining=deadline-time.monotonic()
            if remaining<=1:
                receipt['reason']='workflow budget exhausted';return 3
            command=[args.python,'-B','-X','utf8',str(ROOT/'scripts/no_fault_backend.py'),mode,
                     '--source',str(source),'--source-sha256',expected,'--out',str(destination),
                     '--budget-seconds',str(args.budget_seconds),'--deadline-monotonic',repr(deadline),*map(str,extra)]
            rc=run_guarded(command,timeout=remaining,deadline_monotonic=deadline,
                           log_path=out/f'command_{len(receipt["commands"])+1:02d}.log',cwd=ROOT)
            rc.update(command=command,output=destination.relative_to(out).as_posix(),
                      mapped_exit_code=mapped_code(rc['exit_code'],rc['timed_out']))
            receipt['commands'].append(rc);receipt['original_raw_return_codes'].append(rc['exit_code'])
            json_write(out/'workflow.json',receipt)
            return rc['mapped_exit_code']
        if args.mode=='no-fault':
            code=run('produce',source_path,out/'run',['--decision-s',args.decision_s,'--method',args.method])
            execution=out/'run/solver_execution.json'
            claim=out/'run/solver_claim.json'
            if execution.exists():
                receipt['solver_calls']=strict_json(execution)['solver_calls']
                receipt['solver_count_evidence']='persisted execution diagnostic'
            elif claim.exists():
                # A pending prewrite cannot prove that a killed backend never entered.
                receipt['solver_calls']=None
                receipt['solver_calls_range']=[0,1]
                receipt['solver_count_evidence']='unknown: pending claim without execution diagnostic'
            else:
                receipt['solver_calls']=0
                receipt['solver_count_evidence']='no claim: preparation did not reach call boundary'
            if code==0:
                run_before=tree_snapshot(out/'run')
                code=run('evaluate',out/'run/source',out/'evaluation',['--decision-s',args.decision_s,'--tasks',out/'run/selected.csv'])
                if code==0:
                    code=run('recheck',out/'run',out/'recheck')
                receipt['run_source_unchanged']=run_before==tree_snapshot(out/'run')
                if not receipt['run_source_unchanged']:code=2
        elif args.mode=='evaluate-no-fault':
            code=run('evaluate',source_path,out/'evaluation',['--decision-s',args.decision_s,'--tasks',args.tasks])
        else:
            code=run('recheck',source_path,out/'recheck')
        receipt['protected_after']={str(p):tree_snapshot(p) for p in protected}
        receipt['protected_unchanged']=before==receipt['protected_after']
        if not receipt['protected_unchanged']:code=2
        if code==0 and time.monotonic()>=deadline:
            code=3
            receipt['reason']='workflow budget exhausted during final verification'
        receipt.update(exit_code=code,status='PASS' if code==0 else 'BLOCKED' if code==3 else 'FAIL',
                       seconds=time.perf_counter()-started)
        json_write(out/'workflow.json',receipt)
        if code==0 and time.monotonic()>=deadline:
            code=3
            receipt.update(exit_code=code,status='BLOCKED',
                           reason='workflow budget exhausted while saving final receipt',
                           seconds=time.perf_counter()-started)
            json_write(out/'workflow.json',receipt)
        print(json.dumps(dict(status=receipt['status'],exit_code=code,workflow=str(out/'workflow.json'))))
        return code
    except Exception as error:
        from .common import BudgetExpired
        code=3 if isinstance(error,(FileNotFoundError,PermissionError,BudgetExpired)) else 2
        diagnostic=dict(status='BLOCKED' if code==3 else 'FAIL',exit_code=code,
                        exception_type=type(error).__name__,reason=str(error),traceback=traceback.format_exc())
        if receipt is not None:
            receipt.update(diagnostic)
            json_write(out/'workflow.json',receipt)
        print(json.dumps(diagnostic,ensure_ascii=False),file=sys.stderr)
        return code
