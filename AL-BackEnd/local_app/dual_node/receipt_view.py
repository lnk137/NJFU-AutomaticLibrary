"""Read-only views of source-bound mirrored outcomes, never SchoolRecord IDs.

This validates the bridge's durable provenance and cross-field consistency. It
does not turn a digest into a school receipt or grant execution authority.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from ..models import RunSpec, ZONE
from .contracts import (DualNodeError, NODES, canonical_hash, exact, identifier,
                        require, timestamp, task_identity, validate_task, result_diagnostics)
from .result_bridge import run_spec


def is_dual_row(row):
    if type(row) is not dict:
        return False
    operation = (row.get('spec') or {}).get('operation_id') if type(row.get('spec')) is dict else None
    return (any(key in row for key in ('dual_binding', 'dual_task', 'dual_progress', 'dual_result', 'dual_mirror'))
            or isinstance(operation, str) and operation.startswith('dual:')
            or isinstance(row.get('_id'), str) and ':dual:' in row['_id'])


@dataclass(frozen=True)
class DualReceiptView:
    phase: str
    status: str
    owner: str
    task_id: str
    grant_id: str
    spec: RunSpec
    attempt_count: int
    result_at: float
    seat_id: str | None = None
    receipt_ref: str | None = None
    credential_revision: int | None = None
    verification_source: str = 'node'

    @property
    def owner_label(self):
        return {'mac': 'Mac', 'ubuntu': 'Ubuntu'}[self.owner]

    def matches_plan(self, spec, credential_revision=None):
        # Ordinary scheduled plans alone are part of this activation scope.
        names = ('account_id', 'target_date', 'begin', 'end', 'seat_ids', 'due_at', 'kind')
        return (spec.operation_id is None and spec.kind == 'scheduled'
                and (credential_revision is None or self.credential_revision == credential_revision)
                and self.spec.revision <= spec.revision
                and all(getattr(self.spec, name) == getattr(spec, name) for name in names))

    def matches_record(self, account, record):
        """Recompute the executor digest from an actual, identity-bound record."""
        if (self.phase != 'completed' or type(record) is not dict
                or record.get('student_id') != account.get('student_id')
                or type(record.get('id')) is not str or not 1 <= len(record['id']) <= 256
                or type(record.get('status')) is not int or record['status'] != 1027
                or record.get('seat_id') != self.seat_id
                or record.get('begin') != f'{self.spec.target_date} {self.spec.begin}:00'
                or record.get('end') != f'{self.spec.target_date} {self.spec.end}:00'):
            return False
        value = {'task_id': self.task_id, 'receipt_id': record['id'],
                 **{name: record[name] for name in ('seat_id', 'begin', 'end', 'status')}}
        return canonical_hash(value) == self.receipt_ref

    def commits_record_id(self, account, record):
        """Recognize an actual UUID against the original digest, even if changed.

        This is only an identity/conflict check. The current record must still
        pass matches_record before it supplies active receipt evidence.
        """
        if (self.phase != 'completed' or type(record) is not dict
                or record.get('student_id') != account.get('student_id')
                or type(record.get('id')) is not str or not 1 <= len(record['id']) <= 256):
            return False
        return canonical_hash({'task_id': self.task_id, 'receipt_id': record['id'], 'seat_id': self.seat_id,
            'begin': f'{self.spec.target_date} {self.spec.begin}:00',
            'end': f'{self.spec.target_date} {self.spec.end}:00', 'status': 1027}) == self.receipt_ref

    def snapshot_conflict(self, account):
        values = account.get('snapshot')
        return (type(values) is list and len(values) <= 2000
                and any(self.commits_record_id(account, value) and not self.matches_record(account, value)
                        for value in values))


def _mirror_view(account, row, *, allow_zero_attempts=False):
    """Return a typed view only for an internally consistent production mirror."""
    try:
        require(type(account) is dict and type(row) is dict and 'native_binding' not in row)
        account_id = account['_id']
        require(type(account_id) is str and row['account_id'] == account_id)
        task = validate_task(row['dual_task'])
        require(task['account_ref'].startswith('acct-'))
        task_id, digest = task_identity(task)
        binding = exact(row['dual_binding'], {'schema', 'mode', 'authority_id', 'task_id', 'binding_sha256'})
        require(type(binding['schema']) is int and binding['schema'] == 1
                and binding['mode'] == 'production' and binding['task_id'] == task_id
                and binding['binding_sha256'] == digest)
        identifier(binding['authority_id'], size=32)
        spec = run_spec(task, account_id)
        require(row['_id'] == spec.key and row['target_date'] == spec.target_date
                and canonical_hash(row['spec']) == canonical_hash(asdict(spec)))
        progress = exact(row['dual_progress'], {'phase', 'owner', 'grant_id'})
        require(progress['owner'] in NODES and progress['phase'] in ('completed', 'failed', 'uncertain'))
        grant_id = identifier(progress['grant_id'], size=32)
        value = exact(row['dual_result'], {'status', 'verified', 'attempt_count', 'result_at'},
                      {'receipt_ref', 'seat_index', 'diagnostics'})
        require(value['status'] == progress['phase'] and type(value['verified']) is bool
                and type(value['attempt_count']) is int
                and (0 if allow_zero_attempts else 1) <= value['attempt_count'] <= 6)
        at = timestamp(value['result_at'])
        if 'diagnostics' in value:
            require(len(result_diagnostics(value['diagnostics'], task)['attempts']) <= value['attempt_count'])
        mirror = exact(row['dual_mirror'], {'result_sha256', 'grant_id'})
        require(mirror['grant_id'] == grant_id and mirror['result_sha256'] == canonical_hash(value))
        state = 'success' if progress['phase'] == 'completed' else progress['phase']
        result = exact(row['result'], {'status', 'message', 'records', 'verified_at'}, {'receipt_ref', 'seat_id'})
        require(row['status'] == state and result['status'] == state
                and type(result['message']) is str and type(result['records']) is list and result['records'] == [])
        seat = reference = None
        if state == 'success':
            reference = identifier(value.get('receipt_ref'), size=64)
            index = value.get('seat_index')
            require(value['verified'] is True and type(index) is int and 0 <= index < len(spec.seat_ids))
            seat = spec.seat_ids[index]
            require(result.get('receipt_ref') == reference and result.get('seat_id') == seat
                    and row.get('attempted_seat_id') == seat
                    and result['verified_at'] == datetime.fromtimestamp(at, ZONE).isoformat(timespec='seconds'))
        else:
            require(value['verified'] is False and 'receipt_ref' not in value and 'seat_index' not in value
                    and 'receipt_ref' not in result and 'seat_id' not in result and result['verified_at'] is None)
        return DualReceiptView(progress['phase'], state, progress['owner'], task_id, grant_id,
                               spec, value['attempt_count'], at, seat, reference, task['credential_revision'])
    except (DualNodeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return None


def reconciliation_basis(account, row):
    """Only issued uncertain work may gain independent local receipt evidence."""
    if type(row) is not dict or row.get('status') != 'uncertain': return None
    view = _mirror_view(account, row, allow_zero_attempts=True) or progress_view(account, row)
    return view if view is not None and view.phase in ('uncertain', 'authorized', 'cancel_requested') else None


def _supplemented_basis(account, row):
    """Validate the current node observation behind an independent local success."""
    phase = (row.get('dual_progress') or {}).get('phase')
    underlying = {'completed':'success', 'failed':'failed', 'uncertain':'uncertain',
        'authorized':'uncertain', 'cancel_requested':'uncertain', 'not_sent':'skipped'}.get(phase)
    if underlying is None: return None
    source = {**row, 'status':underlying}
    return _mirror_view(account, source, allow_zero_attempts=True) or progress_view(account, source)


def reconciliation_evidence(account, row, record, checked_at, *, existing=False):
    """Validate a real query record without modifying the server's result."""
    basis = _supplemented_basis(account, row) if existing else reconciliation_basis(account, row)
    require(basis is not None, 'mirror_reconciliation_unavailable')
    # This evidence stays in the local account database. The credential-free
    # RPC validator intentionally forbids student_id and must not be used here.
    require(type(record) is dict and set(record) == {'id', 'student_id', 'seat_id', 'begin', 'end', 'status'})
    require(type(record['id']) is str and 1 <= len(record['id']) <= 256
            and record['student_id'] == account['student_id']
            and record['seat_id'] in basis.spec.seat_ids
            and record['begin'] == f'{basis.spec.target_date} {basis.spec.begin}:00'
            and record['end'] == f'{basis.spec.target_date} {basis.spec.end}:00'
            and type(record['status']) is int and record['status'] == 1027,
            'mirror_receipt_mismatch')
    checked_at = timestamp(checked_at)
    require(checked_at >= (basis.spec.due_at if existing else max(basis.spec.due_at, basis.result_at)),
            'mirror_reconciliation_time')
    reference = canonical_hash({'task_id': basis.task_id, 'receipt_id': record['id'],
        **{key: record[key] for key in ('seat_id', 'begin', 'end', 'status')}})
    if basis.phase == 'completed':
        require(reference == basis.receipt_ref and record['seat_id'] == basis.seat_id,
                'mirror_reconciliation_conflict')
    return {'schema': 1, 'authority_id': row['dual_binding']['authority_id'],
        'binding_sha256': row['dual_binding']['binding_sha256'],
        'grant_id': basis.grant_id, 'source': 'local_query', 'checked_at': checked_at,
        'record': dict(record), 'receipt_ref': reference}


