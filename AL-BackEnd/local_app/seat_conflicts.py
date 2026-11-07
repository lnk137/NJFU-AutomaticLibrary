"""Bounded, advisory conflicts between validated future plans; no side effects."""
from collections import defaultdict
from datetime import timedelta

from .models import ZONE, resolve_runs

HORIZON_DAYS = 7
MAX_CONFLICTS = 50


def detect_seat_conflicts(plans, catalog, now):
    """Consume authorized, enabled plan snapshots prepared by the dashboard."""
    now = now.astimezone(ZONE)
    buckets = defaultdict(list)
    for account in plans:
        for delta in range(HORIZON_DAYS):
            release = now.date() + timedelta(days=delta)
            runs = resolve_runs(account['account_id'], account['revision'], account['plan'], release,
                               date_exceptions=account['date_exceptions'])
            for run in runs:
                if run.due_at <= now.timestamp():
                    continue
                for rank, seat_id in enumerate(run.seat_ids, 1):
                    person = {key: account[key] for key in ('account_id', 'student_id', 'name')}
                    person.update(begin=run.begin, end=run.end, rank=rank)
                    buckets[(run.target_date, seat_id)].append(person)
    ordered = [(key, sorted(people, key=lambda a: (a['begin'], a['end'], a['student_id'])))
               for key, people in sorted(buckets.items()) if len(people) > 1]
    items = []
    result = {'start_date': (now.date() + timedelta(days=1)).isoformat(),
              'end_date': (now.date() + timedelta(days=HORIZON_DAYS)).isoformat(),
              'checked_accounts': len(plans), 'items': items, 'has_more': False}
    # Primary overlaps remain visible even if a large backup bucket occurs earlier.
    for primary in (True, False):
        for (target_date, seat_id), saved_people in ordered:
            people = [person for person in saved_people if person['rank'] == 1] if primary else saved_people
            for index, first in enumerate(people):
                for other in range(index + 1, len(people)):
                    second = people[other]
                    if second['begin'] >= first['end']:
                        break
                    if (first['account_id'] == second['account_id']
                            or (first['rank'] == second['rank'] == 1) != primary):
                        continue
                    if len(items) == MAX_CONFLICTS:
                        result['has_more'] = True
                        return result
                    seat = catalog[seat_id]
                    items.append({'target_date': target_date,
                        'seat': {'id': seat_id, 'name': seat.name, 'location': seat.location},
                        'begin': max(first['begin'], second['begin']), 'end': min(first['end'], second['end']),
                        'kind': 'preferred' if primary else 'backup', 'accounts': [first.copy(), second.copy()]})
    return result
