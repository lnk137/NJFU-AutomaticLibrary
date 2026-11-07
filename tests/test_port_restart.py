"""Restart preflight distinguishes an active listener from closed TCP connections."""
import importlib.util
from pathlib import Path
import socket
import unittest
spec=importlib.util.spec_from_file_location('launcher_port_test',Path(__file__).resolve().parents[1]/'local_run.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

class PortRestartTests(unittest.TestCase):
    def test_active_listener_is_not_claimed_by_a_new_launcher(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            server.bind(('127.0.0.1',0));server.listen()
            with self.assertRaises(OSError):launcher.check_ports([server.getsockname()[1]])
    def test_closed_connections_do_not_prevent_restart(self):
        server=socket.socket();server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server.bind(('127.0.0.1',0));server.listen();port=server.getsockname()[1]
        client=socket.create_connection(('127.0.0.1',port));peer,_=server.accept()
        peer.close();server.close();client.close()
        launcher.check_ports([port])