def receipt_view(account, row):
    """Project immutable node evidence or an independently verified local query."""
    if type(row) is not dict or 'dual_reconciliation' not in row:
        return _mirror_view(account, row)
    try:
        require(row.get('status') == 'success')
        evidence = row['dual_reconciliation']
        require(type(evidence) is dict and set(evidence) == {'schema', 'authority_id', 'binding_sha256', 'grant_id',
            'source', 'checked_at', 'record', 'receipt_ref'})
        require(type(evidence['schema']) is int and evidence['schema'] == 1)
        # Preserve the original result, grant and mirror digest as the basis.
        expected = reconciliation_evidence(account, row, evidence['record'], evidence['checked_at'], existing=True)
        require(evidence == expected, 'mirror_reconciliation_conflict')
        basis = _supplemented_basis(account, row)
        return replace(basis, phase='completed', status='success', result_at=evidence['checked_at'],
            seat_id=evidence['record']['seat_id'], receipt_ref=evidence['receipt_ref'],
            verification_source='local_query')
    except (DualNodeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return None


def progress_view(account, row):
    """Validate durable progress separately from a school's completion proof."""
    try:
        require(type(account) is dict and type(row) is dict and 'native_binding' not in row)
        require(row['account_id'] == account['_id'])
        task = validate_task(row['dual_task'])
        require(task['account_ref'].startswith('acct-'))
        key, digest = task_identity(task)
        binding = exact(row['dual_binding'], {'schema', 'mode', 'authority_id', 'task_id', 'binding_sha256'})
        require(type(binding['schema']) is int and binding['schema'] == 1 and binding['mode'] == 'production'
                and binding['task_id'] == key and binding['binding_sha256'] == digest)
        identifier(binding['authority_id'], size=32)
        spec = run_spec(task, account['_id'])
        require(row['_id'] == spec.key and row['target_date'] == spec.target_date
                and canonical_hash(row['spec']) == canonical_hash(asdict(spec)))
        progress = exact(row['dual_progress'], {'phase', 'owner', 'grant_id'})
        phase, owner, grant = progress['phase'], progress['owner'], progress['grant_id']
        states = {'collecting':'queued', 'selected':'preparing', 'ready':'preparing',
                  'authorized':'submitting', 'cancel_requested':'uncertain',
                  'cancelled':'skipped', 'expired_unissued':'skipped', 'not_sent':'skipped'}
        require(phase in states and owner in (None, *NODES))
        if phase in ('selected','ready','authorized','cancel_requested','not_sent'): require(owner in NODES)
        if phase in ('authorized','cancel_requested','not_sent'): identifier(grant, size=32)
        else: require(grant is None)
        if phase == 'not_sent':
            mirror = exact(row['dual_mirror'], {'result_sha256','grant_id'})
            require(mirror['grant_id'] == grant)
            value = exact(row['dual_result'], {'status','verified','attempt_count','result_at'}, {'diagnostics'})
            require(value['status'] == phase and value['verified'] is False
                    and type(value['attempt_count']) is int and 0 <= value['attempt_count'] <= 6)
            at = timestamp(value['result_at'])
            if 'diagnostics' in value:
                require(len(result_diagnostics(value['diagnostics'], task)['attempts']) <= value['attempt_count'])
            require(mirror['result_sha256'] == canonical_hash(value))
            result = row['result']
            require(type(result) is dict and result.get('status') == 'skipped'
                    and result.get('records') == [] and result.get('verified_at') is None)
        else:
            require(row.get('dual_result') is None and row.get('dual_mirror') is None and row.get('result') is None)
            at = 0
        expected = states[phase]
        require(row['status'] == expected or phase == 'authorized' and row['status'] == 'uncertain')
        return DualReceiptView(phase, row['status'], owner, key, grant, spec, 0, at, credential_revision=task['credential_revision'])
    except (DualNodeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return None
