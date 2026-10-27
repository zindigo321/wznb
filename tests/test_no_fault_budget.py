import contextlib,json,time
from pathlib import Path
import pytest
from no_fault_fixtures import fixture
from planner import no_fault as nf,scheduler,atomic_io
from planner.common import BudgetExpired

@pytest.mark.parametrize('stage,budget,delay,admit',[
 ('source',32.8,4.2,False),('claim',32.8,4.2,False),('log',32.8,4.2,False),
 ('source',40,4.2,True),('claim',40,4.2,True),('log',40,4.2,True),
 ('none',31.999999,0,False),('none',32,0,True),('none',32.000001,0,True),
 ('validation',34,2.000001,False),('validation',34,2,True),('validation',34,1.999999,True)])
def test_final_boundary_observer(tmp_path,monkeypatch,stage,budget,delay,admit):
    source=tmp_path/'source';sha=fixture(source);out=tmp_path/'out'
    tick=[1000.0];deadline=tick[0]+budget;events=[];fired=[]
    monkeypatch.setattr(time,'monotonic',lambda:tick[0])
    def spend(kind):
        if stage==kind and not fired:tick[0]+=delay;fired.append(kind)
    real_writer=atomic_io.atomic_text_writer
    @contextlib.contextmanager
    def slow_writer(path,*a,**kw):
        if Path(path).is_relative_to(out/'source'):spend('source')
        with real_writer(path,*a,**kw) as stream:yield stream
    monkeypatch.setattr(atomic_io,'atomic_text_writer',slow_writer)
    real_json=nf.json_write
    def slow_json(path,obj):
        if Path(path).name=='solver_claim.json':spend('claim')
        return real_json(path,obj)
    monkeypatch.setattr(nf,'json_write',slow_json)
    real_open=Path.open
    def slow_open(path,*a,**kw):
        if path.name=='run.log':spend('log')
        return real_open(path,*a,**kw)
    monkeypatch.setattr(Path,'open',slow_open)
    real_load=nf.load_source
    def slow_load(*a,**kw):
        result=real_load(*a,**kw);spend('validation');return result
    monkeypatch.setattr(nf,'load_source',slow_load)
    real_schedule=scheduler.schedule
    def observe(*a,**kw):
        clock=kw['clock'];events.append(dict(parent_left=deadline-tick[0],clock_left=clock.left(),allocation=clock.deadline-clock.start))
        return real_schedule(*a,**kw)
    monkeypatch.setattr(scheduler,'schedule',observe)
    if admit:
        assert nf.produce(source,sha,120,'baseline',out,lambda:deadline-tick[0])==0
        assert len(events)==1 and events[0]['parent_left']>=32
        assert events[0]['allocation']==pytest.approx(30,abs=1e-9) and events[0]['clock_left']==pytest.approx(30,abs=1e-9)
        receipt=json.loads((out/'branch_receipt.json').read_text());assert receipt['branch']=='ACCEPT_CANDIDATE_STRICTLY_BETTER'
    else:
        with pytest.raises(BudgetExpired):nf.produce(source,sha,120,'baseline',out,lambda:deadline-tick[0])
        assert events==[]
        assert not (out/'selected.csv').exists() and not (out/'branch_receipt.json').exists()
    assert deadline==1000+budget

@pytest.mark.parametrize('error',[BudgetExpired('observer budget'),RuntimeError('observer runtime')])
def test_observed_post_entry_failure(tmp_path,monkeypatch,error):
    source=tmp_path/'source';sha=fixture(source);out=tmp_path/'out';events=[]
    def observe(*a,**kw):events.append(1);raise error
    monkeypatch.setattr(scheduler,'schedule',observe)
    with pytest.raises(type(error),match=str(error)):nf.produce(source,sha,120,'baseline',out,lambda:60)
    ex=json.loads((out/'solver_execution.json').read_text())
    assert events==[1] and ex['solver_calls']==1 and ex['exception_type']==type(error).__name__
    assert ex['status']==('BLOCKED' if isinstance(error,BudgetExpired) else 'FAIL')
    assert not (out/'selected.csv').exists() and not (out/'branch_receipt.json').exists()

@pytest.mark.parametrize('attack',['wrong_sha','illegal_source','history'])
def test_input_error_precedes_budget(tmp_path,monkeypatch,attack):
    from no_fault_fixtures import mutate_csv,freeze
    source=tmp_path/'source';sha=fixture(source);calls=[]
    if attack=='wrong_sha':sha='0'*64
    else:
        mutate_csv(source/'initial_plan.csv',lambda rows:rows[0].update(**({'points_int':'999'} if attack=='illegal_source' else {'status':'completed'})))
        sha=freeze(source)
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**kw:calls.append(1))
    with pytest.raises(ValueError):nf.produce(source,sha,120,'baseline',tmp_path/'out',lambda:0)
    assert calls==[]
