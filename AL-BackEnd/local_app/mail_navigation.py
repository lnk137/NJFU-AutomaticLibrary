"""Short browser-navigation evidence; grants never authorize an account or record."""
import hashlib
import hmac
import math
import re
import secrets
import time

NAV_COOKIE = 'njfu_mail_cancel_navigation'
_PURPOSE = b'njfu:mail-cancel:navigation-grant:v1\0'
_FORMAT = re.compile(r'v1\.([1-9][0-9]{0,11})\.([A-Za-z0-9_-]{32})\.([0-9a-f]{64})')


def _key(value):
    if type(value) not in (str, bytes) or not value or len(value) > 4096:
        raise ValueError('Invalid navigation signing key')
    try: raw = value.encode('utf-8') if type(value) is str else value
    except UnicodeError: raise ValueError('Invalid navigation signing key') from None
    if len(raw) > 4096: raise ValueError('Invalid navigation signing key')
    return raw


def _clock(value):
    value = time.time() if value is None else value
    if type(value) not in (int, float) or not 0 <= value <= 999999999000 or not math.isfinite(value):
        raise ValueError('Invalid navigation clock')
    return value


def issue_navigation_grant(key, now=None):
    raw, current = _key(key), _clock(now)
    payload = 'v1.' + str(int(current) + 60) + '.' + secrets.token_urlsafe(24)
    signature = hmac.new(raw, _PURPOSE + payload.encode('ascii'), hashlib.sha256).hexdigest()
    return payload + '.' + signature


def valid_navigation_grant(key, value, now=None):
    try:
        raw, current = _key(key), _clock(now)
        if type(value) is not str or len(value) > 256: return False
        match = _FORMAT.fullmatch(value)
        if not match: return False
        payload, signature = value.rsplit('.', 1)
        expected = hmac.new(raw, _PURPOSE + payload.encode('ascii'), hashlib.sha256).hexdigest()
        expiry = int(match[1])
        return hmac.compare_digest(expected, signature) and expiry - 60 <= current < expiry
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return False


def is_user_navigation(headers):
    required = {'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document'}
    # WebKit does not implement Sec-Fetch-User. A missing optional header is
    # different from an explicit contradictory one; navigation is not itself
    # account/receipt authorization. The capability and POST checks still own it.
    optional = {'sec-fetch-user': '?1'}
    purposes = {'purpose', 'sec-purpose', 'x-purpose', 'x-moz'}
    found = {}
    try:
        for name, value in headers.items():
            name = name.lower()
            if name not in required and name not in optional and name not in purposes: continue
            if name in found or type(value) is not str: return False
            # Prefetch/preview/prerender requests must never gain this grant,
            # including compound and unknown purpose values.
            if name in purposes and value: return False
            found[name] = value
    except (AttributeError, TypeError, ValueError):
        return False
    return (all(found.get(name) == value for name, value in required.items())
            and all(name not in found or found[name] == value for name, value in optional.items()))
