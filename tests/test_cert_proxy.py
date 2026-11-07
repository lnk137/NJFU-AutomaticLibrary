from pathlib import Path
import importlib.util
import socket
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deploy.cert_proxy import connect_target,CertificateProxy

class CertificateProxyTests(unittest.TestCase):
    def test_only_exact_certificate_hosts_and_tls_ports_are_allowed(self):
        self.assertEqual(connect_target('www.duckdns.org:443'),('www.duckdns.org',443))
        for target in ['example.com:443','www.duckdns.org:80','www.duckdns.org.evil:443','127.0.0.1:22','acme-v02.api.letsencrypt.org:443@evil:443']:
            with self.subTest(target=target),self.assertRaises(ValueError):connect_target(target)
    def test_arbitrary_connect_is_denied_without_opening_any_upstream(self):
        proxy=CertificateProxy(0,upstream=('127.0.0.1',1));proxy.start()
        try:
            with socket.create_connection(proxy.server.server_address,timeout=2) as sock:
                sock.sendall(b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n')
                self.assertIn(b'403 Forbidden',sock.recv(1024))
        finally:proxy.close()
