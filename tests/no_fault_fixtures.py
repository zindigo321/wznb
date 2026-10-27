"""Synthetic-only source fixtures; no TLE, real identifiers, machine paths or external state."""
import csv,json,hashlib,copy
from datetime import datetime,timezone,timedelta
from pathlib import Path
FIELDS=['task_id','candidate_id','window_id','satnum','station_id','priority','start_s','end_s','duration_s','points_int','score','start_utc','end_utc','status']
MEMBERS=['standard_input/targets.csv','standard_input/stations.csv','standard_input/windows.csv','standard_input/metadata.json','candidates.csv','initial_plan.csv','model.json']
def write_json(p,v):
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,indent=2),encoding='utf-8')
def write_csv(p,rows,fields):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
def fp(p):
    data=p.read_bytes();return dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
def freeze(root):
    model=json.loads((root/'model.json').read_text(encoding='utf-8'))
    meta=json.loads((root/'standard_input/metadata.json').read_text(encoding='utf-8'))
    man=dict(schema_version=1,kind='no_fault_source',data_version=meta['data_version'],model=model,
      origin=dict(source_id='public-synthetic-fixture',execution_version='candidate-snapshot'),
      members={n:dict(path=n,**fp(root/n)) for n in MEMBERS})
    write_json(root/'source_manifest.json',man)
    return fp(root/'source_manifest.json')['sha256']
def fixture(root,definitions=None,initial_ids=None):
    # (candidate name, target, priority, start, duration, station)
    if definitions is None:
        definitions=[('a',1,'high',0,120,1),('b',2,'medium',60,120,2),('c',3,'low',120,240,3),('d',4,'high',400,240,1)]
    if initial_ids is None:initial_ids=['a','b','c']
    day=datetime(2000,1,1,tzinfo=timezone.utc)
    utc=lambda s:(day+timedelta(seconds=s)).strftime('%Y-%m-%dT%H:%M:%SZ')
    targets={n:p for _,n,p,_,_,_ in definitions} or {1:'high'}
    rows=[];wins=[]
    for name,n,p,a,d,sid in definitions:
        pts={'high':5,'medium':3,'low':2}[p]*(3 if d==120 else 5)
        rows.append(dict(task_id=name,candidate_id=name,window_id='w'+name,satnum=n,station_id=sid,priority=p,
          start_s=a,end_s=a+d,duration_s=d,points_int=pts,score=pts/5,start_utc=utc(a),end_utc=utc(a+d),status='success'))
        wins.append(dict(window_id='w'+name,target_id=n,station_id=sid,start_utc=utc(0),end_utc=utc(3600)))
    root.mkdir(parents=True,exist_ok=False)
    write_csv(root/'standard_input/targets.csv',[dict(target_id=n,priority=p,full_score={'high':5,'medium':3,'low':2}[p]) for n,p in targets.items()],['target_id','priority','full_score'])
    write_csv(root/'standard_input/stations.csv',[dict(station_id=i) for i in [1,2,3]],['station_id'])
    write_csv(root/'standard_input/windows.csv',wins,['window_id','target_id','station_id','start_utc','end_utc'])
    write_json(root/'standard_input/metadata.json',dict(schema_version=1,time_precision_s=1,data_version='synthetic-no-fault-v1',filters_applied=['synthetic windows; no physical orbit claim'],horizon_start_utc=utc(0),horizon_end_utc=utc(3600)))
    model=dict(schema_version=1,kind='no_fault_model',durations_seconds=[120,240],weights=dict(high=5,medium=3,low=2),
      switch_seconds=30,horizon_start_utc=utc(0),horizon_end_utc=utc(3600),horizon_seconds=3600,station_ids=[1,2,3],elevation_degrees=[10,85])
    write_json(root/'model.json',model)
    write_csv(root/'candidates.csv',rows,FIELDS)
    write_csv(root/'initial_plan.csv',[dict(target_id=r['satnum'],**r) for r in rows if r['task_id'] in initial_ids],['target_id']+FIELDS)
    return freeze(root)
def mutate_csv(path,fn):
    with path.open(encoding='utf-8',newline='') as f:
        rd=csv.DictReader(f);fields=rd.fieldnames;rows=list(rd)
    fn(rows);write_csv(path,rows,fields)
