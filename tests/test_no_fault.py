import copy,json,hashlib,os,sys,time,subprocess
from pathlib import Path
import pytest
from no_fault_fixtures import fixture,freeze,mutate_csv,write_json,write_csv,fp,FIELDS
from planner.no_fault_source import load_source
from planner.no_fault import produce,evaluate,recheck,choose_from_files,seal_run
from planner.no_fault_rules import status_at
from planner.io_contract import read_complete_tasks
from planner.common import BudgetExpired
from planner.path_safety import tree_snapshot
from planner.no_fault_cli import mapped_code
from planner import scheduler

def proposal(source, ids, t=0):
    s=load_source(source,fp(source/'source_manifest.json')['sha256'])
    return [dict(r,status=status_at(r,t)) for r in s['candidates'] if r['task_id'] in ids]

@pytest.mark.parametrize('case,old,new,expected',[
 ('higher_fewer',['a','b'],['c'],'ACCEPT_CANDIDATE_STRICTLY_BETTER'),
 ('same_more',['d','e'],['f','g','h'],'ACCEPT_CANDIDATE_STRICTLY_BETTER'),
 ('same_fewer',['f','g','h'],['d','e'],'KEEP_RETAINED_LOWER_CANDIDATE'),
 ('same_tuple',['c'],['i'],'KEEP_RETAINED_TIE')])
def test_lexicographic(tmp_path,case,old,new,expected):
    defs=[('a',1,'low',0,120,1),('b',2,'low',300,120,1),('c',3,'high',0,240,2),
      ('d',4,'medium',0,240,1),('e',5,'medium',300,240,1),('f',6,'low',0,240,3),
      ('g',7,'low',300,240,3),('h',8,'low',600,240,3),('i',3,'high',700,240,2)]
    root=tmp_path/'s';sha=fixture(root,defs,old);p=proposal(root,new)
    before=copy.deepcopy(p);selected,receipt=choose_from_files(root,sha,0,p)
    assert receipt['branch']==expected and p==before
    if expected=='KEEP_RETAINED_TIE':
        assert selected==proposal(root,['c'])
    selected[0]['station_id']=99
    assert p==before

@pytest.mark.parametrize('t,statuses',[(0,['new_success']*3),(120,['completed','locked','new_success']),(3600,['completed']*3)])
def test_history_boundaries(tmp_path,t,statuses):
    s=tmp_path/'s';sha=fixture(s)
    rows=proposal(s,['a','b','c'],t)
    selected,r=choose_from_files(s,sha,t,rows)
    assert [x['status'] for x in selected]==statuses
    assert selected[1]['start_s']==60 and selected[1]['end_s']==180

@pytest.mark.parametrize('value',[-1,3601,True,1.5,'120'])
def test_bad_decision_zero_solver(tmp_path,monkeypatch,value):
    s=tmp_path/'s';sha=fixture(s);calls=[]
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:calls.append(1))
    with pytest.raises(ValueError):produce(s,sha,value,'baseline',tmp_path/'o',lambda:100)
    assert calls==[] and not (tmp_path/'o').exists()

@pytest.mark.parametrize('empty_pool,initial_ids',[(True,[]),(False,[]),(False,['a','b','c','d'])])
def test_empty_and_full_horizon(tmp_path,empty_pool,initial_ids,monkeypatch):
    s=tmp_path/'s';sha=fixture(s,[] if empty_pool else None,initial_ids)
    calls=[];real=scheduler.schedule
    def count(*a,**k):calls.append(1);return real(*a,**k)
    monkeypatch.setattr(scheduler,'schedule',count)
    run=tmp_path/'run';assert produce(s,sha,3600,'baseline',run,lambda:100)==0
    assert len(calls)==1
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:pytest.fail('evaluation/recheck called solver'))
    assert evaluate(run/'source',sha,3600,run/'selected.csv',tmp_path/'ev')==0
    assert recheck(run,sha,tmp_path/'rc')==0
    if not initial_ids:assert read_complete_tasks(run/'selected.csv')[2]==[]

