"""Private native process/protocol; no test imports or process-wide mutation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import subprocess
import threading
import time
import uuid

from ..elapsed_clock import elapsed_time
from ..models import AppError
from .build import private_directory


def clock_ns():return int(elapsed_time()*1_000_000_000)


class ProtocolError(RuntimeError):pass


@dataclass(frozen=True)
class Endpoint:
    origin:str
    ca:Path|None=None
    fixture_mode:bool=False
    @classmethod
    def production(cls):return cls('https://libseat.njfu.edu.cn')
    @classmethod
    def fixture(cls,port,ca):
        if type(port) is not int or not 1024<=port<=65535 or port in {5003,27019}:
            raise ValueError('Unsafe fixture port')
        return cls('https://127.0.0.1:'+str(port),Path(ca),True)
    def arguments(self):
        if not self.fixture_mode:
            if self.origin!='https://libseat.njfu.edu.cn' or self.ca is not None:raise ValueError('Fixed school endpoint required')
            return ['school','443','-']
        if not re.fullmatch(r'https://127\.0\.0\.1:[0-9]+',self.origin) or not self.ca:
            raise ValueError('Explicit loopback fixture required')
        port=int(self.origin.rsplit(':',1)[1])
        if not 1024<=port<=65535 or port in {5003,27019}:raise ValueError('Unsafe fixture port')
        if self.ca.is_symlink() or not self.ca.is_file():raise ValueError('Fixture CA file required')
        return ['fixture',str(port),str(self.ca)]


class Channel:
    def __init__(self,path,role):
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.socket.settimeout(8)
        try:self.socket.connect(str(path));self.reader=self.socket.makefile('rb');self.send(role+'\n')
        except Exception:self.socket.close();raise
    def send(self,text,body=b''):self.socket.sendall(text.encode()+body)
    def read(self):
        data=self.reader.readline(32769)
        if not data or len(data)>32768 or not data.endswith(b'\n'):raise ProtocolError('Native channel incomplete')
        text=data.decode().rstrip('\n')
        if text.startswith('ERROR '):raise ProtocolError(text[6:])
        return text
    def close(self):
        try:self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.reader.close();self.socket.close()


class Worker(Channel):
    def __init__(self,path,index):
        super().__init__(path,'WORKER '+str(index));self.io=threading.RLock()
        if not self.read().startswith('READY '):raise ProtocolError('Worker not ready')
        before=clock_ns();self.send('CLOCK\n');observed=int(self.read());after=clock_ns()
        if not before<=observed<=after:raise ProtocolError('Native clock mismatch')
    def warm(self,budget_ms=1500):
        if type(budget_ms) is not int or not 50<=budget_ms<=3500:raise ValueError('Bounded warm budget required')
        with self.io:
            self.send('WARM '+str(budget_ms)+'\n');fields=self.read().split()
            if len(fields)!=3 or fields[0]!='WARMED':raise ProtocolError('TLS warmup unconfirmed')
            return {'warmed_ns':int(fields[1]),'new_connections':int(fields[2])}
    def arm_wait(self,job):
        with self.io:
            self.send('WAIT '+job['id']+'\n')
            if self.read()!='WAITING':raise ProtocolError('Worker ARM unconfirmed')
    def result(self):
        with self.io:
            fields=self.read().split()
            if not fields or fields[0]!='DONE':raise ProtocolError('Native result incomplete')
            names=('curl_code','http_status','wake_ns','checked_ns','dispatch_ns','network_done_ns','journal_done_ns',
                   'new_connections','claim_finished_ns','journal_ok','final_guard_sql','checked_account_version','checked_global_version','length')
            if len(fields)!=len(names)+1:raise ProtocolError('Native result shape invalid')
            result=dict(zip(names,map(int,fields[1:]),strict=True));length=result.pop('length')
            if not 0<=length<=32768:raise ProtocolError('Native result exceeds bound')
            body=self.reader.read(length)
            if len(body)!=length:raise ProtocolError('Native body incomplete')
            result['body']=body.decode(errors='replace')
            metrics=self.read()
            if not metrics.startswith('METRICS '):raise ProtocolError('Native timing incomplete')
            values=json.loads(metrics[8:])
            if not isinstance(values,dict) or not all(type(value) is int and value>=0 for value in values.values()):
                raise ProtocolError('Native timing invalid')
            result['metrics']=values;return result


def make_state(key,version,enabled=True,plan=True,credential=1):
    canonical=f'{key}|{version}|{int(enabled)}|{int(plan)}|{credential}'
    return {'version':version,'enabled':enabled,'plan':plan,'credential':credential,
            'fingerprint':hashlib.sha256(canonical.encode()).hexdigest()}


class NativeProcess:
    def __init__(self,runtime,binary,*,endpoint=None,count=20,initialize=None,verified_binary_sha256=None,launcher=None):
        if type(count) is not int or not 1<=count<=24:raise ValueError('Native capacity must be1..24')
        self.endpoint=endpoint or Endpoint.production();self.endpoint.arguments()
        self.runtime=private_directory(runtime);self.binary=Path(binary)
        if self.binary.is_symlink() or not self.binary.is_file() or not os.access(self.binary,os.X_OK):raise ValueError('Verified native binary required')
        observed=hashlib.sha256(self.binary.read_bytes()).hexdigest()
        if verified_binary_sha256 is not None and (not re.fullmatch('[0-9a-f]{64}',verified_binary_sha256) or verified_binary_sha256!=observed):
            raise ValueError('Native binary identity mismatch')
        if not self.endpoint.fixture_mode and verified_binary_sha256 is None:
            raise ValueError('Verified native release required')
        self.binary_sha256=verified_binary_sha256 or observed
        self.verified_release=verified_binary_sha256 is not None and not self.endpoint.fixture_mode
        self.launcher=launcher
        self.state=self.runtime/'ledger';existing=self.state.exists()
        if initialize is True and existing:raise ValueError('Refuse ledger reset')
        if initialize is False and not existing:raise ValueError('Missing ledger')
        self.initialize=not existing if initialize is None else initialize
        private_directory(self.state)
        self.count=count;self.lock=threading.RLock();self.control=None;self.workers=[];self.process=None
        self.applied_states={};self._socket_identity=None
        try:self._spawn()
        except Exception:self.close();raise
    def _spawn(self):
        if not self.initialize:
            try:
                for name in ('identity','state.db'):
                    path=self.state/name;info=path.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                            or info.st_mode&0o077 or info.st_nlink!=1):raise ValueError()
                identity=(self.state/'identity').read_text()
                with (self.state/'state.db').open('rb') as stream:header=stream.read(16)
                if not re.fullmatch('[0-9a-f]{64}',identity) or header!=b'SQLite format 3\0':raise ValueError()
            except (OSError,ValueError):
                raise AppError('native_state_invalid','执行记录校验失败，已暂停；请先核对安装与记录',503) from None
        self.path=self.runtime/('n'+uuid.uuid4().hex[:8]+'.sock')
        if len(os.fsencode(self.path))>=104:raise ValueError('Native socket path too long')
        command=[str(self.binary),str(self.path),*self.endpoint.arguments(),str(self.state),str(self.count),'init' if self.initialize else 'open']
        environment={key:value for key,value in os.environ.items() if not key.startswith('NJFU_NATIVE_')}
        from .interactive import ManagedPopen,validate
        mode=os.environ.get('NJFU_EXECUTOR_MODE','interactive')
        # Existing local fixture callers need no launchd session; production
        # always uses the verified Interactive path and may not fall back.
        default_fixture=(self.endpoint.fixture_mode and 'NJFU_EXECUTOR_MODE' not in os.environ
                         and not environment.get('NJFU_TEST_ISOLATION_ROOT'))
        if self.launcher is not None:
            from ..dual_node.native_launcher import LinuxVerifiedLauncher
            if type(self.launcher) is not LinuxVerifiedLauncher or self.endpoint.fixture_mode:
                raise ValueError('Explicit verified Linux production launcher required')
            self.process=self.launcher(command,environment,self.runtime,
                expected_sha256=self.binary_sha256,allow_school=self.verified_release)
        elif default_fixture:
            self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment)
        elif mode=='interactive':
            self.process=ManagedPopen(command,environment,self.runtime,expected_sha256=self.binary_sha256,allow_school=self.verified_release)
        elif mode=='direct' and self.endpoint.fixture_mode and environment.get('NJFU_TEST_ISOLATION_ROOT'):
            validate(command,self.runtime,self.binary_sha256,environment,root=Path(environment['NJFU_TEST_ISOLATION_ROOT']),allow_school=False)
            self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment)
        else:raise ValueError('Native launch mode denied')
        if not select.select([self.process.stdout],[],[],8)[0]:raise ProtocolError('Native startup timed out')
        self.banner=self.process.stdout.readline(8192).strip()
        if not self.banner.startswith('READY '):
            detail=self.process.stderr.read(2048) if self.process.poll() is not None else ''
            if any(word in detail for word in ('ledger','identity','state fingerprint','private state')):
                raise AppError('native_state_invalid','执行记录校验失败，已暂停；请先核对安装与记录',503)
            raise ProtocolError('Native startup failed; state retained')
        info=self.path.lstat();self._socket_identity=(info.st_dev,info.st_ino)
        self.reconnect()
        with ThreadPoolExecutor(max_workers=self.count) as pool:
            self.workers=list(pool.map(lambda index:Worker(self.path,index),range(self.count)))
        config=self.config()
        if config.get('wire_protocol')!='campus-v1' or config.get('endpoint_mode')!=('fixture' if self.endpoint.fixture_mode else 'school'):
            raise ProtocolError('Native endpoint/protocol mismatch')
        self.pilot_wall_supported=config.get('pilot_wall_v1') is True
    def reconnect(self):
        self.applied_states={}
        if self.control:self.control.close()
        deadline=time.monotonic()+2
        while True:
            channel=Channel(self.path,'CONTROL')
            try:fields=channel.read().split();break
            except ProtocolError:
                channel.close()
                if time.monotonic()>deadline:raise
                time.sleep(.01)
        if len(fields)!=5 or fields[:2]!=['HELLO','1'] or not re.fullmatch('[0-9a-f]{64}',fields[2]):
            channel.close();raise ProtocolError('Native handshake invalid')
        self.identity,self.epoch,self.session=fields[2],int(fields[3]),int(fields[4]);self.control=channel
    def rpc(self,command,body=b''):
        if '\n' in command or len(command)>4096:raise ValueError('Invalid control command')
        with self.lock:
            head,_,tail=command.partition(' ')
            self.control.send(f'{head} {self.epoch} {self.session}'+(' '+tail if tail else '')+'\n',body)
            return self.control.read()
    def config(self):
        reply=self.rpc('CONFIG')
        if not reply.startswith('CONFIG '):raise ProtocolError('Native config unconfirmed')
        return json.loads(reply[7:])
    def lease(self,ttl=500):
        channel=Channel(self.path,f'RENEW {self.epoch} {self.session} {ttl}')
        try:
            if channel.read()!='ACK LEASE':raise ProtocolError('Native lease unconfirmed')
        finally:channel.close()
    def state_update(self,key,state):
        frame=f"STATE {key} {state['version']} {int(state['enabled'])} {int(state['plan'])} {state['credential']} {state['fingerprint']}"
        with self.lock:
            if self.rpc(frame)!=f"ACK STATE {key} {state['version']} {state['fingerprint']}":raise ProtocolError('Native state unconfirmed')
            self.applied_states={**self.applied_states,key:dict(state)}
    def arm(self,jobs,mode='D'):
        if mode!='D':raise ValueError('Durable due-time claims required')
        body=bytearray()
        for job in jobs:
            packet=job['body']
            fields=(job['id'],job['worker'],job['account'],job['day'],job.get('kind','S'),job.get('operation','-'),
                job['account_version'],job['global_version'],job['credential'],job['due_ns'],job['expires_ns'],
                job['created_ns'],hashlib.sha256(packet).hexdigest(),len(packet))
            line=' '.join(map(str,fields))
            if 'pilot_wall_due_ns' in job:
                wall=job['pilot_wall_due_ns']
                if not getattr(self,'pilot_wall_supported',False) or type(wall) is not int or not 0<wall<2**64:
                    raise ProtocolError('Pilot wall deadline unsupported or invalid')
                line+=' PILOT1 '+str(wall)
            body.extend((line+'\n').encode());body.extend(packet)
        started=clock_ns();reply=self.rpc('ARM '+str(len(jobs))+' D',bytes(body));finished=clock_ns()
        if not reply.startswith('ACK ARM '):raise ProtocolError('Native ARM unconfirmed')
        return {'prepare_ms':(finished-started)/1e6,'ack_ns':finished}
    def job_status(self,key):
        if not re.fullmatch('[a-zA-Z0-9_-]{1,128}',key):raise ValueError('Invalid job identifier')
        # Readback must survive loss/revocation of the controller. It grants no
        # lease, state mutation, ARM or send permission.
        channel=Channel(self.path,'READBACK '+key)
        try:return channel.read()
        finally:channel.close()
    def restart(self):
        if self.process and self.process.poll() is None:raise ProtocolError('Existing sender still owns state')
        self.close();self.initialize=False;self._spawn()
    def close_control(self):
        if self.control:self.control.close();self.control=None
    def close(self):
        managed=self.process
        try:
            quit_sent=False
            for worker in self.workers:
                try:worker.close()
                except OSError:pass
            self.workers=[]
            if self.control:
                if self.process and self.process.poll() is None:
                    try:self.rpc('QUIT');quit_sent=True
                    except (OSError,ProtocolError):pass
                self.control.close();self.control=None
            if self.process:
                if self.process.poll() is None:
                    if not quit_sent:self.process.terminate()
                    try:self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.terminate()
                        try:self.process.wait(timeout=3)
                        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
                self.stderr=self.process.stderr.read();self.process.stdout.close();self.process.stderr.close()
                self.process=None
            if getattr(self,'path',None) and self.path.exists() and self._socket_identity:
                info=self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev,info.st_ino)==self._socket_identity:self.path.unlink()
        finally:
            if managed and hasattr(managed,'cleanup'):managed.cleanup()
