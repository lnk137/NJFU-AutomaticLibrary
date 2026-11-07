"""Strict school-record intervals shared by manual previews and backup retries."""
from datetime import datetime

from .models import AppError


def _record_time(value):
    if not isinstance(value, str):
        raise ValueError('Invalid record time')
    parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    if parsed.strftime('%Y-%m-%d %H:%M:%S') != value:
        raise ValueError('Noncanonical record time')
    return parsed


def record_windows(records, student_id, active_statuses):
    """Reject partial, unknown or contradictory evidence instead of assuming absence."""
    if not isinstance(records, list):
        raise AppError('school_records_unverified', '学校记录查询不完整，未继续预约', 502)
    windows = []
    try:
        for record in records:
            if record.student_id != student_id:
                raise AppError('identity_mismatch', '学校查询身份异常，未继续预约', 502)
            if (type(record.status) is not int or record.status not in active_statuses
                    or not isinstance(record.id, str) or not record.id
                    or not isinstance(record.seat_id, str) or not record.seat_id):
                raise ValueError('Unverified record')
            begin, end = _record_time(record.begin), _record_time(record.end)
            if begin >= end or any(begin < finish and end > start for start, finish in windows):
                raise ValueError('Contradictory record intervals')
            windows.append((begin, end))
    except (AttributeError, TypeError, ValueError):
        raise AppError('school_records_unverified', '学校记录状态或时段无法确认，未继续预约', 502) from None
    return windows


def require_free_window(windows, day, begin, end):
    start, finish = _record_time(f'{day} {begin}:00'), _record_time(f'{day} {end}:00')
    if any(start < record_end and finish > record_begin for record_begin, record_end in windows):
        raise AppError('booking_overlap', '学校记录中已有重叠时段预约，请先在官方页面处理', 409)
