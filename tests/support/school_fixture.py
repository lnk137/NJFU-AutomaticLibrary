import json
import requests
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
from .cas_fixture import cas_steps


class Transport:
    def __init__(self, steps):
        self.steps, self.calls = list(steps), []
        self.headers, self.cookies = {}, requests.cookies.RequestsCookieJar()
        self.trust_env = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        expected, suffix, payload, status, headers = self.steps.pop(0)
        assert method == expected and url.endswith(suffix), (method, url, expected, suffix)
        if isinstance(payload, Exception): raise payload
        r = requests.Response(); r.status_code = status; r.url = url; r.headers.update(headers)
        r._content = (json.dumps(payload) if isinstance(payload, (dict, list)) else payload).encode()
        cookie=SimpleCookie();cookie.load(headers.get('Set-Cookie',''))
        for value in cookie.values():
            self.cookies.set(value.key,value.value,domain=urlsplit(url).hostname,
                path=value['path'] or '/',secure=bool(value['secure']))
        return r

    def close(self): pass


def login_steps(student='fixture01'):
    return cas_steps(student)


def query_step(data=None, code=0):
    return ('GET', '/ic-web/reserve/resvInfo', {'code': code, 'data': [] if data is None else data}, 200, {})
