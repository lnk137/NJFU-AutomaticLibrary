"""Elapsed seconds including system suspend, independent of wall-clock changes."""
import sys
import time


def _system_clock():
    if sys.platform == 'darwin':
        import ctypes
        class Timebase(ctypes.Structure):
            _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]
        library = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        library.mach_continuous_time.restype = ctypes.c_uint64
        library.mach_continuous_time.argtypes = []
        library.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        library.mach_timebase_info.restype = ctypes.c_int
        base = Timebase()
        if library.mach_timebase_info(ctypes.byref(base)) or not base.denom:
            raise RuntimeError('Cannot initialize the sleep-inclusive system clock')
        scale = base.numer / base.denom / 1_000_000_000
        return lambda: library.mach_continuous_time() * scale, 'mach_continuous_time'
    if hasattr(time, 'CLOCK_BOOTTIME'):
        return lambda: time.clock_gettime(time.CLOCK_BOOTTIME), 'CLOCK_BOOTTIME'
    return time.monotonic, 'monotonic (platform fallback)'


elapsed_time, CLOCK_SOURCE = _system_clock()
