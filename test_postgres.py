"""Run only against a disposable PostgreSQL database: DATABASE_URL=... python3 test_postgres.py."""
import tempfile,sqlite3,time
from pathlib import Path
from unittest.mock import patch
import paper_trader as p

with tempfile.TemporaryDirectory() as folder,patch.object(p,'DATA',Path(folder)):
    cutoff=int((time.time()-86400)*1000)
    current=int(time.time()*1000)
    with sqlite3.connect(Path(folder)/'chainlink.sqlite') as db:
        db.execute('CREATE TABLE ticks(asset TEXT,timestamp INTEGER,value TEXT)')
        db.executemany('INSERT INTO ticks VALUES (?,?,?)',[('BTC',cutoff-1,'1'),('BTC',cutoff,'2'),('BTC',current,'3')])
    store=p.TickStore()
    rows,opening=store.window('BTC',current)
    assert rows[-1]==(current,'3') and opening==('3',)
    context=store.context24('BTC');assert context['requested_hours']==24 and context['bars'][-1]['close']=='3'
    store.append([('BTC',current,'3')])
    assert store.prune(cutoff)==1 and store.prune(cutoff)==0
    store.close()
    restored=p.TickStore()
    assert restored.prune(cutoff)==0 # migration marker prevents resurrecting expired SQLite data
    assert restored.window('BTC',current)[1]==('3',)
    assert (Path(folder)/'chainlink.sqlite').exists()
    restored.close()
print('PostgreSQL migration, duplicate ingestion, retention boundary and restart checks passed')
