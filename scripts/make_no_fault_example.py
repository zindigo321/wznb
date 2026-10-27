"""Assemble the flat public synthetic example into one no_fault_source v1 directory."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from planner.no_fault import checked_output
from planner.no_fault_source import file_identity,load_source,MEMBERS
from planner.atomic_io import atomic_text_writer,atomic_write_text

def make(out):
    templates=ROOT/'examples/synthetic'
    out=checked_output(out,[templates]);out.mkdir(parents=True,exist_ok=False)
    for name in MEMBERS:
        filename='no_fault_'+Path(name).name
        dst=out/name
        with atomic_text_writer(dst,newline='') as stream:
            stream.write((templates/filename).read_bytes().decode('utf-8'))
    model=json.loads((out/'model.json').read_text(encoding='utf-8'))
    meta=json.loads((out/'standard_input/metadata.json').read_text(encoding='utf-8'))
    manifest=dict(schema_version=1,kind='no_fault_source',data_version=meta['data_version'],model=model,
       origin=dict(source_id='public-synthetic-example',execution_version='candidate_snapshot'),
       members={n:dict(path=n,**file_identity(out/n)) for n in MEMBERS})
    atomic_write_text(out/'source_manifest.json',json.dumps(manifest,indent=2))
    expected=file_identity(out/'source_manifest.json')['sha256']
    load_source(out,expected)
    print(json.dumps(dict(source=str(out),source_sha256=expected,synthetic=True)))
    return expected

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True)
    a=p.parse_args()
    try:make(Path(a.out))
    except Exception as error:
        print(str(error),file=sys.stderr);raise SystemExit(3 if isinstance(error,(FileNotFoundError,PermissionError)) else 2)
