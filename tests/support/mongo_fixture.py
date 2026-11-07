from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid
from pymongo import MongoClient


class MongoFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='njfu-local-tests-')
        path = Path(self.temp.name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        binary = Path(__file__).resolve().parents[2] / '.runtime/mongodb-8.0.34/bin/mongod'
        self.log = (path/'launch.log').open('wb')
        self.process = subprocess.Popen([str(binary), '--bind_ip','127.0.0.1','--port',str(self.port),
            '--dbpath',str(path),'--wiredTigerCacheSizeGB','0.25','--logpath',str(path/'mongo.log')],
            stdin=subprocess.DEVNULL,stdout=self.log,stderr=subprocess.STDOUT)
        self.uri = f'mongodb://127.0.0.1:{self.port}/'
        self.client = MongoClient(self.uri,serverSelectionTimeoutMS=200)
        try:
            until = time.monotonic()+15
            while True:
                try:self.client.admin.command('ping');break
                except Exception:
                    if self.process.poll() is not None or time.monotonic()>until:raise RuntimeError('Isolated MongoDB failed to start') from None
                    time.sleep(.1)
        except Exception:self.close();raise

    def database(self):return self.client['njfu_test_'+uuid.uuid4().hex]

    def close(self):
        self.client.close()
        if self.process.poll() is None:
            self.process.terminate();self.process.wait(timeout=15)
        self.log.close();self.temp.cleanup()
