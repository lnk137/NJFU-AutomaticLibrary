"""Optional bounded Mac assertion; no installation, wake change or school I/O."""
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import threading
import uuid


class MacAwakeGuard:
    LEAD=480
    TAIL=305
    MAX_FUTURE=48*3600

    def __init__(self, *, runner=subprocess.Popen, pid=None, wake_file=None):
        self.runner=runner
        self.pid=os.getpid() if pid is None else pid
        if type(self.pid) is not int or self.pid<1:raise ValueError('invalid_awake_pid')
        self.wake_file=Path(wake_file) if wake_file is not None else None
        self.process=None;self.closed=False;self.deadline=None;self.last_now=None
        self.lock=threading.RLock()

    @staticmethod
    def _time(value):
        try:
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=4102444800:
                raise ValueError()
            return float(value)
        except (ValueError,OverflowError):raise ValueError('invalid_awake_time') from None

    def _stop(self):
        process,self.process=self.process,None
        self.deadline=None
        if process is not None:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=2)

    def _publish_next(self, releases, now):
        if self.wake_file is None:return
        path=self.wake_file
        aliases={'/var','/tmp'} if os.uname().sysname=='Darwin' else set()
        if (not path.is_absolute() or path.is_symlink()
                or any(p.is_symlink() and str(p) not in aliases for p in path.parents)):
            raise ValueError('unsafe_wake_input_path')
        path=path.parent.resolve()/path.name
        info=path.parent.stat()
        if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('unsafe_wake_input_directory')
        if path.exists():
            info=path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                    or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
                raise ValueError('unsafe_wake_input_file')
        upcoming=[value for value in releases if now<value<=now+self.MAX_FUTURE]
        value={'schema':1,'generated_at':int(now),'release_at':int(min(upcoming)) if upcoming else 0}
        temporary=path.parent/('.dual-probe-next-'+uuid.uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(value,stream,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally:
            if temporary.exists():temporary.unlink()

    def update(self, tasks, now):
        with self.lock:
            if self.closed:raise ValueError('awake_guard_closed')
            try:
                now=self._time(now)
                if self.last_now is not None and now<self.last_now:raise ValueError('awake_clock_rollback')
                self.last_now=now
                if type(tasks) not in (list,tuple) or len(tasks)>256:raise ValueError('invalid_awake_tasks')
                releases=[]
                for task in tasks:
                    if type(task) is not dict:raise ValueError('invalid_awake_task')
                    releases.append(self._time(task.get('release_at')))
                self._publish_next(releases,now)
                ends=[release+self.TAIL for release in releases if release-self.LEAD<=now<release+self.TAIL]
                if not ends:
                    self._stop();return {'active':False,'status':'outside_window'}
                deadline=max(ends)
                if self.process is not None and self.process.poll() is not None:
                    self.process.wait(timeout=2);self.process=None
                if self.process is None or deadline>self.deadline+.5:
                    self._stop()
                    # -t is an independent upper bound if an RPC/update stalls;
                    # -w also releases the assertion when this node PID exits.
                    timeout=max(1,min(self.LEAD+self.TAIL,math.ceil(deadline-now)))
                    command=['/usr/bin/caffeinate','-is','-w',str(self.pid),'-t',str(timeout)]
                    self.process=self.runner(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,env={'PATH':'/usr/bin:/bin'},shell=False)
                    self.deadline=deadline
                    if self.process.poll() is not None:raise ValueError('awake_assertion_failed')
                return {'active':True,'status':'assertion_process_running','until':self.deadline}
            except Exception:
                self._stop()
                raise ValueError('awake_guard_unconfirmed') from None

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True;self._stop()
