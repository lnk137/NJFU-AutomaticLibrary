"""Disposable bounded helper: one fixed read-only time-source request."""
import json
import os
import signal
import threading
import time

from .trusted_clock import sample_verified_reference


def main():
    parent=os.getppid()
    if parent<=1:return 2
    signal.signal(signal.SIGALRM,lambda *_:os._exit(124))
    signal.alarm(3)
    def watch_parent():
        while True:
            time.sleep(.05)
            if os.getppid()!=parent:os._exit(125)
    threading.Thread(target=watch_parent,name='clock-probe-parent',daemon=True).start()
    def started():print('STARTED',flush=True)
    evidence=sample_verified_reference(permit=lambda:os.getppid()==parent,on_request_started=started)
    print('PROOF '+json.dumps(evidence,sort_keys=True,separators=(',',':')),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
