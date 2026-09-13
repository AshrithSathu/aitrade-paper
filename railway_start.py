"""Run the private dashboard and tunnel together; restart if either exits."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


def main():
    data=Path('/app/data');data.mkdir(parents=True,exist_ok=True)
    if os.getuid()==0:
        os.chown(data,1000,1000)
        os.setgid(1000);os.setuid(1000)
    os.environ['HOME']='/home/node'
    os.environ['CODEX_HOME']=str(data/'codex')
    Path(os.environ['CODEX_HOME']).mkdir(mode=0o700,exist_ok=True)
    token=os.environ.pop('TUNNEL_TOKEN')
    children=[]
    def stop(*_):raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    with tempfile.NamedTemporaryFile(mode='w') as secret:
        secret.write(token);secret.flush();del token
        try:
            children.append(subprocess.Popen(['python3','dashboard.py']))
            children.append(subprocess.Popen(['cloudflared','tunnel','--no-autoupdate','run','--token-file',secret.name]))
            while all(p.poll() is None for p in children):time.sleep(1)
            raise SystemExit('Dashboard or tunnel exited; restarting service')
        finally:
            for p in children:
                if p.poll() is None:p.terminate()
            for p in children:
                try:p.wait(timeout=10)
                except subprocess.TimeoutExpired:p.kill();p.wait()

if __name__=='__main__':main()
