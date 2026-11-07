"""One bounded candidate order shared by warmup and actual submissions."""
MAX_BUSY_ATTEMPTS = 6
ATTEMPTS_PER_SEAT = 2
BUSY_RETRY_RESPONSE_COOLDOWN = 1.0
BUSY_REJECTION = (1, '当前设备正在被预约，请稍后重试')
OCCUPIED_REJECTION = (1, '设备在该时间段内已被预约')


def retryable_seat_rejection(code, message):
    """Only observed seat-specific rejections allow another candidate."""
    return type(code) is int and (code, message) in (BUSY_REJECTION, OCCUPIED_REJECTION)


def candidate_attempts(seat_ids):
    return remaining_candidate_attempts(seat_ids, ())


def remaining_candidate_attempts(seat_ids, attempts):
    """Keep priority order and actual-attempt limits; discard known occupied seats."""
    ordered = tuple(dict.fromkeys(seat_ids))
    counts = {seat: sum(row.get('seat_id') == seat for row in attempts) for seat in ordered}
    occupied = {row.get('seat_id') for row in attempts
                if type(row.get('code')) is int and (row.get('code'), row.get('message')) == OCCUPIED_REJECTION}
    remaining = tuple(seat for turn in range(ATTEMPTS_PER_SEAT) for seat in ordered
                      if seat not in occupied and counts[seat] <= turn)
    return remaining[:max(0, MAX_BUSY_ATTEMPTS-len(attempts))]


def busy_retry_spacing(attempt_count):
    """Fixed request-start floor for every busy retry; first attempts bypass it."""
    return 1.2


def clear_busy_failure(row):
    """Only ordered, sent and exhausted seat-specific rejections permit another interval."""
    if not isinstance(row, dict) or row.get('status') != 'failed':
        return False
    attempts = row.get('attempts') or []
    seats = (row.get('spec') or {}).get('seat_ids', ())
    if not isinstance(attempts, list) or not attempts or not seats:
        return False
    for index, attempt in enumerate(attempts):
        remaining = remaining_candidate_attempts(seats, attempts[:index])
        if (not isinstance(attempt, dict) or not remaining or attempt.get('seat_id') != remaining[0]
                or attempt.get('outcome') != 'rejected' or attempt.get('retryable') is not True
                or attempt.get('sent') is not True
                or not retryable_seat_rejection(attempt.get('code'), attempt.get('message'))):
            return False
    return not remaining_candidate_attempts(seats, attempts)