@pytest.mark.parametrize('attack',['points','score','precise_score','priority','window','duplicate','overlap','noncandidate','utc'])
def test_illegal_original_zero_solver(tmp_path,monkeypatch,attack):
    s=tmp_path/'s';fixture(s)
    def mutate(rows):
        if attack=='points':rows[0]['points_int']='999'
        elif attack=='score':rows[0]['score']='999'
        elif attack=='precise_score':rows[0]['score']='3.0000000000000000000000001'
        elif attack=='priority':rows[0]['priority']='low'
        elif attack=='window':rows[0]['window_id']='bad'
        elif attack=='duplicate':rows.append(dict(rows[0]))
        elif attack=='noncandidate':rows[0]['candidate_id']='missing'
        elif attack=='utc':rows[0]['end_utc']='2000-01-01T00:02:00.0Z'
        else:rows[1]['station_id']='1'
    mutate_csv(s/'initial_plan.csv',mutate);sha=freeze(s);calls=[]
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:calls.append(1))
    with pytest.raises(ValueError):produce(s,sha,120,'baseline',tmp_path/'o',lambda:100)
    assert calls==[] and not (tmp_path/'o').exists()

@pytest.mark.parametrize('attack',['wrong_sha','missing_member','unknown_member','duplicate_json','escape','absolute','duplicate_column',
 'version','model_bool','nan','infinity','overflow','bad_json','candidate_score','candidate_duplicate','candidate_status'])
def test_source_rejections_zero_solver(tmp_path,monkeypatch,attack):
    s=tmp_path/'s';sha=fixture(s);man=json.loads((s/'source_manifest.json').read_text())
    if attack=='wrong_sha':sha='0'*64
    elif attack=='missing_member':(s/'model.json').unlink()
    elif attack in ('unknown_member','escape','absolute'):
        if attack=='unknown_member':man['members']['unexpected']={}
        else:man['members']['candidates.csv']['path']='../outside.csv' if attack=='escape' else 'C:/outside.csv'
        write_json(s/'source_manifest.json',man);sha=fp(s/'source_manifest.json')['sha256']
    elif attack=='duplicate_json':
        (s/'source_manifest.json').write_text('{"kind":1,"kind":2}',encoding='utf-8');sha=fp(s/'source_manifest.json')['sha256']
    elif attack=='duplicate_column':
        p=s/'candidates.csv';v=p.read_text();p.write_text(v.replace('task_id,candidate_id','task_id,task_id',1),encoding='utf-8');sha=freeze(s)
    elif attack=='version':
        man['data_version']='wrong';write_json(s/'source_manifest.json',man);sha=fp(s/'source_manifest.json')['sha256']
    elif attack=='model_bool':
        model=json.loads((s/'model.json').read_text());model['schema_version']=True;write_json(s/'model.json',model);sha=freeze(s)
    elif attack in ('nan','infinity','overflow','bad_json'):
        token={'nan':'NaN','infinity':'Infinity','overflow':'1e999','bad_json':'{'}[attack]
        (s/'model.json').write_text('{"x":'+token+'}',encoding='utf-8')
        man['members']['model.json'].update(fp(s/'model.json'));write_json(s/'source_manifest.json',man);sha=fp(s/'source_manifest.json')['sha256']
    else:
        def bad(rows):
            if attack=='candidate_score':rows[0]['score']='100'
            elif attack=='candidate_duplicate':rows.append(dict(rows[0]))
            else:rows[0]['status']='new_success'
        mutate_csv(s/'candidates.csv',bad);sha=freeze(s)
    calls=[];monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:calls.append(1))
    with pytest.raises((ValueError,FileNotFoundError)):produce(s,sha,120,'baseline',tmp_path/'o',lambda:100)
    assert calls==[] and not (tmp_path/'o').exists()

