"""Read-only Linux clock qualification for the server authority.

The reported uncertainty is the larger kernel NTP maximum/estimated error,
not a measured independent UTC accuracy or a value inferred from "yes".
The local NTP daemon must maintain those kernel fields. Missing synchronization,
an unsupported ABI, unavailable evidence or a slow observation fails closed.

ABI checked against the deployed x86_64 glibc sys/timex.h and bits/timex.h.
Units and read-only modes=0: https://man7.org/linux/man-pages/man2/adjtimex.2.html
"""
import ctypes
import math
import platform
import subprocess
import sys
import time


class _Timeval(ctypes.Structure):
    _fields_ = [('tv_sec', ctypes.c_long), ('tv_usec', ctypes.c_long)]


class _Timex(ctypes.Structure):
    _fields_ = [
        ('modes', ctypes.c_uint), ('offset', ctypes.c_long), ('freq', ctypes.c_long),
        ('maxerror', ctypes.c_long), ('esterror', ctypes.c_long), ('status', ctypes.c_int),
        ('constant', ctypes.c_long), ('precision', ctypes.c_long), ('tolerance', ctypes.c_long),
        ('time', _Timeval), ('tick', ctypes.c_long), ('ppsfreq', ctypes.c_long),
        ('jitter', ctypes.c_long), ('shift', ctypes.c_int), ('stabil', ctypes.c_long),
        ('jitcnt', ctypes.c_long), ('calcnt', ctypes.c_long), ('errcnt', ctypes.c_long),
        ('stbcnt', ctypes.c_long), ('tai', ctypes.c_int), ('_reserved', ctypes.c_int * 11),
    ]


def _supported_abi():
    return (sys.platform == 'linux' and platform.machine() == 'x86_64'
            and platform.libc_ver()[0] == 'glibc' and sys.byteorder == 'little'
            and ctypes.sizeof(ctypes.c_long) == ctypes.sizeof(ctypes.c_void_p) == 8
            and ctypes.sizeof(ctypes.c_int) == 4 and ctypes.sizeof(_Timex) == 208
            and (_Timex.maxerror.offset, _Timex.esterror.offset,
                 _Timex.status.offset, _Timex.tai.offset) == (24, 32, 40, 160))


def _read_kernel_clock():
    if not _supported_abi():
        raise ValueError('unsupported_clock_abi')
    library = ctypes.CDLL(None, use_errno=True)
    call = library.adjtimex
    call.argtypes = [ctypes.POINTER(_Timex)]
    call.restype = ctypes.c_int
    value = _Timex()  # All zero: modes=0 reads; no clock parameter is changed.
    state = call(ctypes.byref(value))
    if state < 0:
        raise OSError('kernel_clock_unavailable')
    return {'state':state, 'status':value.status,
            'maxerror_us':value.maxerror, 'esterror_us':value.esterror}


def _ntp_synchronized():
    try:
        result = subprocess.run(
            ['/usr/bin/timedatectl', 'show', '--property=NTPSynchronized', '--value'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=.15, check=True,
            close_fds=True, env={'PATH':'/usr/bin:/bin', 'LC_ALL':'C'})
        return result.stdout.strip() == b'yes'
    except (OSError, subprocess.SubprocessError):
        return False


def time_health():
    """Return the coordinator contract; never request an NTP packet or set time."""
    started = time.monotonic()
    synchronized, uncertainty = False, None
    try:
        ntp = _ntp_synchronized()
        kernel = _read_kernel_clock()
        maximum, estimate = kernel['maxerror_us'], kernel['esterror_us']
        if (type(maximum) is int and type(estimate) is int
                and maximum >= 0 and estimate >= 0):
            uncertainty = max(maximum, estimate)/1000
            # TIME_ERROR=5 and all unknown/error returns are refused. The other
            # documented states 0..4 include the kernel's leap-second states.
            synchronized = (ntp is True and type(kernel['state']) is int
                and kernel['state'] in (0, 1, 2, 3, 4) and type(kernel['status']) is int
                and kernel['status'] >= 0 and not kernel['status'] & (0x0040 | 0x1000)
                and uncertainty <= 250)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        synchronized = False
    elapsed = time.monotonic()-started
    checked_at = time.time()
    if (not 0 <= elapsed <= .2 or type(checked_at) not in (int, float)
            or not math.isfinite(checked_at)):
        synchronized = False
    return {'synchronized':synchronized, 'checked_at':checked_at, 'uncertainty_ms':uncertainty}
