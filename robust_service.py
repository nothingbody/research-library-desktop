"""Local control service; session credentials remain in process memory."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time

from robust_collector import Engine
from robust_runtime import InstanceLock, ORIGIN, atomic_json, export_snapshot


def make_handler(engine, nonce):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def allowed(self):
            return (self.client_address[0]=='127.0.0.1' and self.headers.get('Origin')==ORIGIN
                    and self.path in ('/session/'+nonce,'/control/'+nonce,'/status/'+nonce))

        def reply(self,status,body=None):
            content=json.dumps(body or {},ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Access-Control-Allow-Origin',ORIGIN)
            self.send_header('Access-Control-Allow-Methods','POST, GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers','Content-Type')
            self.send_header('Access-Control-Allow-Private-Network','true')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(content)))
            self.end_headers()
            if status!=204:
                self.wfile.write(content)

        def do_OPTIONS(self):
            self.reply(204 if self.allowed() else 403)

        def do_GET(self):
            if not self.allowed() or not self.path.startswith('/status/'):
                self.reply(403)
                return
            self.reply(200,engine.current())

        def do_POST(self):
            if not self.allowed():
                self.reply(403)
                return
            try:
                length=int(self.headers.get('Content-Length',0))
                if not 0<length<=16384:
                    raise ValueError()
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict):
                    raise ValueError()
            except (ValueError,UnicodeError):
                self.reply(400,{'error':'Invalid request'})
                return
            if self.path.startswith('/session/'):
                token=value.get('authorization')
                if not isinstance(token,str) or not token.startswith('Bearer ') or len(token)<16 or any(c in token for c in '\r\n'):
                    self.reply(400,{'error':'Invalid authorization format'})
                    return
                engine.set_session(token)
                self.reply(202,{'accepted':True,'session_persisted':False})
                return
            if not self.path.startswith('/control/'):
                self.reply(405)
                return
            action=value.get('action')
            if action=='pause':
                engine.pause.set()
            elif action=='resume':
                engine.pause.clear()
            elif action=='retry_failed':
                engine.retry.set()
            elif action=='export':
                engine.export_requested.set()
            elif action=='configure_rate':
                try:
                    engine.configure_rate(value.get('max_rps'))
                except ValueError as exc:
                    self.reply(400,{'error':str(exc)})
                    return
            elif action in ('start_partitions','audit_coverage','pause_partitions','resume_partitions'):
                scope=value.get('scope','all')
                if scope not in ('pilot','all'):
                    self.reply(400,{'error':'scope must be pilot or all'})
                    return
                engine.commands.put({'action':action,'scope':scope})
            elif action=='configure_scheduler':
                mode=value.get('scheduler_mode')
                if mode not in ('balanced','discovery_first'):
                    self.reply(400,{'error':'Invalid scheduler mode'})
                    return
                engine.commands.put({'action':action,'mode':mode})
            elif action=='client_request':
                journal_id, kind = value.get('journal_id'), value.get('kind')
                if not isinstance(journal_id, int) or isinstance(journal_id, bool) or not 0 < journal_id <= 1000000000 or kind not in ('detail', 'related'):
                    self.reply(400, {'error':'Invalid journal request'})
                    return
                engine.commands.put({'action':action,'journal_id':journal_id,'kind':kind})
            elif action=='stop':
                engine.stop.set()
                threading.Thread(target=self.server.shutdown,daemon=True).start()
            else:
                self.reply(400,{'error':'Unknown action'})
                return
            engine.wake.set()
            self.reply(202,{'accepted':True,'action':action})
    return Handler


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'data'/'scholay')
    parser.add_argument('--max-rps',type=float,default=4.0)
    parser.add_argument('--network-mode',choices=('thread','async'),default='thread')
    parser.add_argument('--export-only',action='store_true')
    parser.add_argument('--wait-lock',type=int,default=0)
    args=parser.parse_args()
    root=args.output.resolve()
    if args.export_only:
        print(json.dumps(export_snapshot(root),ensure_ascii=False))
        return
    if not .5<=args.max_rps<=12:
        raise ValueError('Rate envelope is 0.5 to 12 requests per second')
    deadline = time.monotonic() + min(120, max(0, args.wait_lock))
    while True:
        try:
            instance=InstanceLock(root/'collector.lock').acquire()
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.5)
    if args.network_mode=='async':
        import async_runtime  # Fail before starting the service if the dependency is absent.
    engine=Engine(root,args.max_rps,args.network_mode)
    server=None
    try:
        nonce=secrets.token_urlsafe(24)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(engine,nonce))
        address=f'http://127.0.0.1:{server.server_port}'
        atomic_json(root/'handoff.json',{'url':address+'/session/'+nonce,
            'control_url':address+'/control/'+nonce,'status_url':address+'/status/'+nonce})
        (root/'job.pid').write_text(str(os.getpid()),encoding='ascii')
        worker=threading.Thread(target=engine.run,name='journal-queue',daemon=False)
        worker.start()
        print(f'Collector v7 ({args.network_mode}) is ready for an in-memory session',flush=True)
        server.serve_forever()
    finally:
        engine.stop.set()
        engine.wake.set()
        if 'worker' in locals():
            worker.join()
        if server:
            server.server_close()
        instance.close()


if __name__=='__main__':
    main()
