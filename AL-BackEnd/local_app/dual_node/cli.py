"""Explicit node lifecycle. Default-disabled configs perform no school requests."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import signal
import time


def read_config(path):
    path = Path(path).absolute()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError('unsafe_node_config')
    if info.st_size > 16384:
        raise ValueError('node_config_too_large')
    from .contracts import parse_json
    value = parse_json(path.read_bytes())
    fields = {'schema','enabled','mode','node_id','authority_id','public_key','ssh_config','ssh_host',
              'source_root','runtime_dir','credential_dir'}
    if (type(value) is not dict or set(value) != fields or type(value['schema']) is not int
            or value['schema'] != 1 or type(value['enabled']) is not bool
            or value['node_id'] not in ('mac','ubuntu') or value['mode'] != 'production'):
        raise ValueError('invalid_node_config')
    for field in ('ssh_config','source_root','runtime_dir','credential_dir'):
        if type(value[field]) is not str or not Path(value[field]).is_absolute():
            raise ValueError('node_path_must_be_absolute')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description='Opt-in NJFU dual-node executor')
    parser.add_argument('--config', required=True)
    parser.add_argument('--check', action='store_true', help='Read-only local checks; no network or login')
    parser.add_argument('--initialize-runtime', action='store_true', help='Explicit new node journal initialization')
    args = parser.parse_args(argv)
    diagnostics = None
    try:
        config = read_config(args.config)
        if not config['enabled']:
            print(json.dumps({'status':'disabled','school_requests':0}))
            return 3
        from .credentials import CredentialStore
        from .grants import GrantVerifier
        from .transport import SSHTransport
        from .agent import RPCClient, NodeAgent, NodeSafetyError
        from .diagnostics import NodeDiagnostics
        from .executor import HeadlessExecutor
        from .journal import BurnJournal
        from local_app.native.build import read_build
        from local_app.native.protocol import clock_ns
        if not args.check and not args.initialize_runtime:
            diagnostics = NodeDiagnostics(Path(config['runtime_dir'])/'diagnostics', node_id=config['node_id'])
        build = read_build(config['source_root'])
        credentials = CredentialStore(config['credential_dir'])
        provider = credentials.provider()
        verifier = GrantVerifier(config['public_key'], mode='production', authority_id=config['authority_id'])
        if args.check:
            print(json.dumps({'status':'local_checks_passed','node_id':config['node_id'],
                              'native_binary_sha256':build.binary_sha256,'school_requests':0,
                              'server_and_credentials_authentication':'not_checked'}))
            return 0
        rpc = RPCClient(lambda: SSHTransport(config['ssh_config'], config['ssh_host']))
        executor = None
        agent = None
        try:
            # This helper owns no native process and never authenticates.
            bootstrap = object.__new__(NodeAgent)
            bootstrap.rpc, bootstrap.node_id = rpc, config['node_id']
            bootstrap.verifier, bootstrap.continuous_ns = verifier, clock_ns
            permit = bootstrap.time_permit()
            runtime = Path(config['runtime_dir'])
            if args.initialize_runtime:
                if runtime.exists():
                    raise ValueError('runtime_already_exists')
                runtime.mkdir(mode=0o700)
                journal = BurnJournal.initialize(runtime/'grants'); journal.close()
                print(json.dumps({'status':'runtime_initialized','school_requests':0}))
                return 0
            executor = HeadlessExecutor(runtime, build, config['node_id'], provider, verifier,
                                        time_permit=permit, source_root=config['source_root'])
            cached = {'permit':permit, 'refreshed':time.monotonic()}
            def capability(task):
                try:
                    if time.monotonic()-cached['refreshed'] >= 60:
                        cached['permit'] = bootstrap.time_permit()
                        cached['refreshed'] = time.monotonic()
                    cached['permit'].require_valid(task)
                    executor.set_time_permit(cached['permit'])
                except (ValueError, OSError):
                    raise NodeSafetyError('clock_invalid') from None
                present = True
                try:
                    provider.binding(task)
                except ValueError:
                    present = False
                now = time.time()
                return {'schema':1,'checked_at':now,'expires_at':now+45,
                        'credential_revision':task['credential_revision'],'credential_present':present,
                        'native_ready':executor.health() == 'ready','native_binary_sha256':build.binary_sha256,
                        'worker_count':20,'clock_checked':True}
            guard = None
            if sys.platform == 'darwin':
                from .power_guard import MacAwakeGuard
                guard = MacAwakeGuard(wake_file=runtime/'dual-probe-next.json')
            agent = NodeAgent(rpc, config['node_id'], executor, verifier, capability,power_guard=guard,diagnostics=diagnostics)
            previous_term=signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM,lambda *_:agent.request_stop())
            try:
                agent.run()
            finally:
                signal.signal(signal.SIGTERM,previous_term)
        finally:
            if agent is None:
                if executor is not None: executor.close()
                rpc.close()
            close = getattr(credentials, 'close', None)
            if close is not None: close()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        if diagnostics is not None:
            diagnostics.record('startup', 'node_preflight_or_runtime_failed')
        # Keep fixed diagnostics: exception strings from SSH/auth/vault/native
        # may include sensitive page text or filesystem data.
        print(json.dumps({'status':'blocked','code':'node_preflight_or_runtime_failed'}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
