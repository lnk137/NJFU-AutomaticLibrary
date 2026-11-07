import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from support.school_fixture import Transport, login_steps, query_step


class SchoolTests(unittest.TestCase):
    def test_login_identity_must_match(self):
        from local_app.school import CampusClient
        from local_app.models import AppError
        t = Transport(login_steps('other'))
        with self.assertRaises(AppError): CampusClient('fixture01', 'pass', transport=t).login()

    def test_query_failure_is_not_empty_success(self):
        from local_app.school import CampusClient
        from local_app.models import AppError
        t = Transport(login_steps()+[query_step(code=300)])
        c = CampusClient('fixture01', 'pass', transport=t);c.login()
        from datetime import date
        with self.assertRaises(AppError): c.query(date.today(), date.today())

    def test_two_clients_never_share_cookies(self):
        from local_app.school import CampusClient
        a, b = Transport(login_steps()), Transport(login_steps('fixture02'))
        ca=CampusClient('fixture01','pass',transport=a); cb=CampusClient('fixture02','pass',transport=b)
        self.assertEqual(len(a.calls), 0)
        ca.login();cb.login()
        self.assertNotEqual(a.cookies.get('ic-cookie'), b.cookies.get('ic-cookie'))

    def test_wrong_json_and_verification_required(self):
        from local_app.school import CampusClient
        from local_app.models import AppError
        for body in ('<html>验证码</html>', {'code': 123, 'message': '验证码'}):
            steps=login_steps(); steps[-1]=('GET','/ic-web/auth/userInfo',body,200,{})
            with self.subTest(body=body), self.assertRaises(AppError): CampusClient('fixture01','pass',transport=Transport(steps)).login()

    def test_untrusted_redirect_blocked(self):
        from local_app.school import CampusClient
        from local_app.models import AppError
        t=Transport([('GET','/ic-web/auth/address','',302,{'Location':'https://example.com/'})])
        with self.assertRaises(AppError): CampusClient('fixture01','pass',transport=t).login()
        self.assertEqual(len(t.calls),1)

    def test_submit_disabled_before_network(self):
        from local_app.school import CampusClient
        from local_app.models import AppError
        t=Transport([])
        with self.assertRaises(AppError): CampusClient('fixture01','pass',transport=t).submit(None,'1')
        self.assertEqual(t.calls,[])


if __name__ == '__main__':unittest.main()
