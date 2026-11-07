"""Short-lived same-boot evidence from the existing HTTPS Date health check.

HTTP Date is a coarse server assertion, not a precision UTC certificate.  The
bounded local pair only constrains subsequent changes to the same wall clock.
"""
import ctypes
from datetime import timedelta
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import http.client
import json
import math
from pathlib import Path
import sys
import time
import uuid

import requests

from .clock_pair import CLOCK_LIMIT_NS,MAX_PAIR_WIDTH_NS,ClockPair,sample_clock_pair
from .protocol import clock_ns


SOURCE='school_public_https_date'
MAX_AGE_NS=120_000_000_000
URL='https://libseat.njfu.edu.cn/ic-web/sysConfig/public'


def boot_identifier():
    """Read the kernel's current boot UUID, never infer it from wall time."""
    if sys.platform.startswith('linux'):
        path = Path('/proc/sys/kernel/random/boot_id')
        try:
            with path.open('r', encoding='ascii') as stream:
                value = stream.read(129)
            if len(value) > 128:
                raise ValueError('boot identity oversized')
            return str(uuid.UUID(value.strip()))
        except (OSError, UnicodeError, ValueError):
            raise ValueError('boot identity unavailable') from None
    if sys.platform!='darwin':raise ValueError('unsupported boot identity')
    library=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    call=library.sysctlbyname
    call.argtypes=[ctypes.c_char_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t]
    call.restype=ctypes.c_int
    length=ctypes.c_size_t()
    if call(b'kern.bootsessionuuid',None,ctypes.byref(length),None,0) or not 1<length.value<=128:
        raise ValueError('boot identity unavailable')
    value=ctypes.create_string_buffer(length.value)
    if call(b'kern.bootsessionuuid',value,ctypes.byref(length),None,0):raise ValueError('boot identity unavailable')
    return str(uuid.UUID(value.value.decode('ascii')))


def _encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _digest(value):return hashlib.sha256(_encoded(value).encode()).hexdigest()


def sample_verified_reference(*,wall_clock=time.time,continuous_ns=clock_ns,boot_clock=None,permit=None,on_request_started=None):
    """Read the fixed public TLS endpoint, returning evidence or None."""
    session=response=None;original_endheaders=None
    try:
        boot_clock=boot_clock or boot_identifier
        first_boot=boot_clock()
        before=sample_clock_pair(wall_clock,continuous_ns)
        if before.width_ns>MAX_PAIR_WIDTH_NS:return None
        session=requests.Session();session.trust_env=False
        if permit is not None and not permit():return None
        if on_request_started is not None:
            original_endheaders=http.client.HTTPConnection.endheaders
            def started(connection,*args,**kwargs):
                result=original_endheaders(connection,*args,**kwargs)
                on_request_started()
                return result
            http.client.HTTPConnection.endheaders=started
        response=session.get(URL,timeout=(.8,1.2),allow_redirects=False,
            headers={'Cache-Control':'no-cache, no-store','Pragma':'no-cache'},stream=True)
        after=sample_clock_pair(wall_clock,continuous_ns)
        if first_boot!=boot_clock() or after.before_ns<before.after_ns or after.width_ns>MAX_PAIR_WIDTH_NS:return None
        if after.after_ns-before.before_ns>2_500_000_000:return None
        lower,upper=before.offset_change_bounds(after)
        if lower<=-CLOCK_LIMIT_NS or upper>=CLOCK_LIMIT_NS:return None
        if response.status_code!=200 or response.history:return None
        age=response.headers.get('Age')
        if age is not None and (not isinstance(age,str) or not age.isdecimal() or int(age)!=0):return None
        value=response.headers.get('Date')
        if not isinstance(value,str):return None
        parsed=parsedate_to_datetime(value)
        if parsed.tzinfo is None or parsed.utcoffset()!=timedelta(0):return None
        observed=parsed.timestamp()
        if not math.isfinite(observed) or not before.wall-2.5<=observed<=after.wall+2.5:return None
        evidence={'schema':1,'source':SOURCE,'boot_id':first_boot,'date_seconds':int(observed),
            'pair':{'wall':after.wall,'before_ns':after.before_ns,'after_ns':after.after_ns},
            'expires_ns':after.before_ns+MAX_AGE_NS}
        evidence['digest']=_digest(evidence)
        return evidence
    except Exception:return None
    finally:
        if original_endheaders is not None:http.client.HTTPConnection.endheaders=original_endheaders
        if response is not None:response.close()
        if session is not None:session.close()


def validate_evidence(value,current,*,boot_clock=None):
    """Fail closed on corrupted, old, cross-boot or offset-shifted evidence."""
    try:
        boot_clock=boot_clock or boot_identifier
        if not isinstance(value,dict) or set(value)!={'schema','source','boot_id','date_seconds','pair','expires_ns','digest'}:return False
        data=dict(value);digest=data.pop('digest')
        if not isinstance(digest,str) or not hmac.compare_digest(digest,_digest(data)):return False
        if type(value['schema']) is not int or value['schema']!=1 or value['source']!=SOURCE:return False
        if not isinstance(value['boot_id'],str) or value['boot_id']!=boot_clock():return False
        if type(value['date_seconds']) is not int:return False
        fields=value['pair']
        if not isinstance(fields,dict) or set(fields)!={'wall','before_ns','after_ns'}:return False
        wall,before,after=fields['wall'],fields['before_ns'],fields['after_ns']
        if type(wall) not in (int,float) or not math.isfinite(wall):return False
        if type(before) is not int or type(after) is not int or not 0<=before<=after:return False
        if after-before>MAX_PAIR_WIDTH_NS or type(value['expires_ns']) is not int or value['expires_ns']!=before+MAX_AGE_NS:return False
        if not isinstance(current,ClockPair) or current.width_ns>MAX_PAIR_WIDTH_NS:return False
        if current.before_ns<after or current.after_ns>=value['expires_ns']:return False
        initial=ClockPair(float(wall),before,after,1)
        lower,upper=initial.offset_change_bounds(current)
        return -CLOCK_LIMIT_NS<lower<=upper<CLOCK_LIMIT_NS
    except Exception:return False
