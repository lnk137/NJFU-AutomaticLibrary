"""Start the local multi-account app; real booking requires explicit CLI opt-in."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

from pymongo import MongoClient
from pymongo.errors import PyMongoError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / '.local-data'
URL = 'http://127.0.0.1:5003'
MONGO_PORT = 27019


def healthy():
    try:
        with urlopen(URL + '/health', timeout=1) as response:
            return json.load(response).get('application') == 'NJFU-AutomaticLibrary-local-multi'
    except (URLError, ValueError, TimeoutError, OSError):
        return False


def running_live_mode():
    try:
        with urlopen(URL + '/health', timeout=1) as response:
            status = json.load(response)
    except (URLError, ValueError, TimeoutError, OSError):
        raise RuntimeError('无法核实已运行后台的预约模式，请检查原启动终端。') from None
    if not isinstance(status, dict) or type(status.get('allow_live')) is not bool:
        raise RuntimeError('已运行后台未提供有效预约模式，请先在原启动终端停止再启动。')
    return status['allow_live']


def wait_ready(check, child, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f'{description}退出，请查看 {DATA / "logs"}')
        if check():
            return
        time.sleep(0.2)
    raise RuntimeError(f'{description}启动超时，请查看 {DATA / "logs"}')


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        child.wait()


def check_ports(ports):
    for port in ports:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',port))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--enable-live-booking', action='store_true',
                        help='Allow real scheduled bookings for account plans enabled in the page')
    parser.add_argument('--public-config',type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    DATA.mkdir(exist_ok=True, mode=0o700)
    with (DATA / 'launcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if healthy():
                if running_live_mode() != args.enable_live_booking:
                    raise RuntimeError('已运行后台与本次要求的预约模式不同。请先在原启动终端按 Ctrl+C 停止，再重新启动；本次未切换模式。')
                if args.public_config:
                    with urlopen(URL+'/health',timeout=2) as response:status=json.load(response)
                    if not status.get('public_enabled'):raise RuntimeError('已运行后台未启用公网入口，请先停止原后台再切换。')
                print(f'已经运行：{URL}', flush=True)
                if args.open_browser:
                    webbrowser.open(URL)
                return
            raise RuntimeError('另一个启动过程正在运行，请等待或检查启动终端。')
        if not (ROOT / 'AL-FrontEnd/dist/build/h5/index.html').is_file():
            raise RuntimeError('前端未编译，请先在 AL-FrontEnd 运行 npm run build:h5')
        check_ports((5003,MONGO_PORT))
        logs = DATA / 'logs'
        logs.mkdir(exist_ok=True, mode=0o700)
        dbpath = DATA / 'mongodb'
        dbpath.mkdir(exist_ok=True, mode=0o700)
        mongo = ROOT / '.runtime/mongodb-8.0.34/bin/mongod'
        children = []

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        try:
            with (logs / 'mongo-launch.log').open('a') as mongo_log, (logs / 'backend.log').open('a') as backend_log:
                child = subprocess.Popen([
                    str(mongo), '--bind_ip', '127.0.0.1', '--port', str(MONGO_PORT),
                    '--dbpath', str(dbpath), '--logpath', str(logs / 'mongodb.log'),
                    '--logappend', '--wiredTigerCacheSizeGB', '0.25',
                ], stdout=mongo_log, stderr=subprocess.STDOUT, start_new_session=True)
                children.append(child)
                with MongoClient(f'mongodb://127.0.0.1:{MONGO_PORT}/', serverSelectionTimeoutMS=500) as client:
                    def mongo_ready():
                        try:
                            return client.admin.command('ping')['ok'] == 1
                        except PyMongoError:
                            return False
                    wait_ready(mongo_ready, child, 'MongoDB')
                    print('本地数据库已连接；原有数据保留，使用独立多账号集合。', flush=True)
                env = {**os.environ, 'NJFU_MONGO_URI': f'mongodb://127.0.0.1:{MONGO_PORT}/', 'PYTHONUNBUFFERED': '1'}
                command = [sys.executable, '-B', 'main.py']
                if args.enable_live_booking:
                    command.append('--enable-live-booking')
                if args.public_config:command.extend(['--public-config',str(args.public_config.resolve())])
                backend = subprocess.Popen(command, cwd=ROOT / 'AL-BackEnd', env=env,
                                           stdout=backend_log, stderr=subprocess.STDOUT, start_new_session=True)
                children.append(backend)
                wait_ready(healthy, backend, '网页服务')
                mode = ('真实预约执行已开启；请在网页为所需账号点击“保存并开启定时”。'
                        if args.enable_live_booking else '真实预约总开关关闭，仅允许登录和查询。')
                print(f'已启动：{URL}\n保持本终端运行；按 Ctrl+C 停止。后台调度器已启动；{mode}', flush=True)
                if args.open_browser:
                    webbrowser.open(URL)
                while all(process.poll() is None for process in children):
                    time.sleep(1)
                raise RuntimeError(f'服务意外退出，请查看 {logs}')
        except KeyboardInterrupt:
            print('\n正在停止本地服务，保留数据库。', flush=True)
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            for process in reversed(children):
                stop_child(process)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'启动失败：{exc}', file=sys.stderr)
        sys.exit(1)
