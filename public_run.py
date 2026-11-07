#!/usr/bin/env python3
"""Keep the HTTPS relay connected; booking deadlines remain entirely local."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parent
PUBLIC=ROOT/'.local-data/public'
sys.path.insert(0,str(ROOT/'AL-BackEnd'))
from local_app.elapsed_clock import elapsed_time
from local_app.power import PowerFiles, quiet


def ssh_command(config):
    if set(config)!={'host','port','user','key','known_hosts','remote_port'}:
        raise ValueError('Invalid connection config')
    if (not isinstance(config['host'],str) or not config['host'] or config['host'].startswith('-')
            or config['user']!='njfu-relay' or type(config['port']) is not int or config['port']!=23486
            or config['remote_port']!=15003):raise ValueError('Invalid relay endpoint')
    for name in ('key','known_hosts'):
        path=Path(config[name])
        if path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError('Relay files must be private regular files')
    return ['/usr/bin/ssh','-N','-T','-p',str(config['port']),'-i',config['key'],
        '-o','ControlMaster=no','-o','ControlPath=none','-o','IdentitiesOnly=yes','-o','BatchMode=yes',
        '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+config['known_hosts'],
        '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
        '-o','ConnectTimeout=10','-R','127.0.0.1:15003:127.0.0.1:5003','-R','127.0.0.1:15004:127.0.0.1:5027',config['user']+'@'+config['host']]


def stop(child):
    if child and child.poll() is None:
        child.terminate()
        child.wait()


class AwakeGuard:
    """A dead/stale supervisor never removes the runner's independent protection."""
    def __init__(self,managed,*,files=None,spawn=subprocess.Popen):
        self.managed=managed
        self.files=files or PowerFiles(ROOT/'.local-data/power')
        self.spawn,self.child=spawn,None
        self.last_write=-float('inf')

    def step(self,wall=None,elapsed=None):
        if sys.platform!='darwin':return
        wall=time.time() if wall is None else wall
        elapsed=elapsed_time() if elapsed is None else elapsed
        delegated=False
        if self.managed:
            try:delegated=self.files.delegated(elapsed,wall)
            except Exception:pass
        if self.child is not None and self.child.poll() is not None:self.child=None
        if delegated and self.child is not None:
            stop(self.child);self.child=None
        elif not delegated and self.child is None:
            self.child=self.spawn(['/usr/bin/caffeinate','-is','-w',str(os.getpid())])
        if self.managed and not quiet(wall) and not quiet(time.time()) and elapsed-self.last_write>=5:
            try:
                self.files.write('runner.json',{'pid':os.getpid(),'at':wall,'elapsed':elapsed,
                                               'managed':True,'hold':self.child is not None})
                self.last_write=elapsed
            except Exception:pass

    def close(self):
        stop(self.child);self.child=None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--enable-live-booking',action='store_true')
    parser.add_argument('--open-browser',action='store_true')
    parser.add_argument('--power-managed',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    config_path=PUBLIC/'config.json'
    config=json.loads((PUBLIC/'connection.json').read_text())
    ssh=ssh_command(config)
    origin=json.loads(config_path.read_text())['origin']
    with (PUBLIC/'launcher.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            with urlopen('http://127.0.0.1:5003/health',timeout=2) as response:health=json.load(response)
            if health.get('allow_live')!=args.enable_live_booking:raise RuntimeError('已运行服务的预约模式不同，请先停止原服务')
            print('公网连接已经运行：'+origin);return
        backend=tunnel=certificate_proxy=None
        awake=AwakeGuard(args.power_managed)
        def interrupted(*_):raise KeyboardInterrupt
        for sig in (signal.SIGTERM,signal.SIGHUP):signal.signal(sig,interrupted)
        try:
            command=[sys.executable,'-B',str(ROOT/'local_run.py'),'--public-config',str(config_path)]
            if args.enable_live_booking:command.append('--enable-live-booking')
            backend=subprocess.Popen(command,cwd=ROOT)
            deadline=time.monotonic()+35
            while True:
                if backend.poll() is not None:raise RuntimeError('本机服务未能启动，请查看日志并停止原运行实例')
                try:
                    with urlopen('http://127.0.0.1:5003/health',timeout=1) as response:health=json.load(response)
                    if health.get('public_enabled') and health.get('allow_live')==args.enable_live_booking:break
                except OSError:pass
                if time.monotonic()>deadline:raise RuntimeError('本机服务启动超时')
                time.sleep(.2)
            awake.step()
            if args.open_browser:
                import webbrowser
                webbrowser.open('http://127.0.0.1:5003/')
            print('公网入口：'+origin+'；root管理：http://127.0.0.1:5003/；保持此进程运行。',flush=True)
            from deploy.cert_proxy import CertificateProxy
            certificate_proxy=CertificateProxy();certificate_proxy.start()
            delay=1
            with (ROOT/'.local-data/logs/public-tunnel.log').open('ab') as log:
                while backend.poll() is None:
                    started=time.monotonic();tunnel=subprocess.Popen(ssh,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                    while tunnel.poll() is None:
                        if backend.poll() is not None:raise RuntimeError('本机后台退出，已停止公网连接')
                        awake.step()
                        time.sleep(.5)
                    if time.monotonic()-started>60:delay=1
                    print('公网通道断开，'+str(delay)+'秒后重连；本机定时计划继续运行。',flush=True)
                    retry_at=time.monotonic()+delay
                    while time.monotonic()<retry_at:
                        awake.step();time.sleep(.5)
                    delay=min(delay*2,15)
                raise RuntimeError('本机后台退出')
        except KeyboardInterrupt:print('正在停止公网连接和本机服务，账号与历史保留。',flush=True)
        finally:
            for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,signal.SIG_IGN)
            stop(tunnel);stop(backend);awake.close()
            if certificate_proxy:certificate_proxy.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:print(str(exc),file=sys.stderr);sys.exit(1)
