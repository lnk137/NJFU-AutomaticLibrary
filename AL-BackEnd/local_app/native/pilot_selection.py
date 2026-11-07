"""Bind an isolated pilot to one exact identity and scheduled release."""
from datetime import date
import json
import math
import os
import stat


class PilotSelection:
    @classmethod
    def from_file(cls, path):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:raise ValueError('duplicate configuration field')
                result[key] = value
            return result
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_nlink != 1 or not 0 < info.st_size <= 4096):
                raise ValueError('invalid private configuration')
            raw = os.read(descriptor, 4097)
            if len(raw) != info.st_size or len(raw) > 4096:
                raise ValueError('invalid private configuration size')
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
            fields = {'schema','account_id','student_id','revision','credential_revision','due_at','target_date'}
            if (not isinstance(value, dict) or set(value) != fields or
                    type(value['schema']) is not int or value['schema'] != 1):
                raise ValueError('invalid private configuration schema')
            return cls(value['account_id'], value['student_id'], value['revision'],
                       value['credential_revision'], value['due_at'], value['target_date'])
        except (OSError, UnicodeError, TypeError, ValueError):
            raise ValueError('invalid private pilot configuration') from None
        finally:
            if descriptor is not None:os.close(descriptor)

    def __init__(self, account_id, student_id, revision, credential_revision, due_at, target_date):
        if (not isinstance(account_id, str) or not 0 < len(account_id) <= 256 or
                not isinstance(student_id, str) or not 0 < len(student_id) <= 256 or
                any(ord(char) < 33 for char in account_id+student_id) or
                type(revision) is not int or revision < 1 or
                type(credential_revision) is not int or credential_revision < 1 or
                type(due_at) not in (int, float) or not math.isfinite(due_at) or due_at <= 0 or
                not isinstance(target_date, str)):
            raise ValueError('invalid pilot selection')
        try:
            if date.fromisoformat(target_date).isoformat() != target_date:
                raise ValueError('invalid pilot date')
        except ValueError:
            raise ValueError('invalid pilot date') from None
        self.account_id = account_id
        self.student_id = student_id
        self.revision = revision
        self.credential_revision = credential_revision
        self.due_at = due_at
        self.target_date = target_date

    def selects(self, run):
        return (getattr(run, 'kind', None) == 'scheduled' and
                getattr(run, 'account_id', None) == self.account_id and
                type(getattr(run, 'due_at', None)) in (int, float) and
                getattr(run, 'due_at', None) == self.due_at)

    def verify(self, run, row, state):
        return (self.selects(run) and type(getattr(run, 'revision', None)) is int and
                run.revision == self.revision and run.target_date == self.target_date and
                isinstance(row, dict) and row.get('student_id') == self.student_id and
                type(row.get('revision')) is int and row['revision'] == self.revision and
                isinstance(state, dict) and type(state.get('credential')) is int and
                state['credential'] == self.credential_revision)
