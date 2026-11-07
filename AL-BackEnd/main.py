"""Explicit campus-local entry point; importing it starts no external activity."""
import argparse
from pathlib import Path
import os
import signal


def main():
    parser = argparse.ArgumentParser(description='Campus-local app; real bookings are disabled by default.')
    parser.add_argument('--enable-live-booking', action='store_true',
                        help='Allow scheduled school submissions for individually enabled account plans')
    parser.add_argument('--public-config',type=Path,help='Private HTTPS relay config; root remains local only')
    args = parser.parse_args()
    from local_app.config import load_settings, public_overrides
    from local_app.runtime import create_runtime
    root=Path(__file__).resolve().parents[1]
    settings=load_settings(root,{'mongo_uri':os.environ.get('NJFU_MONGO_URI','mongodb://127.0.0.1:27019/'),
                                 'allow_live':args.enable_live_booking} | (public_overrides(args.public_config) if args.public_config else {}))
    runtime=create_runtime(settings)
    def stop(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        runtime.start()
        if settings.public_origin:
            from waitress import serve
            serve(runtime.app,host='127.0.0.1',port=settings.port,threads=16,connection_limit=100,
                  max_request_body_size=65536,channel_timeout=60,clear_untrusted_proxy_headers=True)
        else:runtime.app.run(host='127.0.0.1',port=settings.port,threaded=True,debug=False,use_reloader=False)
    except KeyboardInterrupt:pass
    finally:runtime.close()


if __name__=='__main__':main()
