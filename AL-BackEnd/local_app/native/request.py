"""Transfer one canonical prepared school request without sending or logging it."""
from dataclasses import dataclass
import copy
import hashlib
import json
import math

import requests

from local_app.booking_protocol import safe_message
from local_app.elapsed_clock import elapsed_time
from local_app.models import AppError,Submission
from local_app.school import CampusClient
from local_app.recovery_policy import retryable_seat_rejection

URL='https://libseat.njfu.edu.cn/ic-web/reserve/seat'
MAGIC=b'NJFU-CAMPUS/1\n'
HEADER_NAMES=frozenset({'user-agent','accept','content-type','cookie','token'})


def invalid_request():
    return AppError('native_request_invalid','预约请求未通过原生协议校验，未发送',502)


@dataclass(frozen=True,repr=False)
class PreparedRequestCapsule:
    body: bytes
    headers: tuple[tuple[str,str],...]
    created_ns: int
    expires_ns: int
    method: str='POST'
    url: str=URL

    def encode(self):
        frame=bytearray(MAGIC+f'{len(self.headers)} {len(self.body)}\n'.encode())
        for name,value in self.headers:
            if (name not in HEADER_NAMES or not value or len(value)>4096
                    or any(ord(char)<32 or ord(char)>126 for char in value)):
                raise invalid_request()
            raw_name,raw_value=name.encode('ascii'),value.encode('ascii')
            frame.extend(f'{len(raw_name)} {len(raw_value)}\n'.encode())
            frame.extend(raw_name);frame.extend(raw_value)
        frame.extend(self.body)
        if (self.method!='POST' or self.url!=URL or not 0<len(self.body)<=8192
                or len(frame)>16384 or {name for name,_ in self.headers}!=HEADER_NAMES
                or len(self.headers)!=len(HEADER_NAMES)):
            raise invalid_request()
        return bytes(frame)

    def metadata(self):
        return {'method':self.method,'url':self.url,'body_sha256':hashlib.sha256(self.body).hexdigest(),
                'capsule_sha256':hashlib.sha256(self.encode()).hexdigest(),
                'created_ns':self.created_ns,'expires_ns':self.expires_ns}


class _Capture:
    """The canonical client may serialize one reservation; no network method exists."""
    def __init__(self,source):
        self.session=requests.Session();self.session.trust_env=False
        self.session.headers.clear();self.session.headers.update(dict(source.headers))
        self.session.cookies=copy.deepcopy(source.cookies)
        self.headers=self.session.headers
        self.cookies=self.session.cookies
        self.prepared=None

    def request(self,method,url,**options):
        if method!='POST' or url!=URL or self.prepared is not None or 'json' not in options:
            raise invalid_request()
        self.prepared=self.session.prepare_request(requests.Request(method,url,
            headers=options.get('headers'),json=options['json']))
        response=requests.Response();response.status_code=200;response.url=url
        response._content=b'{"code":0}'  # capture acknowledgement, never a school result
        return response

    def close(self):self.session.close()


def prepared_session(client):
    """Fingerprint the actual target Cookie and this transport generation."""
    capture=None
    try:
        capture=_Capture(client.http)
        request=capture.session.prepare_request(requests.Request('POST',URL))
        values=(id(client.http),id(client.http.cookies),client.identity,client._token,
                request.headers.get('Cookie',''))
        return hashlib.sha256(repr(values).encode()).digest()
    except Exception:raise invalid_request() from None
    finally:
        if capture:capture.close()


def remember_preparation(client,before):
    if before!=prepared_session(client):
        client._prepared=[];client._native_prepared_session=None
        raise AppError('native_session_changed','预约会话已变化，请重新预热',409)
    client._native_prepared_session=(before,frozenset(id(fields) for _,_,fields in client._prepared))


def prepare_payload(client,run,seat_id):
    """Standalone fixture callers use the same original crypto with provenance."""
    before=prepared_session(client);client._native_prepared_session=None
    client.prepare_submission(run,seat_id)
    remember_preparation(client,before)