@pytest.mark.parametrize('attack',['changed_source','prepared_ticket','future_rehashed','pass_text'])
def test_ticket_trust_boundary(tmp_path,attack):
    s=tmp_path/'s';sha=fixture(s);p=proposal(s,['a','b','c'],120)
    if attack=='prepared_ticket':
        with pytest.raises(TypeError):choose_from_files(s,sha,120,p,ticket={'PASS':True})
    elif attack=='pass_text':
        with pytest.raises(TypeError):choose_from_files(s,sha,120,p,validation_receipt='PASS')
    elif attack=='changed_source':
        mutate_csv(s/'candidates.csv',lambda rows:rows.pop());freeze(s)
        with pytest.raises(ValueError):choose_from_files(s,sha,120,p)
    else:
        # Recompute the old-style unkeyed hash: it is neither accepted nor used by the native entry.
        fake=dict(p[2],candidate_id='not-in-pool',task_id='not-in-pool')
        ticket=dict(future=[fake],retained=p);ticket['validated_payload']=hashlib.sha256(json.dumps(ticket,sort_keys=True).encode()).hexdigest()
        with pytest.raises(ValueError,match='original candidate pool'):choose_from_files(s,sha,120,p[:2]+ticket['future'])

@pytest.mark.parametrize('attack',['status','locked_delete','locked_truncate','past','noncandidate','points','switch29'])
def test_native_history_rejects_physical_and_status_attacks(tmp_path,attack):
    s=tmp_path/'s';sha=fixture(s)
    rows=proposal(s,['a','b','c'],120)
    if attack=='status':rows[0]['status']='new_success'
    elif attack=='locked_delete':rows.pop(1)
    elif attack=='locked_truncate':rows[1].update(end_s=120,duration_s=60,end_utc='2000-01-01T00:02:00Z')
    elif attack=='past':rows[2].update(start_s=119,end_s=359,start_utc='2000-01-01T00:01:59Z',end_utc='2000-01-01T00:05:59Z')
    elif attack=='noncandidate':rows[2]['candidate_id']='bad'
    elif attack=='points':rows[2].update(points_int=999,score=199.8)
    else:
        s2=tmp_path/'s2';sha=fixture(s2,[('a',1,'high',0,120,1),('b',2,'low',149,120,1)],['a'])
        s=s2;rows=proposal(s,['a','b'],60)
    submitted=tmp_path/'submitted.csv';write_csv(submitted,rows,FIELDS)
    with pytest.raises(ValueError):evaluate(s,sha,60 if attack=='switch29' else 120,submitted,tmp_path/'eval')

@pytest.mark.parametrize('attack',['selected','retained','raw_proposal','source','branch'])
@pytest.mark.parametrize('reseal',[False,True])
def test_recheck_tamper_business_even_when_rehashed(tmp_path,monkeypatch,attack,reseal):
    s=tmp_path/'s';sha=fixture(s);run=tmp_path/'run'
    assert produce(s,sha,120,'improved',run,lambda:100)==0
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:pytest.fail('recheck solver invocation'))
    if attack in ('selected','retained','raw_proposal'):
        mutate_csv(run/(attack+'.csv'),lambda rows:rows[0].update(status='new_success'))
    elif attack=='source':
        mutate_csv(run/'source/candidates.csv',lambda rows:rows[0].update(points_int='999',score='199.8'))
    else:
        p=run/'branch_receipt.json';v=json.loads(p.read_text());v['branch']='KEEP_RETAINED_TIE';write_json(p,v)
    if reseal:seal_run(run)
    with pytest.raises(ValueError):recheck(run,sha,tmp_path/'rc')

@pytest.mark.parametrize('destination',['same','inside','parent','existing','case_alias'])
def test_output_prewrite_protection(tmp_path,destination):
    source=tmp_path/'Source';sha=fixture(source)
    out={'same':source,'inside':source/'child','parent':tmp_path,'existing':tmp_path/'old','case_alias':Path(str(source).upper())}[destination]
    if destination=='existing':out.mkdir();(out/'keep.txt').write_text('keep')
    before=tree_snapshot(tmp_path)
    with pytest.raises(ValueError):produce(source,sha,120,'baseline',out,lambda:100)
    assert tree_snapshot(tmp_path)==before

