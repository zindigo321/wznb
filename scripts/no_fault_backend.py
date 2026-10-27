"""Thin native backend. Shared runner implementation; invoked under the workflow supervisor."""
import argparse,sys,time,traceback,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from planner.no_fault_cli import decimal_decision
from planner.no_fault import checked_output
from planner.common import BudgetExpired
from planner.atomic_io import atomic_write_text

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['produce','evaluate','recheck'])
    p.add_argument('--source',required=True);p.add_argument('--source-sha256',required=True)
    p.add_argument('--out',required=True);p.add_argument('--budget-seconds',type=float,required=True)
    p.add_argument('--decision-s',type=decimal_decision);p.add_argument('--method',choices=['baseline','improved'])
    p.add_argument('--tasks')
    p.add_argument('--deadline-monotonic',type=float,required=True)
    a=p.parse_args()
    try:
        import math
        if not math.isfinite(a.budget_seconds) or not 1<a.budget_seconds<=300:
            raise ValueError('backend budget must be finite, >1 and <=300')
        if not math.isfinite(a.deadline_monotonic) or a.deadline_monotonic<=0 or a.deadline_monotonic>time.monotonic()+a.budget_seconds:
            raise ValueError('invalid same-host monotonic parent deadline')
        if a.mode in ('produce','evaluate') and a.decision_s is None:
            raise ValueError('decision-s required')
        if (a.mode=='produce' and (a.method is None or a.tasks is not None) or
            a.mode=='evaluate' and (a.tasks is None or a.method is not None) or
            a.mode=='recheck' and any(v is not None for v in (a.decision_s,a.tasks,a.method))):
            raise ValueError('backend mode parameter conflict')
        from planner.runner import run_no_fault,evaluate_no_fault,recheck_no_fault
        if a.mode=='produce':
            return run_no_fault(a.source,a.source_sha256,a.decision_s,a.method,Path(a.out),
                                lambda:a.deadline_monotonic-time.monotonic())
        if a.mode=='evaluate':
            return evaluate_no_fault(a.source,a.source_sha256,a.decision_s,a.tasks,Path(a.out))
        return recheck_no_fault(a.source,a.source_sha256,Path(a.out))
    except Exception as error:
        code=3 if isinstance(error,(BudgetExpired,FileNotFoundError,PermissionError)) else 2
        info=dict(status='BLOCKED' if code==3 else 'FAIL',exit_code=code,reason=str(error),
                  exception_type=type(error).__name__,original_exception_traceback=traceback.format_exc())
        # Do not write into an existing or rejected output; supervisor preserves stderr verbatim.
        print(json.dumps(info,ensure_ascii=False),file=sys.stderr)
        return code

if __name__=='__main__':
    raise SystemExit(main())
