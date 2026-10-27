"""Parent deadline regressions; child outcomes are controlled, no solver is run."""
import copy,csv,json,shutil,sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from no_fault_fixtures import fixture,write_csv
from planner import no_fault_cli as cli


def run_parent(tmp_path,monkeypatch,*,late=None,remaining=-0.1,
               child_exit=0,input_changed=False,mode='evaluate-no-fault'):
    source=tmp_path/'source';sha=fixture(source);tasks=tmp_path/'tasks.csv'
    with (source/'initial_plan.csv').open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
    write_csv(tasks,[dict(row,status='new_success') for row in rows],fields)
    out=tmp_path/'workflow';tick=[1000.0];budget=70 if mode=='no-fault' else 5
    deadline=tick[0]+budget;calls=[];fired=[];written=[];observed_deadlines=[]
    real_snapshot=cli.tree_snapshot;real_write=cli.json_write

    def consume_remaining():
        if not fired:
            tick[0]=deadline-remaining;fired.append(late)

    def guarded(command,**kwargs):
        # Only the parent is under test. These are explicitly controlled child
        # returns, not substitutes for native evaluation or scheduler tests.
        child_mode=command[5];calls.append(child_mode)
        observed_deadlines.append(kwargs['deadline_monotonic'])
        if child_mode=='produce' and child_exit==0:
            run=out/'run';run.mkdir()
            shutil.copytree(source,run/'source')
            shutil.copyfile(tasks,run/'selected.csv')
            (run/'solver_execution.json').write_text(
                json.dumps(dict(solver_calls=1)),encoding='utf-8')
        return dict(exit_code=child_exit,timed_out=child_exit==3,
                    seconds=0.1,tree_guard='controlled child result')

    def snapshot(path):
        value=real_snapshot(path)
        # This is the final protected-input scan, not the no-fault result scan.
        if calls and Path(path)==source:
            if late=='snapshot':consume_remaining()
            if input_changed:
                value=copy.deepcopy(value);value['test_observed_change']=True
        return value

    def write(path,value):
        real_write(path,value)
        if Path(path)==out/'workflow.json':
            written.append(value['status'])
            if calls and (late=='command_receipt' and value['status']=='RUNNING'
                          or late=='success_receipt' and value['status']=='PASS'):
                consume_remaining()

    monkeypatch.setattr(cli.time,'monotonic',lambda:tick[0])
    monkeypatch.setattr(cli,'run_guarded',guarded)
    monkeypatch.setattr(cli,'tree_snapshot',snapshot)
    monkeypatch.setattr(cli,'json_write',write)
    args=SimpleNamespace(budget_seconds=budget,source=str(source),source_sha256=sha,
        mode=mode,out=str(out),decision_s=0,tasks=str(tasks),method='baseline',python=sys.executable)
    code=cli.workflow(args)
    receipt=json.loads((out/'workflow.json').read_text(encoding='utf-8'))
    assert observed_deadlines and all(value==deadline for value in observed_deadlines)
    return code,receipt,calls,written,fired


@pytest.mark.parametrize('remaining,expected',[(0.001,0),(0,3),(-0.1,3)])
def test_final_protected_scan_obeys_parent_deadline(tmp_path,monkeypatch,remaining,expected):
    code,receipt,calls,_,fired=run_parent(tmp_path,monkeypatch,late='snapshot',remaining=remaining)
    assert fired and calls==['evaluate']
    assert code==expected and receipt['exit_code']==expected
    assert receipt['status']==('PASS' if expected==0 else 'BLOCKED')
    assert receipt['protected_unchanged'] and receipt['solver_calls']==0
    assert receipt['original_raw_return_codes']==[0]


@pytest.mark.parametrize('stage',['command_receipt','success_receipt'])
def test_slow_receipt_write_cannot_leave_success_after_deadline(tmp_path,monkeypatch,stage):
    code,receipt,calls,written,fired=run_parent(tmp_path,monkeypatch,late=stage)
    assert fired and calls==['evaluate']
    assert code==3 and receipt['status']=='BLOCKED' and receipt['exit_code']==3
    assert receipt['solver_calls']==0 and receipt['original_raw_return_codes']==[0]
    assert written[-1]=='BLOCKED'
    if stage=='success_receipt':assert written[-2:]==['PASS','BLOCKED']


def test_finalization_block_preserves_observed_solver_count(tmp_path,monkeypatch):
    code,receipt,calls,_,_=run_parent(tmp_path,monkeypatch,late='snapshot',mode='no-fault')
    assert calls==['produce','evaluate','recheck']
    assert code==3 and receipt['status']=='BLOCKED'
    assert receipt['solver_calls']==1 and receipt['original_raw_return_codes']==[0,0,0]
    assert receipt['protected_unchanged'] and receipt['run_source_unchanged']


def test_parent_completion_within_budget_still_passes(tmp_path,monkeypatch):
    code,receipt,calls,written,fired=run_parent(tmp_path,monkeypatch)
    assert code==0 and receipt['status']=='PASS' and calls==['evaluate']
    assert not fired and written[-1]=='PASS'
    assert receipt['solver_calls']==0 and receipt['protected_unchanged']


@pytest.mark.parametrize('child_exit,input_changed,expected',[(7,False,2),(3,False,3),(0,True,2)])
def test_deadline_does_not_replace_existing_failure(tmp_path,monkeypatch,child_exit,input_changed,expected):
    code,receipt,_,_,_=run_parent(tmp_path,monkeypatch,late='snapshot',
        child_exit=child_exit,input_changed=input_changed)
    assert code==expected and receipt['exit_code']==expected
    assert receipt['status']==('FAIL' if expected==2 else 'BLOCKED')
    assert receipt['solver_calls']==0
    assert receipt['original_raw_return_codes']==[child_exit]
    assert 'budget exhausted' not in receipt.get('reason','')