@pytest.mark.parametrize('error',[BudgetExpired('original budget'),RuntimeError('original unknown')])
def test_solver_exception_never_retained_success(tmp_path,monkeypatch,error):
    source=tmp_path/'s';sha=fixture(source);calls=[]
    def fail(*a,**k):calls.append(1);raise error
    monkeypatch.setattr(scheduler,'schedule',fail)
    with pytest.raises(type(error),match=str(error)):produce(source,sha,120,'baseline',tmp_path/'o',lambda:100)
    assert calls==[1] and not (tmp_path/'o/selected.csv').exists()
    receipt=json.loads((tmp_path/'o/solver_execution.json').read_text())
    assert receipt['status']==('BLOCKED' if isinstance(error,BudgetExpired) else 'FAIL')

@pytest.mark.parametrize('raw,timed,expected',[(0,False,0),(1,False,2),(7,False,2),(2,False,2),(3,False,3),(0,True,3)])
def test_original_return_code_mapping(raw,timed,expected):
    assert mapped_code(raw,timed)==expected

def test_deep_inputs_and_output_no_alias(tmp_path,monkeypatch):
    s=tmp_path/'s';sha=fixture(s);before=tree_snapshot(s);p=proposal(s,['a','b','c'],120);saved=copy.deepcopy(p)
    selected,_=choose_from_files(s,sha,120,p);selected[0]['priority']='low'
    assert p==saved and tree_snapshot(s)==before

def test_manifest_link_alias_rejected(tmp_path):
    s=tmp_path/'s';fixture(s);outside=tmp_path/'other';outside.mkdir()
    # A real Windows junction is used; no privilege-dependent symlink skip.
    link=tmp_path/'junction'
    completed=subprocess.run(['cmd','/c','mklink','/J',str(link),str(s)],capture_output=True)
    assert completed.returncode==0
    with pytest.raises(ValueError,match='links/reparse'):
        load_source(link,fp(s/'source_manifest.json')['sha256'])


def test_invalid_proposal_is_failed_not_retained(tmp_path,monkeypatch):
    source=tmp_path/'s';sha=fixture(source)
    bad=proposal(source,['a','b','c'],120);bad[2].update(points_int=999,score=199.8)
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:copy.deepcopy(bad))
    with pytest.raises(ValueError):produce(source,sha,120,'baseline',tmp_path/'run',lambda:100)
    assert (tmp_path/'run/raw_proposal.csv').exists() and not (tmp_path/'run/selected.csv').exists()

def test_full_solver_block_or_no_call(tmp_path,monkeypatch):
    source=tmp_path/'s';sha=fixture(source);calls=[]
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:calls.append(1))
    with pytest.raises(BudgetExpired):produce(source,sha,120,'baseline',tmp_path/'run',lambda:31)
    assert calls==[] and not (tmp_path/'run').exists()

@pytest.mark.parametrize('path_field',['input_dir','cache_dir','reuse_source','frozen_sample','fault_event_path'])
def test_model_rejects_executable_paths(tmp_path,path_field):
    source=tmp_path/'s';fixture(source)
    model=json.loads((source/'model.json').read_text());model[path_field]='external'
    write_json(source/'model.json',model);sha=freeze(source)
    with pytest.raises(ValueError,match='model contract'):load_source(source,sha)

def test_native_runner_recheck_dispatch(tmp_path,monkeypatch):
    from planner.runner import recheck as dispatch
    source=tmp_path/'s';sha=fixture(source);run=tmp_path/'run'
    produce(source,sha,120,'baseline',run,lambda:100)
    monkeypatch.setattr(scheduler,'schedule',lambda *a,**k:pytest.fail('dispatch called scheduler'))
    assert dispatch(run,tmp_path/'rc',None,source_sha256=sha)==0
    with pytest.raises((ValueError,AttributeError)):dispatch(run,tmp_path/'no_trust',None)

def test_unknown_ticket_future_higher_score_rehash(tmp_path):
    source=tmp_path/'s';sha=fixture(source)
    rows=proposal(source,['a','b','c','d'],0)
    rows[-1]['candidate_id']='unlisted-high-value';rows[-1]['task_id']='unlisted-high-value'
    ticket={'future':rows,'PASS':True}
    ticket['payload_sha256']=hashlib.sha256(json.dumps(ticket,sort_keys=True).encode()).hexdigest()
    assert sum(r['points_int'] for r in rows)>34
    with pytest.raises(ValueError,match='original candidate pool'):choose_from_files(source,sha,0,ticket['future'])
