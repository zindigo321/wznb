import json,os,sys,time,subprocess,shutil,ctypes
from pathlib import Path
import pytest
from no_fault_fixtures import fixture,fp,mutate_csv
from planner.process_guard import run_guarded
from planner.path_safety import tree_snapshot
from planner.no_fault_cli import mapped_code
ROOT=Path(__file__).resolve().parents[1]

def cli(args,timeout=15,cwd=ROOT):
    return subprocess.run([sys.executable,'-B','-X','utf8',str(ROOT/'scripts/team3_run.py'),*map(str,args)],
                          cwd=cwd,capture_output=True,text=True,encoding='utf-8',timeout=timeout)

@pytest.mark.parametrize('mode',['no-fault','evaluate-no-fault','recheck'])
def test_help(mode):
    r=cli([mode,'--help'])
    assert r.returncode==0 and '--source-sha256' in r.stdout

@pytest.mark.parametrize('option',['missing_hash','missing_decision','both','decimal','bool','negative','fault','nan_budget'])
def test_cli_required_and_conflicting(tmp_path,option):
    s=tmp_path/'s';sha=fixture(s);out=tmp_path/'out'
    args=['--budget-seconds','70','no-fault','--source',s,'--source-sha256',sha,'--decision-s','120','--method','baseline','--out',out]
    if option=='missing_hash':
        j=args.index('--source-sha256');del args[j:j+2]
    elif option=='missing_decision':
        j=args.index('--decision-s');del args[j:j+2]
    elif option=='both':args[args.index('--method')+1]='both'
    elif option in ('decimal','bool','negative'):
        args[args.index('--decision-s')+1]={'decimal':'120.0','bool':'true','negative':'-1'}[option]
    elif option=='fault':args+=['--event','fault.json']
    else:args[1]='NaN'
    before=tree_snapshot(s);r=cli(args)
    assert r.returncode==2 and not out.exists() and tree_snapshot(s)==before

def test_native_formal_workflow_and_independent_commands(tmp_path):
    source=tmp_path/'s';sha=fixture(source);out=tmp_path/'workflow'
    r=cli(['--budget-seconds',70,'no-fault','--source',source,'--source-sha256',sha,'--decision-s',120,'--method','improved','--out',out])
    assert r.returncode==0,(r.stdout,r.stderr)
    w=json.loads((out/'workflow.json').read_text());assert len(w['commands'])==3 and w['solver_calls']==1 and w['original_raw_return_codes']==[0,0,0]
    ev=json.loads((out/'evaluation/evaluation.json').read_text())
    assert ev['evaluation_scope']=='native_no_fault_supplied_windows' and ev['history_counts']['locked']==1 and ev['solver_calls']==0
    before=tree_snapshot(out/'run')
    r=cli(['evaluate-no-fault','--source',out/'run/source','--source-sha256',sha,'--decision-s',120,'--tasks',out/'run/selected.csv','--out',tmp_path/'eval'])
    assert r.returncode==0
    r=cli(['recheck','--source',out/'run','--source-sha256',sha,'--out',tmp_path/'check'])
    assert r.returncode==0
    assert tree_snapshot(out/'run')==before
    for path in [tmp_path/'eval/workflow.json',tmp_path/'check/workflow.json']:
        assert json.loads(path.read_text())['solver_calls']==0
    # Status-only mutation is physically identical but must fail native evaluation.
    altered=tmp_path/'altered.csv';shutil.copyfile(out/'run/selected.csv',altered)
    mutate_csv(altered,lambda rows:rows[0].update(status='new_success'))
    r=cli(['evaluate-no-fault','--source',source,'--source-sha256',sha,'--decision-s',120,'--tasks',altered,'--out',tmp_path/'bad'])
    assert r.returncode==2

@pytest.mark.parametrize('kind',['missing','wrong_hash','existing'])
def test_formal_failure_codes(tmp_path,kind):
    source=tmp_path/'s';sha=fixture(source);out=tmp_path/'out'
    if kind=='missing':source=tmp_path/'absent'
    if kind=='wrong_hash':sha='0'*64
    if kind=='existing':out.mkdir();(out/'keep').write_text('untouched')
    r=cli(['no-fault','--source',source,'--source-sha256',sha,'--decision-s',120,'--method','baseline','--out',out])
    assert r.returncode==(3 if kind=='missing' else 2)
    if kind=='existing':assert (out/'keep').read_text()=='untouched' and set(q.name for q in out.iterdir())=={'keep'}

@pytest.mark.parametrize('code',[1,7])
def test_actual_child_return_code_preserved(tmp_path,code):
    result=run_guarded([sys.executable,'-B','-c',f'raise SystemExit({code})'],timeout=3,log_path=tmp_path/'child.log')
    assert result['exit_code']==code and not result['timed_out'] and mapped_code(result['exit_code'])==2

def test_actual_process_tree_timeout(tmp_path):
    child=tmp_path/'child.py';heartbeat=tmp_path/'heartbeat';ids=tmp_path/'pids.json'
    child.write_text("import time,sys\nfrom pathlib import Path\np=Path(sys.argv[1])\nend=time.monotonic()+8\nwhile time.monotonic()<end:\n p.write_text(str(time.monotonic()))\n time.sleep(.04)\n",encoding='utf-8')
    parent=tmp_path/'parent.py'
    parent.write_text("import subprocess,sys,time,json,os\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-B',sys.argv[1],sys.argv[2]],creationflags=subprocess.CREATE_NO_WINDOW)\nPath(sys.argv[3]).write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(10)\n",encoding='utf-8')
    result=run_guarded([sys.executable,'-B',str(parent),str(child),str(heartbeat),str(ids)],timeout=1,log_path=tmp_path/'log')
    assert result['exit_code']==3 and result['timed_out']
    before=heartbeat.read_bytes();time.sleep(.15);assert heartbeat.read_bytes()==before
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];api.OpenProcess.restype=wintypes.HANDLE
    api.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    for pid in json.loads(ids.read_text()):
        h=api.OpenProcess(0x1000,False,pid)
        if h:
            code=wintypes.DWORD();assert api.GetExitCodeProcess(h,ctypes.byref(code));api.CloseHandle(h);assert code.value!=259
