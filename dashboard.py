"""Local UI and lifecycle owner for the Polymarket paper engine."""
import copy
import fcntl
import json
import os
import random
import subprocess
import threading
import time
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import paper_trader as p

lock=threading.Lock()
idle=threading.Condition(lock)
view={'busy':False,'error':None,'updated':None}
login={'running':False,'output':'','authenticated':False,'status':'Checking Codex login','checked_at':0}

def refresh_login():
    if login['running'] or time.monotonic()-login['checked_at']<60:return
    try:
        result=subprocess.run(['codex','login','status'],capture_output=True,text=True,timeout=5)
        login.update(authenticated=result.returncode==0,status='Codex connected' if result.returncode==0 else 'Codex sign-in required')
    except (OSError,subprocess.TimeoutExpired):
        login.update(authenticated=False,status='Could not check Codex login')
    login['checked_at']=time.monotonic()


def codex_login():
    try:
        process=subprocess.Popen(['codex','login','--device-auth'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        timer=threading.Timer(600,process.kill);timer.start()
        try:
            for line in process.stdout:
                with lock:login['output']=(login['output']+line)[-4000:]
            process.wait()
        finally:timer.cancel()
    except Exception:
        with lock:login['output']='Login failed. Retry login.'
    finally:
        with lock:
            login['running']=False;login['checked_at']=0;refresh_login()

def allowed_origins(public_origin):
    origins=[None,'http://127.0.0.1:8765','http://localhost:8765']
    if public_origin:
        url=urlsplit(public_origin)
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.port not in [None,443] or url.path or url.query or url.fragment:
            raise ValueError('PUBLIC_ORIGIN must be an exact HTTPS origin without a path or credentials')
        origins.append(public_origin)
    return origins

ORIGINS=allowed_origins(os.environ.get('PUBLIC_ORIGIN',''))

def publish():
    view.update(state=copy.deepcopy(engine.state),account=p.account(engine.state),
                markets=copy.deepcopy(engine.snapshots),errors=copy.deepcopy(engine.errors),
                codex=copy.deepcopy(engine.last_codex))

def worker():
    while True:
        with lock:view['busy']=True
        try:
            engine.tick()
            with lock:view.update(error=None,updated=p.now().isoformat())
        except Exception as exc:
            engine.pause()
            with lock:view['error']=str(exc)
        finally:
            with lock:
                publish();view['busy']=False;idle.notify_all()
        time.sleep(float(engine.config['interval'])+random.uniform(0,float(engine.config['jitter'])))

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,status,data,mime='application/json'):
        body=data if isinstance(data,bytes) else json.dumps(data,default=str).encode()
        self.send_response(status);self.send_header('Content-Type',mime)
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def local(self):return self.headers.get('Host') in ['127.0.0.1:8765','localhost:8765']
    def do_GET(self):
        if not self.local():return self.send(403,{'error':'Local requests only'})
        if self.path=='/api/codex/login':
            with lock:
                refresh_login()
                return self.send(200,dict(login))
        if self.path=='/':return self.send(200,(p.ROOT/'dashboard.html').read_bytes(),'text/html; charset=utf-8')
        if self.path=='/dashboard.js':return self.send(200,(p.ROOT/'dashboard.js').read_bytes(),'text/javascript; charset=utf-8')
        if self.path=='/api/status':
            with lock:
                data=copy.deepcopy(view);data['settings']=settings;data['paused']=engine.state['paused']
                data['state']['events']=data['state']['events'][-300:]
            return self.send(200,data)
        if self.path=='/api/history':return self.send(200,json.loads(p.STATE_FILE.read_text())['events'])
        self.send(404,{'error':'Not found'})
    def do_POST(self):
        global settings
        if not self.local() or self.headers.get('Origin') not in ORIGINS or self.headers.get('Content-Type')!='application/json':
            return self.send(403,{'error':'Local JSON requests only'})
        try:
            size=int(self.headers.get('Content-Length',0))
            if not 0<size<12000:raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(size))
            with lock:
                s=engine.state
                if self.path=='/api/codex/login':
                    if not s['paused'] or engine.future or login['running']:raise ValueError('Pause and wait for the current review or login')
                    login.update(running=True,output='Starting separate cloud Codex login...')
                    threading.Thread(target=codex_login,daemon=True).start()
                elif self.path=='/api/pause':engine.pause()
                elif self.path=='/api/start':
                    if view['error']:raise ValueError('Resolve the worker error before starting')
                    if s['halted']:raise ValueError(s['halted'])
                    if not idle.wait_for(lambda:not view['busy'],timeout=30):raise ValueError('Wait for the current data refresh')
                    for asset in engine.config['assets']:
                        if not engine.ready(asset,history=True):raise ValueError(f'{asset}: fresh Polymarket books, Chainlink TWAP, opening tick and history must be available')
                    if body.get('resume_run') is True:
                        if not s.get('run_until') or p.now()>=p.parse_time(s['run_until']):raise ValueError('The previous run has ended')
                        s['paused']=False;engine.epoch+=1;engine.expire_run()
                    else:engine.start_run(body.get('duration_hours',12),body.get('profit_target_percent',0))
                elif self.path=='/api/review':
                    if not idle.wait_for(lambda:not view['busy'],timeout=30) or engine.future:raise ValueError('A feed update or Codex review is running. Try again shortly.')
                    if not engine.request_review('manual_account_review'):raise ValueError('Resume paper trading before requesting an AI review')
                elif self.path=='/api/settings':
                    if not idle.wait_for(lambda:not view['busy'],timeout=30) or not s['paused'] or engine.future or s['positions'] or s['pending']:
                        raise ValueError('Pause, wait for open positions/settlements and the current review to finish, then save.')
                    checked=p.validate(body)
                    if checked['balance']!=p.dec(s['initial_balance']):
                        if s['trades']:raise ValueError('Starting cash cannot change after trading begins')
                        s['initial_balance']=s['cash']=str(checked['balance'])
                    p.atomic_json(p.SETTINGS_FILE,body)
                    settings=body;engine.config=checked;engine.epoch+=1
                    engine.snapshots={};engine.errors={}
                    if s['halted'] and not engine.limits():s['halted']=None
                else:raise ValueError('Unknown action')
                if not view['busy']:p.save_state(s);publish()
            self.send(200,{'ok':True})
        except (ValueError,KeyError,TypeError) as exc:self.send(400,{'error':str(exc)})

if __name__=='__main__':
    p.DATA.mkdir(parents=True,exist_ok=True)
    process_lock=(p.DATA/'paper_trader.lock').open('w')
    try:fcntl.flock(process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('A paper trader is already running')
    settings=copy.deepcopy(p.DEFAULTS)
    if p.SETTINGS_FILE.exists():
        previous=json.loads(p.SETTINGS_FILE.read_text())
        if 'assets' in previous:settings.update({k:v for k,v in previous.items() if k in settings})
        else:
            settings.update({k:v for k,v in previous.items() if k in settings})
            settings['interval']=p.DEFAULTS['interval']
            settings['assets']=[previous.get('series','KXBTC15M')[2:-3]]
    settings['assets']=['BTC']
    engine=p.Engine(p.load_state(settings['balance']),settings)
    engine.pause()
    p.save_state(engine.state);p.atomic_json(p.SETTINGS_FILE,settings);publish()
    threading.Thread(target=worker,daemon=True).start()
    print('Paper console: http://127.0.0.1:8765',flush=True)
    try:ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()
    except KeyboardInterrupt:engine.pause()
