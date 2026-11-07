"""Independent loopback TLS school with real crypto and school-shaped receipts."""
import base64
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import copy
import hashlib
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs,urlsplit,urlunsplit,urlencode

from Crypto.Cipher import AES,PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import unpad
import requests
from requests.cookies import extract_cookies_to_jar

from local_app.elapsed_clock import elapsed_time
from local_app.school import CampusClient
from support.cas_fixture import form,SALT


def clock_ns():return int(elapsed_time()*1_000_000_000)


class FixtureTransport(requests.Session):
    """Keep canonical request/cookie semantics; send only to this private fixture."""
    def __init__(self,fixture,student_id=None):
        super().__init__();self.fixture=fixture;self.student_id=student_id;self.trust_env=False
        self.verify=str(fixture.cert)

    def send(self,request,**kwargs):
        parsed=urlsplit(request.url)
        if parsed.scheme!='https' or parsed.hostname not in {'libseat.njfu.edu.cn','uia.njfu.edu.cn'}:
            raise AssertionError('fixture transport rejected non-school canonical URL')
        original=request.url
        local=copy.copy(request);local.headers=request.headers.copy()
        local.headers['Host']=parsed.hostname
        if self.student_id:local.headers['X-Fixture-Student']=self.student_id
        local.url=urlunsplit(('https',f'127.0.0.1:{self.fixture.port}',parsed.path,parsed.query,''))
        kwargs['verify']=str(self.fixture.cert);kwargs['allow_redirects']=False
        response=super().send(local,**kwargs)
        extract_cookies_to_jar(self.cookies,request,response.raw)
        response.url=original
        return response


class _Server(ThreadingHTTPServer):
    request_queue_size=256
    daemon_threads=True
    def handle_error(self,*args):pass  # expected TLS/drop faults contain no diagnostics


