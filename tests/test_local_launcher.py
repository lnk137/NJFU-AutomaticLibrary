import errno
from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('njfu_launcher_under_test',ROOT/'local_run.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def fixture_root(self,path):
        page=path/'AL-FrontEnd/dist/build/h5/index.html'
        page.parent.mkdir(parents=True);page.write_text('<html>Synthetic</html>')

    def test_occupied_port_never_starts_or_stops_another_process(self):
        listener=socket.socket()
        try:
            try:listener.bind(('127.0.0.1',5003));listener.listen(1)
            except OSError as exc:
                if exc.errno!=errno.EADDRINUSE:raise
            with tempfile.TemporaryDirectory() as d:
                root=Path(d);self.fixture_root(root)
                with patch.object(launcher,'ROOT',root),patch.object(launcher,'DATA',root/'data'),\
                        patch('sys.argv',['local_run.py']),patch.object(launcher.subprocess,'Popen') as spawn:
                    with self.assertRaises(OSError):launcher.main()
                    spawn.assert_not_called()
        finally:listener.close()

    def test_partial_startup_failure_stops_only_owned_child(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture_root(root)
            owned=MagicMock();owned.poll.return_value=None
            with patch.object(launcher,'ROOT',root),patch.object(launcher,'DATA',root/'data'),\
                    patch('sys.argv',['local_run.py']),patch.object(launcher.socket,'socket'),\
                    patch.object(launcher.signal,'signal'),\
                    patch.object(launcher,'MongoClient'),\
                    patch.object(launcher.subprocess,'Popen',return_value=owned) as spawn,\
                    patch.object(launcher,'wait_ready',side_effect=RuntimeError('Synthetic startup failure')):
                with self.assertRaisesRegex(RuntimeError,'Synthetic startup failure'):launcher.main()
                self.assertEqual(spawn.call_count,1)
                owned.terminate.assert_called_once_with();owned.wait.assert_called_once_with()

    @contextmanager
    def existing_launcher(self, live_mode, options=()):
        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = ({'application': 'NJFU-AutomaticLibrary-local-multi', 'status': 'ok', 'allow_live': live_mode}
                        if self.path == '/health' else {'allow_live': live_mode})
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory(prefix='njfu-launcher-test-') as directory:
            root = Path(directory)
            server = ThreadingHTTPServer(('127.0.0.1', 0), StatusHandler)
            runner = threading.Thread(target=server.serve_forever)
            runner.start()
            try:
                with (root / 'launcher.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with patch.object(launcher, 'ROOT', root), patch.object(launcher, 'DATA', root), \
                            patch.object(launcher, 'URL', f'http://127.0.0.1:{server.server_port}'), \
                            patch('sys.argv', ['local_run.py', *options]), patch('sys.stdout', new_callable=io.StringIO):
                        yield
            finally:
                server.shutdown()
                server.server_close()
                runner.join()

    def test_default_launch_refuses_to_reuse_a_live_instance(self):
        with self.existing_launcher(True):
            with self.assertRaises(RuntimeError):
                launcher.main()

    def test_live_launch_refuses_to_reuse_a_query_only_instance(self):
        with self.existing_launcher(False, ['--enable-live-booking']):
            with self.assertRaises(RuntimeError):
                launcher.main()

    def test_matching_launch_modes_reuse_existing_service(self):
        for mode, options in ((False, []), (True, ['--enable-live-booking'])):
            with self.subTest(mode=mode), self.existing_launcher(mode, options):
                launcher.main()

    def test_unreadable_running_mode_is_not_assumed_safe(self):
        with self.existing_launcher('false'):
            with self.assertRaises(RuntimeError):
                launcher.main()

    def test_live_option_reaches_only_the_backend_child(self):
        for options in ([], ['--enable-live-booking']):
            with self.subTest(options=options), tempfile.TemporaryDirectory(prefix='njfu-launcher-test-') as directory:
                root = Path(directory)
                self.fixture_root(root)
                children = [MagicMock(), MagicMock()]
                for child in children:
                    child.poll.return_value = None
                with patch.object(launcher, 'ROOT', root), patch.object(launcher, 'DATA', root / 'data'), \
                        patch('sys.argv', ['local_run.py', *options]), patch.object(launcher.socket, 'socket'), \
                        patch.object(launcher.signal, 'signal'), patch.object(launcher, 'MongoClient'), \
                        patch.object(launcher, 'wait_ready'), patch.object(launcher.time, 'sleep', side_effect=KeyboardInterrupt), \
                        patch.object(launcher.subprocess, 'Popen', side_effect=children) as spawn, \
                        patch('sys.stdout', new_callable=io.StringIO):
                    launcher.main()
                commands = [call.args[0] for call in spawn.call_args_list]
                self.assertNotIn('--enable-live-booking', commands[0])
                self.assertEqual('--enable-live-booking' in commands[1], bool(options))


if __name__=='__main__':unittest.main()
