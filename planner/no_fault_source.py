"""Strict, portable no_fault_source v1 loader. Expected manifest hash comes from caller."""
import hashlib,json,re,os
from pathlib import Path,PurePosixPath
from .io_contract import load_tables,parse_utc,read_complete_tasks,table,integer
from .no_fault_rules import require,validate_source
MEMBERS = ('standard_input/targets.csv','standard_input/stations.csv','standard_input/windows.csv',
           'standard_input/metadata.json','candidates.csv','initial_plan.csv','model.json')
MODEL_KEYS = {'schema_version','kind','durations_seconds','weights','switch_seconds','horizon_start_utc',
              'horizon_end_utc','horizon_seconds','station_ids','elevation_degrees'}

def strict_json(path):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result: raise ValueError('duplicate JSON key: '+k)
            result[k]=v
        return result
    def invalid(value): raise ValueError('nonfinite JSON number: '+value)
    def finite_float(value):
        import math
        result=float(value)
        if not math.isfinite(result): raise ValueError('nonfinite JSON number: '+value)
        return result
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=pairs,parse_constant=invalid,parse_float=finite_float)

def file_identity(path):
    data=Path(path).read_bytes()
    return dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())

def safe_relative(name):
    require(isinstance(name,str),'relative member name required')
    p=PurePosixPath(name)
    require(bool(name) and p.as_posix()==name and not p.is_absolute() and '\\' not in name and
            all(x not in ('.','..') and ':' not in x for x in p.parts),'unsafe relative member path')
    return p

def reject_links(path):
    path=Path(path).absolute()
    for p in (path,*path.parents):
        if p.exists() or p.is_symlink():
            st=p.lstat()
            require(not p.is_symlink() and not (getattr(st,'st_file_attributes',0)&0x400),
                    'links/reparse points are not supported: '+str(p))
    return path

def checked_members(root, expected, manifest_name, required=None):
    root=reject_links(root)
    manifest_path=reject_links(root/manifest_name)
    require(isinstance(expected,str) and re.fullmatch('[0-9a-fA-F]{64}',expected) is not None,
            'external expected source-manifest SHA256 required')
    require(file_identity(manifest_path)['sha256']==expected.lower(),'external source-manifest hash mismatch')
    manifest=strict_json(manifest_path)
    return root,manifest

def load_source(directory, expected_sha256):
    root,manifest=checked_members(directory,expected_sha256,'source_manifest.json')
    require(set(manifest)=={'schema_version','kind','data_version','model','members','origin'},'source manifest keys/schema')
    require(type(manifest['schema_version']) is int and manifest['schema_version']==1 and manifest['kind']=='no_fault_source','no_fault_source v1 required')
    require(isinstance(manifest['members'],dict) and set(manifest['members'])==set(MEMBERS),'missing/unknown source member')
    require(isinstance(manifest['origin'],dict) and set(manifest['origin'])=={'source_id','execution_version'} and
            all(isinstance(v,str) and v for v in manifest['origin'].values()),'source origin required')
    paths=[]
    for name,entry in manifest['members'].items():
        safe_relative(name)
        require(isinstance(entry,dict) and set(entry)=={'path','bytes','sha256'},'source member descriptor')
        safe_relative(entry['path']);require(entry['path']==name,'source member path must equal schema name')
        require(type(entry['bytes']) is int and entry['bytes']>=0 and isinstance(entry['sha256'],str) and
                re.fullmatch('[0-9a-f]{64}',entry['sha256']) is not None,'member bytes/hash')
        path=reject_links(root/name);require(path.resolve().is_relative_to(root.resolve()),'source member escapes root')
        require(file_identity(path)=={k:entry[k] for k in ('bytes','sha256')},'source member hash mismatch: '+name)
        paths.append(path)
    actual=[]
    for base,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:reject_links(Path(base)/name)
        actual.extend((Path(base)/name).relative_to(root).as_posix() for name in files)
    require(set(actual)==set(MEMBERS)|{'source_manifest.json'},'unknown source files')
    model=strict_json(root/'model.json');require(model==manifest['model'] and set(model)==MODEL_KEYS,'model contract mismatch')
    require(type(model['schema_version']) is int and model['schema_version']==1 and model['kind']=='no_fault_model','model schema')
    require(model['durations_seconds']==[120,240] and all(type(v) is int for v in model['durations_seconds']) and
            model['weights']=={'high':5,'medium':3,'low':2} and all(type(v) is int for v in model['weights'].values()) and
            type(model['switch_seconds']) is int and model['switch_seconds']==30 and model['elevation_degrees']==[10,85], 'unsupported model parameters')
    require(type(model['horizon_seconds']) is int and model['horizon_seconds']>0,'model horizon')
    require(isinstance(model['station_ids'],list) and model['station_ids'] and
            all(type(v) is int and v>0 for v in model['station_ids']) and len(set(model['station_ids']))==len(model['station_ids']),'model stations')
    meta=strict_json(root/'standard_input/metadata.json')
    require(meta['data_version']==manifest['data_version'] and isinstance(manifest['data_version'],str) and manifest['data_version'],'data version mismatch')
    day,end=parse_utc(model['horizon_start_utc']),parse_utc(model['horizon_end_utc'])
    require(meta['horizon_start_utc']==model['horizon_start_utc'] and meta['horizon_end_utc']==model['horizon_end_utc'] and
            (end-day).total_seconds()==model['horizon_seconds'],'frozen horizon mismatch')
    cfg=dict(start_utc=model['horizon_start_utc'],horizon_seconds=model['horizon_seconds'],station_ids=model['station_ids'])
    records,stations,windows,day,_=load_tables(root/'standard_input',cfg)
    all_stations={integer(r['station_id'],'station_id',1) for r in table(root/'standard_input/stations.csv',{'station_id'},'station_id')}
    require(all_stations==set(model['station_ids']),'model must cover exactly frozen stations')
    context=dict(day=day,horizon=model['horizon_seconds'],priorities={r['satnum']:r['priority'] for r in records},
                 stations=all_stations,windows={r['window_id']:r for r in windows})
    fields,raw,initial=read_complete_tasks(root/'initial_plan.csv')
    _,_,candidates=read_complete_tasks(root/'candidates.csv')
    validate_source(context,initial,candidates)
    # Re-read bytes after parsing so no changed member can pass a previously-read seal.
    for name,entry in manifest['members'].items():
        require(file_identity(root/name)=={k:entry[k] for k in ('bytes','sha256')},'source changed while loading')
    require(file_identity(root/'source_manifest.json')['sha256']==expected_sha256.lower(),'manifest changed while loading')
    return dict(root=root,manifest=manifest,model=model,context=context,initial=initial,candidates=candidates,
                initial_fields=fields,initial_raw=raw,source_sha256=expected_sha256.lower())
