import contextlib,json,os,runpy,subprocess,sys,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from no_fault_fixtures import fixture
from planner import no_fault as nf,no_fault_cli as cli,scheduler,atomic_io
from planner.common import BudgetExpired
ROOT=Path(__file__).resolve().parents[1]
# When replayed against R10, the imported planner, not this observer file, defines ROOT.
ROOT=Path(nf.__file__).resolve().parents[1]


def test_real_slow_snapshot(tmp_path,monkeypatch,record_property):
    source=tmp_path/'s';sha=fixture(source);out=tmp_path/'out';entries=[];samples=[];fired=[]
    deadline=time.monotonic()+32.8;real_writer=atomic_io.atomic_text_writer;real=scheduler.schedule
    @contextlib.contextmanager
    def slow(path,*a,**kw):
        if Path(path).is_relative_to(out/'source') and not fired:
            fired.append(1);time.sleep(4.2)
        with real_writer(path,*a,**kw) as stream:yield stream
    def remaining():
        value=deadline-time.monotonic();samples.append(value);return value
    def observe(*a,**kw):
        entries.append(dict(parent_left=deadline-time.monotonic(),clock_left=kw['clock'].left()));return real(*a,**kw)
    monkeypatch.setattr(atomic_io,'atomic_text_writer',slow);monkeypatch.setattr(scheduler,'schedule',observe)
    code=0
    try:nf.produce(source,sha,120,'baseline',out,remaining)
    except BudgetExpired:code=3
    record_property('observer',json.dumps(dict(code=code,entry_samples=entries,admission_samples=samples,delay=4.2)))
    assert code==3 and entries==[] and samples[-1]<32
    assert not (out/'selected.csv').exists() and not (out/'branch_receipt.json').exists()


def child_observer(tmp_path,monkeypatch,mode,budget):
    source=tmp_path/'s';sha=fixture(source);out=tmp_path/'out';marker=tmp_path/'entry.json';heartbeat=tmp_path/'heartbeat'
    script=tmp_path/'observer.py'
    script.write_text('''import os,sys,time,json,runpy,subprocess
from pathlib import Path
root,marker,heartbeat,mode,parent_deadline=sys.argv[1:6]
backend=sys.argv[6];args=sys.argv[7:]
start=time.monotonic()
if mode=='startup':time.sleep(1.4)
sys.path.insert(0,root)
from planner import scheduler
from planner.common import BudgetExpired
real=scheduler.schedule
def observe(*a,**kw):
    Path(marker).write_text(json.dumps(dict(entered=True,pid=os.getpid(),child_started=start,at=time.monotonic(),parent_left=float(parent_deadline)-time.monotonic(),clock_left=kw['clock'].left(),clock_budget=kw['clock'].deadline-kw['clock'].start)))
    if mode=='timeout':
        child="import sys,time;from pathlib import Path;p=Path(sys.argv[1]);end=time.monotonic()+8\\nwhile time.monotonic()<end:p.write_text(str(time.monotonic()));time.sleep(.04)"
        subprocess.Popen([sys.executable,'-B','-c',child,heartbeat],creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        time.sleep(10)
    if mode=='BudgetExpired':raise BudgetExpired('observer post-entry BudgetExpired')
    if mode=='RuntimeError':raise RuntimeError('observer post-entry RuntimeError')
    return real(*a,**kw)
scheduler.schedule=observe
sys.argv=[backend,*args]
raise SystemExit(runpy.run_path(backend)['main']())
''',encoding='utf-8')
    guard=cli.run_guarded;commands=[];parent_deadline=time.monotonic()+budget
    def wrapped(command,**kw):
        actual=[sys.executable,'-B','-X','utf8',str(script),str(ROOT),str(marker),str(heartbeat),mode,str(parent_deadline),*command[4:]]
        commands.append(dict(actual=actual,guard_deadline=kw.get('deadline_monotonic'),timeout=kw['timeout']))
        if mode=='timeout':
            kw['timeout']=2
            if 'deadline_monotonic' in kw:kw['deadline_monotonic']=min(kw['deadline_monotonic'],time.monotonic()+2)
        return guard(actual,**kw)
    monkeypatch.setattr(cli,'run_guarded',wrapped)
    args=SimpleNamespace(budget_seconds=budget,source=str(source),source_sha256=sha,mode='no-fault',out=str(out),decision_s=120,method='baseline',python=sys.executable)
    code=cli.workflow(args);wf=json.loads((out/'workflow.json').read_text())
    observed=json.loads(marker.read_text()) if marker.exists() else None
    return code,wf,observed,commands,out,heartbeat


