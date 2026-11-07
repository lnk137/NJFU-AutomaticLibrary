"""Loopback CONNECT relay for certificate endpoints only; TLS stays end-to-end."""
import re
import select
import socket
import socketserver
import ssl
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
import subprocess
import sys
import threading

HOSTS=frozenset({'acme-v02.api.letsencrypt.org','acme-staging-v02.api.letsencrypt.org','www.duckdns.org'})


def connect_target(value):
    if not isinstance(value,str) or value not in {host+':443' for host in HOSTS}:
        raise ValueError('Certificate endpoint not allowed')
    return value.rsplit(':',1)[0],443


def system_proxy():
    if sys.platform!='darwin':return None
    result=subprocess.run(['/usr/sbin/scutil','--proxy'],capture_output=True,text=True,timeout=3)
    values=dict(re.findall(r'^\s*(HTTPS(?:Enable|Proxy|Port))\s*:\s*(\S+)',result.stdout,re.M))
    if values.get('HTTPSEnable')=='1' and values.get('HTTPSProxy') in {'127.0.0.1','localhost'}:
        port=int(values['HTTPSPort'])
        if 1<=port<=65535:return values['HTTPSProxy'],port
    return None


class CertificateProxy:
    def __init__(self,port=5027,*,upstream=None):
        upstream=system_proxy() if upstream is None else upstream
        dns_lock=threading.Lock();dns_cache={}
        def dns_address():
            with dns_lock:
                if dns_cache.get('until',0)>time.monotonic():return dns_cache['address']
                addresses=sorted({r[4][0] for r in socket.getaddrinfo('www.duckdns.org',443,socket.AF_INET)})
                def probe(address):
                    try:
                        with socket.create_connection((address,443),timeout=4) as raw:
                            with ssl.create_default_context().wrap_socket(raw,server_hostname='www.duckdns.org'):return address
                    except OSError:return None
                pool=ThreadPoolExecutor(max_workers=min(4,len(addresses)))
                try:
                    for future in as_completed([pool.submit(probe,ip) for ip in addresses]):
                        address=future.result()
                        if address:
                            dns_cache.update(address=address,until=time.monotonic()+60)
                            return address
                finally:pool.shutdown(wait=False,cancel_futures=True)
                raise OSError('DNS provider TLS unreachable')
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(15)
                try:
                    line=self.rfile.readline(1024).decode('ascii').strip().split()
                    if len(line)!=3 or line[0]!='CONNECT':raise ValueError()
                    host,port=connect_target(line[1])
                    total=0
                    while True:
                        header=self.rfile.readline(1024);total+=len(header)
                        if total>8192:raise ValueError()
                        if header in (b'\r\n',b'\n'):break
                        if not header:raise ValueError()
                    selected_upstream=upstream if host!='www.duckdns.org' else None
                    target=selected_upstream or ((dns_address(),443) if host=='www.duckdns.org' else (host,port))
                    with socket.create_connection(target,timeout=10) as remote:
                        if selected_upstream:
                            remote.sendall(('CONNECT '+line[1]+' HTTP/1.1\r\nHost: '+line[1]+'\r\n\r\n').encode())
                            response=b''
                            while b'\r\n\r\n' not in response:
                                chunk=remote.recv(1024)
                                if not chunk or len(response)>8192:raise OSError()
                                response+=chunk
                            if response.split(b'\r\n',1)[0].split()[1]!=b'200':raise OSError()
                        self.connection.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                        while True:
                            ready,_,_=select.select([self.connection,remote],[],[],120)
                            if not ready:return
                            for source in ready:
                                data=source.recv(16384)
                                if not data:return
                                (remote if source is self.connection else self.connection).sendall(data)
                except ValueError:self.connection.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                except (OSError,UnicodeError):return
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True;daemon_threads=True
        self.server=Server(('127.0.0.1',port),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def start(self):self.thread.start()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()