class CampusFixture:
    def __init__(self):
        self.stack=ExitStack();self.lock=threading.RLock()
        self.accounts={};self.states={};self.events=[];self.receipts=[];self.faults={};self.post_counts={}
        self.login_failures={};self.prelogin_failures={};self.public_failures={};self.key_delays={}
        self.new_login=False;self.auth_challenges=set();self.auth_posts={}
        self.request_controls=[];self.warm_count=0;self.warm_delays={}
        self.key=RSA.generate(2048)
        self.public=base64.b64encode(self.key.public_key().export_key(format='DER')).decode()
        self.login_key=RSA.generate(1024)
        modulus='00'+format(self.login_key.n,'x')
        self.new_login_script=('RSAUtils.getKeyPair("010001", "", "'+modulus+'");\n')*2
        self.new_login_css='.modal-overlay { display: none; }'
        self.new_login_key_digest=hashlib.sha256(modulus.lower().encode()).hexdigest()

    def new_form(self,state):
        return f'''<html><form id="fm1" method="post" action="/authserver/login?state={state}">
        <input id="username" name="username" type="text"><input id="passwordShow" type="password">
        <input id="password" name="password" type="hidden"><input name="execution" type="hidden" value="{state}">
        <input name="encrypted" type="hidden" value="true"><input name="_eventId" type="hidden" value="submit">
        <input name="loginType" type="hidden" value="fixture-type"></form>
        <script src="/authserver/themes/sudy_njfu/js/login.js"></script>
        <link rel="stylesheet" href="/authserver/themes/sudy_njfu/css/pc/index.css">
        <div id="modalOverlay" class="modal-overlay"><div id="captcha2"></div></div></html>'''

    def __enter__(self):
        try:
            self.directory=Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='njfu-campus-school-')))
            self.cert,self.private=self.directory/'cert.pem',self.directory/'key.pem'
            config=self.directory/'cert.cnf'
            config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=127.0.0.1\n[ext]\nsubjectAltName=IP:127.0.0.1,DNS:libseat.njfu.edu.cn,DNS:uia.njfu.edu.cn\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n')
            subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                '-config',str(config),'-keyout',str(self.private),'-out',str(self.cert)],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
            fixture=self
            class Handler(BaseHTTPRequestHandler):
                protocol_version='HTTP/1.1'
                def log_message(self,*args):pass
                def setup(self):
                    super().setup();self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                def parse_request(self):self.received_ns=clock_ns();return super().parse_request()
                def reply(self,data,status=200,headers=None,partial_length=0):
                    raw=data if isinstance(data,bytes) else json.dumps(data).encode()
                    self.send_response(status);self.send_header('Content-Length',str(len(raw)+partial_length))
                    self.send_header('Content-Type','application/json' if not isinstance(data,bytes) else 'text/html; charset=utf-8')
                    for key,value in (headers or {}).items():self.send_header(key,value)
                    self.end_headers()
                    try:
                        self.wfile.write(raw);self.wfile.flush()
                        if partial_length:
                            self.connection.shutdown(socket.SHUT_RDWR);self.close_connection=True
                    except (OSError,ssl.SSLError):pass
                def account(self,token=True):
                    cookies=SimpleCookie()
                    try:cookies.load(self.headers.get('Cookie',''))
                    except Exception:return None
                    value=cookies.get('ic-cookie');value=value.value if value else None
                    with fixture.lock:
                        return next((row for row in fixture.accounts.values() if row['cookie']==value
                                     and (not token or row['token']==self.headers.get('token'))),None)
                def do_GET(self):
                    parsed=urlsplit(self.path);query=parse_qs(parsed.query);path=parsed.path
                    if path=='/ic-web/auth/address':
                        student=self.headers.get('X-Fixture-Student')
                        with fixture.lock:
                            failure=fixture.prelogin_failures.get(student,0)
                            if failure:fixture.prelogin_failures[student]-=1
                        if failure:return self.reply({'code':-1},503)
                        state=secrets.token_hex(12);target='https://libseat.njfu.edu.cn/ic-web//auth/token?uuid='+state
                        with fixture.lock:fixture.states[state]=None
                        entry='https://libseat.njfu.edu.cn/authcenter/toLoginPage?'+urlencode({'redirectUrl':target,'extInfo':state})
                        return self.reply({'code':0,'data':entry})
                    if path=='/authcenter/toLoginPage':
                        state=query['extInfo'][0]
                        return self.reply(b'',302,{'Location':'https://uia.njfu.edu.cn/authserver/login?state='+state})
                    if path=='/authserver/login':
                        state=query.get('state',[''])[0]
                        if fixture.new_login:
                            page=fixture.new_form(state)
                            if self.headers.get('X-Fixture-Student') in fixture.auth_challenges:
                                page=page.replace('</form>','<input id="otpauthcode" name="otpauthcode"></form>',1)
                            return self.reply(page.encode())
                        return self.reply(form().replace('fixture-dynamic-execution',state).encode())
                    if path=='/authserver/themes/sudy_njfu/js/login.js' and fixture.new_login:
                        return self.reply(fixture.new_login_script.encode())
                    if path=='/authserver/themes/sudy_njfu/css/pc/index.css' and fixture.new_login:
                        return self.reply(fixture.new_login_css.encode())
                    if path=='/authserver/needCaptcha.html':return self.reply(b'false')
                    if path.startswith('/authcenter/doAuth/'):
                        state=path.rsplit('/',1)[1]
                        target='https://libseat.njfu.edu.cn/ic-web//auth/token?'+urlencode({'uuid':state,'uniToken':state,'extInfo':state})
                        return self.reply(('<script>window.location.href = "'+target+'";</script>').encode())
                    if path in {'/ic-web//auth/token','/ic-web/auth/token'}:
                        with fixture.lock:student=fixture.states.get(query.get('uuid',[''])[0]);row=fixture.accounts.get(student)
                        if not row:return self.reply({'code':-1},401)
                        return self.reply(b'',302,{'Location':'https://libseat.njfu.edu.cn/',
                            'Set-Cookie':'ic-cookie='+row['cookie']+'; Domain=libseat.njfu.edu.cn; Path=/; Secure; SameSite=Lax'})
                    if path=='/ic-web/auth/userInfo':
                        row=self.account(False)
                        if not row:return self.reply({'code':-1})
                        return self.reply({'code':0,'data':{'logonName':row['student_id'],'trueName':'Fixture',
                            'accNo':row['acc_no'],'token':row['token']}})
                    if path=='/ic-web/sysConfig/public':
                        student=self.headers.get('X-Fixture-Student')
                        with fixture.lock:
                            failure=fixture.public_failures.get(student,0)
                            if failure:fixture.public_failures[student]-=1
                            fixture.warm_count+=1;index=fixture.warm_count
                            fixture.events.append({'kind':'public_config','index':index,'received_ns':self.received_ns})
                        if failure:return self.reply({'code':-1},503)
                        if fixture.warm_delays.get(index):threading.Event().wait(fixture.warm_delays[index])
                        return self.reply({'code':0,'data':[{'sysKey':'resvCode','sysValue':'0'}]})
                    if path=='/ic-web/login/publicKey':
                        row=self.account();student=row['student_id'] if row else None
                        with fixture.lock:fixture.events.append({'kind':'key','student_id':student,'received_ns':self.received_ns})
                        if fixture.key_delays.get(student):threading.Event().wait(fixture.key_delays[student])
                        return self.reply({'code':0,'data':{'publicKey':fixture.public}})
                    if path=='/ic-web/reserve/resvInfo':
                        row=self.account()
                        if not row:return self.reply({'code':-1,'message':'fixture unauthenticated'})
                        with fixture.lock:
                            rows=[copy.deepcopy(value) for value in fixture.receipts if value.get('owner')==row['student_id']
                                  and query['beginDate'][0]<=value['resvBeginTime'][:10]<=query['endDate'][0]]
                            fixture.events.append({'kind':'query','student_id':row['student_id'],'received_ns':self.received_ns})
                        page=int(query.get('page',['1'])[0]);return self.reply({'code':0,'data':rows[(page-1)*50:page*50]})
                    return self.reply(b'<html>isolated campus fixture</html>')
                def do_POST(self):
                    parsed=urlsplit(self.path);length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=16384:return self.reply({'code':-1},400)
                    body=self.rfile.read(length)
                    if parsed.path=='/authserver/login':
                        fields=parse_qs(body.decode());student=fields.get('username',[''])[0]
                        with fixture.lock:
                            fixture.auth_posts[student]=fixture.auth_posts.get(student,0)+1
                            failure=fixture.login_failures.get(student,0)
                            if failure:fixture.login_failures[student]-=1
                        if failure:return self.reply({'code':-1},503)
                        try:
                            if fixture.new_login:
                                chunk_size=2*((fixture.login_key.n.bit_length()-1)//16)
                                value=pow(int(fields['password'][0],16),fixture.login_key.d,fixture.login_key.n)
                                plain=value.to_bytes(chunk_size,'little').rstrip(b'\0').decode('ascii')
                            else:
                                plain=unpad(AES.new(SALT.encode(),AES.MODE_CBC,bytes(16)).decrypt(base64.b64decode(fields['password'][0])),16)[64:].decode()
                            with fixture.lock:row=fixture.accounts.get(student)
                            if not row or plain!=row['password']:raise ValueError()
                            state=fields['execution'][0]
                            with fixture.lock:fixture.states[state]=student
                        except Exception:
                            state=fields.get('execution',[''])[0]
                            return self.reply((fixture.new_form(state) if fixture.new_login else form()).encode())
                        with fixture.lock:fixture.events.append({'kind':'login','student_id':student,'received_ns':self.received_ns})
                        return self.reply(b'',302,{'Location':'http://libseat.njfu.edu.cn/authcenter/doAuth/'+state+'?ticket=fixture-ticket'})
                    if parsed.path!='/ic-web/reserve/seat':
                        with fixture.lock:fixture.events.append({'kind':'wrong_post','path':parsed.path,'received_ns':self.received_ns})
                        return self.reply({'code':-1,'message':'unexpected fixture path'},404)
                    row=self.account()
                    if not row:return self.reply({'code':-1,'message':'fixture auth mismatch'},401)
                    try:
                        data=json.loads(body)
                        aes=PKCS1_v1_5.new(fixture.key).decrypt(base64.b64decode(data['encryptVlaue']),b'invalid')
                        seat=AES.new(aes,AES.MODE_CBC,b'ABCDEF1234123412').decrypt(bytes.fromhex(data['resvDevEncrypts'][0])).rstrip(b'\0').decode()
                        if (data['resvDev']!=[0] or data['appAccNo']!=row['acc_no'] or data['resvMember']!=[row['acc_no']]
                                or not seat.isdecimal() or data['resvBeginTime']>=data['resvEndTime']):raise ValueError()
                    except Exception:return self.reply({'code':-1,'message':'fixture packet mismatch'},400)
                    with fixture.lock:
                        index=fixture.post_counts.get(row['student_id'],0)+1;fixture.post_counts[row['student_id']]=index
                        fault=copy.deepcopy(fixture.faults.get((row['student_id'],index),{}))
                        event={'kind':'post','path':parsed.path,'student_id':row['student_id'],'seat_id':seat,
                            'received_ns':self.received_ns,'body_sha256':hashlib.sha256(body).hexdigest(),'index':index}
                        fixture.events.append(event)
                        accepted=not fault.get('busy') and not fault.get('missing_receipt') and not fault.get('reject')
                        if fault.get('drop') and not fault.get('record_on_drop'):accepted=False
                        if accepted or fault.get('record_on_busy'):
                            receipt={'owner':row['student_id'],'uuid':'receipt-'+secrets.token_hex(8),'logonName':row['student_id'],
                                'resvDevInfoList':[{'devId':seat}],'resvBeginTime':data['resvBeginTime'],
                                'resvEndTime':data['resvEndTime'],'resvStatus':1027}
                            receipt.update(fault.get('receipt_patch',{}));fixture.receipts.append(receipt)
                    if fault.get('delay_ms'):threading.Event().wait(fault['delay_ms']/1000)
                    if fault.get('drop'):
                        self.connection.shutdown(socket.SHUT_RDWR);self.close_connection=True;return
                    if fault.get('malformed'):return self.reply(b'not json')
                    if fault.get('redirect'):return self.reply(b'',302,{'Location':'https://uia.njfu.edu.cn/authserver/login'})
                    busy=fault.get('busy');code=fault.get('code',1 if busy or fault.get('reject') else 0)
                    message=fault.get('message','当前设备正在被预约，请稍后重试' if busy else 'fixture accepted')
                    return self.reply({'code':code,'message':message},fault.get('http',200),
                                      partial_length=fault.get('partial_length',0))
            self.server=_Server(('127.0.0.1',0),Handler)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(self.cert,self.private)
            self.server.socket=context.wrap_socket(self.server.socket,server_side=True)
            self.port=self.server.server_port
            self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
            self.thread.start();return self
        except BaseException:self.stack.close();raise

    def __exit__(self,*args):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
        self.stack.close()

    def register(self,student_id,acc_no=None,password='fixture-password'):
        with self.lock:
            self.accounts[student_id]={'student_id':student_id,'acc_no':acc_no or len(self.accounts)+1000,
                'password':password,'token':secrets.token_urlsafe(24),'cookie':secrets.token_urlsafe(24)}

    def client(self,student_id,password='fixture-password',**kwargs):
        return CampusClient(student_id,password,transport=FixtureTransport(self,student_id),**kwargs)

    def posts(self):
        with self.lock:return [dict(row) for row in self.events if row['kind']=='post']
