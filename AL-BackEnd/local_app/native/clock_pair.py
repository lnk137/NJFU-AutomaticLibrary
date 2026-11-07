"""Bounded wall/continuous-clock observations for one native batch."""
from dataclasses import dataclass
import math

from local_app.models import AppError


MAX_PAIR_WIDTH_NS=1_000_000
MAX_PAIR_ATTEMPTS=3
MIN_RETRY_REMAINING_NS=250_000_000
CLOCK_LIMIT_NS=250_000_000


def _precision_ns(*values):
    # Account for wall timestamps represented as Python floats as well as the
    # final conversion from seconds to nanoseconds.
    return 1+sum(max(1,math.ceil(math.ulp(float(value))*1e9)) for value in values)


def _invalid():
    return AppError('native_clock_sample_invalid','时钟采样无法确认，未登记预约',503)


def wall_deadline_ns(deadline_wall):
    if type(deadline_wall) not in (int,float) or not math.isfinite(deadline_wall):raise _invalid()
    numerator,denominator=float(deadline_wall).as_integer_ratio()
    value=-(-(numerator*1_000_000_000)//denominator)
    if not 0<value<2**64:raise _invalid()
    return value


@dataclass(frozen=True)
class ClockPair:
    wall:float
    before_ns:int
    after_ns:int
    attempts:int

    @property
    def width_ns(self):return self.after_ns-self.before_ns

    @property
    def midpoint_ns(self):return (self.before_ns+self.after_ns)//2

    @property
    def error_bound_ns(self):return (self.width_ns+1)//2+_precision_ns(self.wall)

    def deadline_ns(self,deadline_wall):
        if type(deadline_wall) not in (int,float) or not math.isfinite(deadline_wall):raise _invalid()
        remaining_ns=math.ceil((deadline_wall-self.wall)*1e9)
        # The wall sample could have occurred anywhere inside the observed
        # interval. Its upper bound avoids advancing the local wall deadline.
        due=self.after_ns+remaining_ns+_precision_ns(self.wall,deadline_wall)
        if not 0<=due<2**64:raise _invalid()
        return due

    def wall_at(self,monotonic_ns):
        return self.wall+(monotonic_ns-self.midpoint_ns)/1e9

    def offset_change_bounds(self,other):
        wall_delta=(other.wall-self.wall)*1e9
        precision=_precision_ns(self.wall,other.wall)
        lower=math.floor(wall_delta)-(other.after_ns-self.before_ns)-precision
        upper=math.ceil(wall_delta)-(other.before_ns-self.after_ns)+precision
        return lower,upper

    def require_consistent(self,anchor_wall,anchor_ns):
        if type(anchor_wall) not in (int,float) or not math.isfinite(anchor_wall):raise _invalid()
        delta=(self.wall-anchor_wall)*1e9-(self.midpoint_ns-anchor_ns)
        error=self.error_bound_ns+_precision_ns(anchor_wall)
        if abs(delta)+error>=CLOCK_LIMIT_NS:
            raise AppError('native_clock_changed','时钟变化或采样范围超出允许值，未登记预约',503)

    def diagnostics(self):
        return {'clock_pair_wall':self.wall,'clock_pair_midpoint_ns':self.midpoint_ns,
                'clock_pair_width_ns':self.width_ns,'clock_pair_attempts':self.attempts,
                'clock_pair_error_bound_ns':self.error_bound_ns}


def sample_clock_pair(wall_clock,monotonic_ns,*,deadline_wall=None):
    """Retry interrupted samples, then retain the latest conservative bound."""
    last_after=None
    for attempt in range(1,MAX_PAIR_ATTEMPTS+1):
        try:
            before=monotonic_ns()
            wall=wall_clock()
            after=monotonic_ns()
        except Exception as exc:
            raise _invalid() from exc
        if (type(before) is not int or type(after) is not int or before<0 or after<before
                or (last_after is not None and before<last_after)
                or type(wall) not in (int,float) or not math.isfinite(wall)):
            raise _invalid()
        pair=ClockPair(float(wall),before,after,attempt)
        if pair.width_ns<=MAX_PAIR_WIDTH_NS:return pair
        if deadline_wall is not None:
            if type(deadline_wall) not in (int,float) or not math.isfinite(deadline_wall):raise _invalid()
            # Worst case: the wall was sampled at `before`. Do not launch
            # another optional sample when little real preparation time may remain.
            remaining=math.floor((deadline_wall-pair.wall)*1e9)-pair.width_ns-_precision_ns(pair.wall,deadline_wall)
            if remaining<=MIN_RETRY_REMAINING_NS:return pair
        last_after=after
    # A long interval is not silently treated as precise. The upper bound
    # preserves the existing ability to run if the wall deadline is still safe.
    return pair
