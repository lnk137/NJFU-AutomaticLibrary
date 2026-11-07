"""A silent local database peer cannot hold an application operation forever."""
from pathlib import Path
import select
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest

from pymongo.errors import PyMongoError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.config import load_settings
from local_app.runtime import create_runtime
from support.account_fixture import School
from support.mongo_fixture import MongoFixture


class DatabaseBudgetTests(unittest.TestCase):
    def test_stalled_database_operation_times_out_and_next_read_recovers(self):
        mongo = MongoFixture()
        self.addCleanup(mongo.close)
        blocked, stopping = threading.Event(), threading.Event()
        class Proxy(socketserver.BaseRequestHandler):
            def handle(self):
                with socket.create_connection(('127.0.0.1', mongo.port), timeout=2) as backend:
                    peers = {self.request: backend, backend: self.request}
                    try:
                        while not stopping.is_set():
                            readable, _, _ = select.select(list(peers), [], [], .02)
                            for source in readable:
                                if source is backend and blocked.is_set():continue
                                data = source.recv(65536)
                                if not data:return
                                peers[source].sendall(data)
                    except OSError:pass
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
        server = Server(('127.0.0.1', 0), Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        runtime = None
        with tempfile.TemporaryDirectory(prefix='njfu-db-budget-') as directory:
            try:
                runtime = create_runtime(load_settings(Path(__file__).resolve().parents[1], {
                    'data_dir': Path(directory), 'mongo_uri': f'mongodb://127.0.0.1:{server.server_address[1]}/',
                    'database_name': mongo.database().name}), client_factory=School)
                self.assertEqual(runtime.store.list_accounts(), [])
                blocked.set()
                finished, errors = threading.Event(), []
                def read():
                    try:runtime.store.list_accounts()
                    except PyMongoError as exc:errors.append(type(exc).__name__)
                    finally:finished.set()
                worker = threading.Thread(target=read, daemon=True);worker.start()
                try:
                    self.assertTrue(finished.wait(3.2), 'Silent database exceeded the bounded operation budget')
                    self.assertTrue(errors, 'A missing response cannot be treated as an empty successful read')
                finally:
                    blocked.clear();worker.join(3)
                self.assertEqual(runtime.store.list_accounts(), [])
            finally:
                blocked.clear()
                if runtime:runtime.close()
                stopping.set();server.shutdown();server.server_close();thread.join(2)
