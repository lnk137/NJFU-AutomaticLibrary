"""Install this staged release only; keep exact rollback and never print secrets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import sys

BASE = Path('/srv/njfu-inbox')
SITE = Path('/etc/nginx/conf.d/njfu-library.conf')
UNIT = Path('/etc/systemd/system/njfu-inbox.service')
ORIGIN = 'https://njfuautolib.duckdns.org:45581'


def command(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode: raise RuntimeError('Command failed: '+args[0])
    return result.stdout


def write(path, data, mode, owner=0):
    if path.is_symlink(): raise RuntimeError('Refusing symlink target')
    temporary = path.with_name(path.name+'.njfu-new')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(temporary,mode); os.chown(temporary,owner,-1); os.replace(temporary,path)
    finally:
        if temporary.exists(): temporary.unlink()


def install(activate=False):
    if os.geteuid() != 0: raise RuntimeError('Server administrator required')
    app = Path(__file__).resolve().parents[1]; release = app.parent
    if release.parent != BASE/'releases' or not (release/'h5/index.html').is_file():
        raise RuntimeError('Run only from a staged project release')
    for directory in (BASE,release,app):
        if directory.is_symlink() or directory.stat().st_uid != 0 or directory.stat().st_mode&0o022:
            raise RuntimeError('Untrusted release directory')
    if not (release/'validated.json').is_file(): raise RuntimeError('Validate this release before installation')
    validated=json.loads((release/'validated.json').read_text())
    if not isinstance(validated.get('files'),dict) or not validated['files']:
        raise RuntimeError('Source-bound validation manifest required')
    for name,digest in validated['files'].items():
        path=release/name
        if path.is_symlink() or not path.resolve().is_relative_to(release) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise RuntimeError('Validated source or asset changed')
    for path in (BASE,BASE/'releases',release):os.chmod(path,0o755)
    rollback = BASE/'rollback'/release.name
    rollback.mkdir(parents=True,mode=0o700,exist_ok=True)
    if not (rollback/'njfu-library.conf').exists(): shutil.copy2(SITE,rollback/'njfu-library.conf')
    if UNIT.exists() and not (rollback/'njfu-inbox.service').exists():
        if 'Description=NJFU offline inbox' not in UNIT.read_text(): raise RuntimeError('Foreign service file exists')
        shutil.copy2(UNIT,rollback/'njfu-inbox.service')
    try: user = pwd.getpwnam('njfu-inbox')
    except KeyError:
        command(['/usr/sbin/useradd','--system','--no-create-home','--home-dir','/nonexistent','--shell','/usr/sbin/nologin','njfu-inbox'])
        user = pwd.getpwnam('njfu-inbox')
    private = BASE/'private'
    if private.is_symlink(): raise RuntimeError('Unsafe private directory')
    private.mkdir(mode=0o700,exist_ok=True); os.chmod(private,0o700); os.chown(private,user.pw_uid,user.pw_gid)
    proxy = Path('/etc/njfu-relay/proxy.inc').read_text()
    match = re.search(r'proxy_set_header\s+X-NJFU-Relay-Token\s+["\']?([0-9a-f]{64})["\']?;',proxy)
    if not match: raise RuntimeError('Existing relay token was not found safely')
    relay = match.group(1)
    write(private/'relay-token',relay+'\n',0o600,user.pw_uid)
    token_path = private/'worker-token'
    if not token_path.exists(): write(token_path,secrets.token_hex(32)+'\n',0o600,user.pw_uid)
    if token_path.is_symlink() or token_path.stat().st_mode&0o077 or not re.fullmatch(r'[0-9a-f]{64}\n?',token_path.read_text()):
        raise RuntimeError('Invalid worker token file')
    config = {'origin':ORIGIN,'relay_token_file':str(private/'relay-token'),'worker_token_file':str(token_path),
              'database':str(private/'inbox.sqlite'),'static_root':str(release/'h5')}
    old_config = private/'config.json'
    if old_config.exists() and not (rollback/'config.json').exists(): shutil.copy2(old_config,rollback/'config.json')
    write(old_config,json.dumps(config)+'\n',0o600,user.pw_uid)
    for directory in (BASE/'runtime', app, release/'h5'):
        for parent,dirs,files in os.walk(directory,followlinks=False):
            if Path(parent).is_symlink(): raise RuntimeError('Unexpected release symlink')
            os.chmod(parent,0o755)
            for name in files:
                path=Path(parent)/name
                if path.is_symlink(): raise RuntimeError('Unexpected release symlink')
                os.chmod(path,0o644)
    service = f'''[Unit]
Description=NJFU offline inbox
After=network-online.target
Wants=network-online.target

[Service]
User=njfu-inbox
Group=njfu-inbox
WorkingDirectory={app}
Environment=PYTHONPATH={BASE}/runtime:{app}:{app}/AL-BackEnd
ExecStart=/usr/bin/python3 -B -m deploy.inbox_gateway --config {private}/config.json
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths={private}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
'''
    write(UNIT,service,0o644)
    command(['/usr/bin/systemctl','daemon-reload'])
    command(['/usr/bin/systemctl','enable','njfu-inbox.service'])
    command(['/usr/bin/systemctl','restart','njfu-inbox.service'])
    if activate:
        include = Path('/etc/njfu-inbox')
        if include.is_symlink(): raise RuntimeError('Unsafe proxy directory')
        include.mkdir(mode=0o700,exist_ok=True)
        new_proxy = proxy.replace('http://127.0.0.1:15003','http://127.0.0.1:15005').replace('proxy_intercept_errors on;','proxy_intercept_errors off;')
        write(include/'proxy.inc',new_proxy,0o600)
        write(include/'worker.inc',new_proxy.replace('http://127.0.0.1:15005','http://127.0.0.1:15006'),0o600)
        write(SITE,(app/'deploy/nginx-inbox.conf.template').read_text(),0o644)
        try: command(['/usr/sbin/nginx','-t'])
        except Exception:
            write(SITE,(rollback/'njfu-library.conf').read_text(),0o644)
            raise
        command(['/usr/bin/systemctl','reload','nginx'])
    report = {'release':release.name,'gateway_active':True,'public_switched':activate,
              'rollback_directory':str(rollback),'source_sha256':hashlib.sha256((app/'deploy/inbox_gateway.py').read_bytes()).hexdigest()}
    write(release/'installation.json',json.dumps(report,indent=2)+'\n',0o600)
    print(json.dumps(report))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--activate',action='store_true');args=parser.parse_args()
    try:install(args.activate)
    except Exception as exc:
        print(type(exc).__name__+': installation stopped; existing rollback files retained',file=sys.stderr)
        sys.exit(1)
