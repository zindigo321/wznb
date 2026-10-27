"""Bounded subprocess tree; Windows Job Object closes all descendant processes."""
import ctypes
import os
import signal
import subprocess
import time


class WindowsJob:
    def __init__(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),
                        ('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),
                        ('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(k,ctypes.c_uint64) for k in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),
                        ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
        self.api.CreateJobObjectW.restype=w.HANDLE
        self.api.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.api.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        self.api.CloseHandle.argtypes=[w.HANDLE]
        self.handle=self.api.CreateJobObjectW(None,None)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        info=Extended(); info.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def attach_and_resume(self, process):
        if not self.api.AssignProcessToJobObject(self.handle,int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        resume=ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes=[ctypes.c_void_p]; resume.restype=ctypes.c_long
        if resume(int(process._handle)) != 0: raise RuntimeError('NtResumeProcess failed')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle=None


def run_guarded(command, *, timeout, log_path, cwd=None, deadline_monotonic=None):
    started=time.perf_counter()
    job=WindowsJob() if os.name=='nt' else None
    process=None
    timed_out=False
    try:
        with open(log_path,'wb') as log:
            process=subprocess.Popen(command,cwd=cwd,stdout=log,stderr=subprocess.STDOUT,
                creationflags=0x4 | subprocess.CREATE_NO_WINDOW if job else 0,
                start_new_session=not bool(job))
            if job: job.attach_and_resume(process)
            try:
                wait_seconds=timeout if deadline_monotonic is None else max(0,min(timeout,deadline_monotonic-time.monotonic()))
                code=process.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                timed_out=True
                if job: job.close()
                else: os.killpg(process.pid,signal.SIGKILL)
                process.wait(timeout=10)
                code=3
        return dict(exit_code=code,timed_out=timed_out,seconds=time.perf_counter()-started,
                    tree_guard='Windows_Job_Object' if job else 'POSIX_process_group')
    finally:
        if job: job.close()
        if process is not None and process.poll() is None:
            if not job: os.killpg(process.pid,signal.SIGKILL)
            else: process.kill()
            process.wait(timeout=10)