def capture_request(client,run,seat_id,*,clock=elapsed_time):
    if (client.identity is None or client.identity.student_id!=client.student_id
            or not isinstance(client._token,str) or not client._token or seat_id not in run.seat_ids):
        raise invalid_request()
    provenance=getattr(client,'_native_prepared_session',None)
    if not provenance or provenance[0]!=prepared_session(client):
        client._prepared=[];client._native_prepared_session=None
        raise AppError('native_session_changed','预约会话已变化，请重新预热',409)
    expires=client.prepared_until(run,seat_id)
    now=clock()
    if type(expires) not in (int,float) or not math.isfinite(expires) or expires<=now:
        raise AppError('native_payload_expired','预约参数尚未准备或已经过期，未发送',409)
    fields=client._take_prepared(run,seat_id)
    if fields is None or id(fields) not in provenance[1]:
        raise AppError('native_payload_expired','预约参数与当前会话不匹配，未发送',409)
    capture=_Capture(client.http)
    clone=None
    try:
        # Use the original implementation to construct every body field. The
        # synthetic transport cannot send; enabling this clone changes no source
        # client's switch or guard and grants no native send permission.
        clone=CampusClient(client.student_id,client.password,transport=capture,allow_live=True)
        capture.headers.clear();capture.headers.update(dict(client.http.headers))
        clone.identity=client.identity;clone._token=client._token
        clone._booking_ready=client._booking_ready
        clone._prepared=[(clone._submission_binding(run,seat_id),expires-2,fields)]
        result=clone.submit(run,seat_id)
        if capture.prepared is None or result.outcome!='accepted':raise invalid_request()
        prepared=capture.prepared
        headers=tuple(sorted((name.lower(),value) for name,value in prepared.headers.items()
                             if name.lower() in HEADER_NAMES))
        body=prepared.body.encode('utf-8') if isinstance(prepared.body,str) else prepared.body
        if not isinstance(body,bytes):raise invalid_request()
        expiry_ns=int(expires*1_000_000_000)
        capsule=PreparedRequestCapsule(body,headers,expiry_ns-2_000_000_000,expiry_ns)
        capsule.encode()
        if provenance[0]!=prepared_session(client):raise invalid_request()
        return capsule
    except AppError:
        raise
    except Exception:
        # requests.InvalidHeader can include the original secret value.
        raise invalid_request() from None
    finally:
        if clone:clone.close()
        else:capture.close()


def decode_result(status,body,timings,*,sent=True,journal_ok=True,transport_ok=True):
    # Only allowlisted numeric transport timings reach persistent diagnostics.
    allowed={'request_ms','request_started_at','request_finished_at','guard_ms','prepare_ms','timing_version'}
    safe={key:value for key,value in timings.items() if key in allowed and type(value) in (int,float) and math.isfinite(value)}
    if not journal_ok:
        return Submission('uncertain',message='原生发送状态未确认，禁止重交',sent=sent,timings=safe)
    if sent is False:
        return Submission('rejected',message='原生最终校验阻断，未发送预约',sent=False,timings=safe)
    if not transport_ok:
        return Submission('uncertain',message='首发传输未完成，禁止自动重交',sent=sent,timings=safe)
    try:
        if status!=200 or not isinstance(body,bytes) or not 0<len(body)<=32768:raise ValueError()
        result=json.loads(body)
        if not isinstance(result,dict) or type(result.get('code')) is not int:raise ValueError()
    except (ValueError,UnicodeError):
        return Submission('uncertain',message='学校提交结果未确认，请查询记录',sent=sent,timings=safe)
    if result['code']==0:
        return Submission('accepted',code=0,message='学校已接收，等待记录核实',sent=sent,timings=safe)
    return Submission('rejected',retryable_seat_rejection(result['code'],result.get('message')),
                      result['code'],safe_message(result.get('message')),sent=sent,timings=safe)
