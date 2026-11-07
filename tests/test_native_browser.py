"""Browser acceptance of the real candidate app on a private fixture port."""
from dataclasses import replace
import os
from pathlib import Path
import socket
import subprocess
import threading
from werkzeug.serving import make_server
from local_app.runtime import create_runtime
from support.native_runtime import NativeRuntimeCase


class NativeBrowserTests(NativeRuntimeCase):
    def test_root_and_student_desktop_ipad_phone(self):
        output=os.environ.get('NJFU_BROWSER_EVIDENCE')
        if not output:self.skipTest('Explicit browser evidence directory required')
        output=Path(output);output.mkdir(mode=0o700,exist_ok=True)
        self.runtime.close()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        settings=replace(self.settings,port=port)
        self.runtime=create_runtime(settings,client_factory=self.client_factory,native_factory=self.native_factory)
        self.addCleanup(self.runtime.close);self.runtime.start()
        server=make_server('127.0.0.1',port,self.runtime.app,threaded=True)
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        result=subprocess.run(['/opt/homebrew/bin/node',str(self.root/'tests/support/native_browser.mjs'),
            f'http://127.0.0.1:{port}',str(output)],capture_output=True,text=True,timeout=100)
        (output/'console.log').write_text(result.stdout+result.stderr)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.fixture.posts(),[])
