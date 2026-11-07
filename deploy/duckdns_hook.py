#!/usr/bin/python3
"""Certbot DNS-01 hook; credentials stay in a root-only file and out of logs."""
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler

DOMAIN='njfuautolib.duckdns.org'
TOKEN=Path('/etc/njfu-relay/duckdns-token')

def main():
    if os.environ.get('CERTBOT_DOMAIN')!=DOMAIN:raise RuntimeError('Unexpected certificate domain')
    if TOKEN.is_symlink() or TOKEN.stat().st_mode & 0o077:raise RuntimeError('Unsafe DNS credential file')
    token=TOKEN.read_text().strip()
    if not re.fullmatch(r'[a-zA-Z0-9-]{20,80}',token):raise RuntimeError('Invalid DNS credential format')
    cleanup=sys.argv[1]=='cleanup'
    value=os.environ.get('CERTBOT_VALIDATION','')
    if not cleanup and not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',value):raise RuntimeError('Invalid challenge')
    params={'domains':'njfuautolib','token':token,'txt':value}
    if cleanup:params['clear']='true'
    proxy=os.environ.get('NJFU_CERT_PROXY')
    if proxy and proxy!='http://127.0.0.1:15004':raise RuntimeError('Unexpected certificate proxy')
    opener=build_opener(ProxyHandler({'https':proxy} if proxy else {}))
    for attempt in range(5):
        try:
            with opener.open('https://www.duckdns.org/update?'+urlencode(params),timeout=15) as response:body=response.read(256).decode()
            if body.strip()!='OK':raise RuntimeError('DNS provider rejected update')
            break
        except Exception:
            if attempt==4:raise RuntimeError('DNS update unavailable; credentials omitted') from None
            time.sleep(2+attempt*2)
    if cleanup:return
    for attempt in range(60):
        answers=[]
        for resolver in (None,'1.1.1.1','8.8.8.8'):
            command=['dig','+short','+time=2','+tries=1','TXT','_acme-challenge.'+DOMAIN]+(['@'+resolver] if resolver else [])
            result=subprocess.run(command,capture_output=True,text=True,timeout=5)
            if value in result.stdout:print('DNS challenge is visible',flush=True);return
        time.sleep(5)
    raise RuntimeError('DNS challenge propagation timed out')

if __name__=='__main__':
    try:main()
    except Exception as exc:print(str(exc),file=sys.stderr);sys.exit(1)
