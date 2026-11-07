"""Regular school rules read from official user help on 2026-10-19."""
import re

_RULES = {
    'version': '2026-10-19',
    'source_url': 'https://libseat.njfu.edu.cn/#/ic/userhelp',
    'booking_open': '07:00:00', 'day_offset': 1,
    'open': '07:30', 'close': '22:00', 'friday_close': '20:00',
    'min_minutes': 120, 'max_minutes': 900, 'sign_in_minutes': 30,
}


def school_policy():return dict(_RULES)


def daily_window(weekday):
    return _RULES['open'], _RULES['friday_close'] if weekday==5 else _RULES['close']


def closing_end(end, weekday):
    """The actual use date sets the upper bound; earlier choices stay intact."""
    return min(end, daily_window(weekday)[1])


def _minutes(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):
        raise ValueError('使用时间须为 HH:MM')
    hour,minute=map(int,value.split(':'));return hour*60+minute


def validate_window(begin,end,weekday=None):
    start,finish=_minutes(begin),_minutes(end)
    opening,closing=daily_window(weekday)
    if start<_minutes(opening) or finish>_minutes(closing):
        raise ValueError('周五座位开放时间为07:30–20:00' if weekday==5 else '座位开放时间为07:30–22:00')
    if not _RULES['min_minutes']<=finish-start<=_RULES['max_minutes']:
        raise ValueError('学校规定每次预约为2–15小时')