def test_real_startup_delay(tmp_path,monkeypatch,record_property):
    code,wf,observed,commands,out,_=child_observer(tmp_path,monkeypatch,'startup',32.8)
    record_property('observer',json.dumps(dict(code=code,entry=observed,commands=commands,workflow=wf)))
    assert code==3 and wf['status']=='BLOCKED' and wf['solver_calls']==0 and observed is None
    assert not (out/'run/selected.csv').exists()


def test_real_post_entry_tree_timeout(tmp_path,monkeypatch,record_property):
    code,wf,observed,commands,out,heartbeat=child_observer(tmp_path,monkeypatch,'timeout',70)
    assert observed and observed['entered'] and observed['parent_left']>=32 and observed['clock_left']>29.9
    assert code==3 and wf['status']=='BLOCKED' and wf['commands'][0]['timed_out']
    assert wf['solver_calls'] is None and wf['solver_calls_range']==[0,1]
    assert not (out/'run/selected.csv').exists() and not (out/'run/branch_receipt.json').exists()
    before=heartbeat.read_bytes();time.sleep(.15);assert heartbeat.read_bytes()==before
    record_property('observer',json.dumps(dict(code=code,entry=observed,commands=commands,workflow=wf,descendant_heartbeat_stopped=True)))


@pytest.mark.parametrize('error,code',[('BudgetExpired',3),('RuntimeError',2)])
def test_real_post_entry_exception(tmp_path,monkeypatch,record_property,error,code):
    result,wf,observed,commands,out,_=child_observer(tmp_path,monkeypatch,error,70)
    assert result==code and observed['entered'] and wf['solver_calls']==1
    ex=json.loads((out/'run/solver_execution.json').read_text())
    assert ex['exception_type']==error and ex['raw_exit_code']==(3 if code==3 else 1)
    assert wf['original_raw_return_codes']==[code]
    assert not (out/'run/selected.csv').exists() and not (out/'run/branch_receipt.json').exists()
    assert error in (out/'command_01.log').read_text()
    record_property('observer',json.dumps(dict(code=result,entry=observed,workflow=wf,execution=ex)))


@pytest.mark.parametrize('elapsed,expected',[(.799999,0),(.8,0),(.800001,3),(1.4,3)])
def test_backend_absolute_deadline_deterministic(tmp_path,monkeypatch,elapsed,expected):
    source=tmp_path/'s';sha=fixture(source);out=tmp_path/'out';entries=[]
    tick=1000+elapsed;monkeypatch.setattr(time,'monotonic',lambda:tick)
    monkeypatch.setattr(sys,'argv',['backend','produce','--source',str(source),'--source-sha256',sha,'--out',str(out),'--budget-seconds','70','--deadline-monotonic','1032.8','--decision-s','120','--method','baseline'])
    real=scheduler.schedule
    def observer(*a,**kw):entries.append(kw['clock'].left());return real(*a,**kw)
    monkeypatch.setattr(scheduler,'schedule',observer)
    assert runpy.run_path(str(ROOT/'scripts/no_fault_backend.py'))['main']()==expected
    assert len(entries)==(1 if expected==0 else 0)


def test_native_clock_never_restarts(tmp_path):
    from planner.no_fault_clock import NativeSolverClock
    clock=NativeSolverClock(tmp_path)
    try:
        assert clock.start is None
        clock.activate();deadline=clock.deadline
        with pytest.raises(RuntimeError):clock.activate()
        assert clock.deadline==deadline
    finally:clock.logfile.close()


def test_cross_process_monotonic_basis(tmp_path,record_property):
    before=time.monotonic()
    r=subprocess.run([sys.executable,'-B','-c',"import time,json;print(json.dumps(dict(stamp=time.monotonic(),info=vars(time.get_clock_info('monotonic')))))"],capture_output=True,text=True,timeout=3)
    after=time.monotonic();value=json.loads(r.stdout)
    assert r.returncode==0 and before<=value['stamp']<=after
    assert value['info']==vars(time.get_clock_info('monotonic')) and value['info']['monotonic']
    record_property('observer',json.dumps(dict(parent_before=before,child=value,parent_after=after)))
