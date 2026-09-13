"""Deterministic paper-engine scenarios; never changes the real paper account."""
import copy
import tempfile
import sys
import threading
import subprocess
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
import paper_trader as p

class FakeFeed:
    def __init__(self):
        self.data={a:dict(asset=a,ticker=f'{a.lower()}-updown-15m-1789263000',close_time=(p.now()+timedelta(minutes=4)).isoformat(),
            received_at=p.now().isoformat(),yes_ask_dollars='0.80',no_ask_dollars='0.22',yes_bid_dollars='0.79',no_bid_dollars='0.20',
            underlying=dict(price='101',open15m='100',delta='1',source='fixture')) for a in ['BTC','ETH']}
        self.results={};self.failed=set()
    def snapshot(self,a,c):
        if a in self.failed:raise OSError('feed unavailable')
        return copy.deepcopy(self.data[a])
    def market(self,ticker):return {'payouts':self.results.get(ticker)}

def run():
    import dashboard
    assert 'https://paper.example.com' in dashboard.allowed_origins('https://paper.example.com')
    for origin in ['http://paper.example.com','https://paper.example.com/path','https://user:pass@paper.example.com','https://paper.example.com?x=1']:
        try:dashboard.allowed_origins(origin)
        except ValueError:pass
        else:raise AssertionError('Unsafe deployment origin accepted')
    import json,sqlite3,time
    raw=dict(slug='btc-updown-15m-1789263000',conditionId='0x'+'a'*64,eventStartTime='2026-09-13T01:30:00Z',endDate='2026-09-13T01:45:00Z',
        outcomes='["Down","Up"]',clobTokenIds='["22","11"]',description='fixture',resolutionSource='https://data.chain.link/streams/btc-usd-twap-60s-streams',
        cryptoMarketConfig={'twapLookbackSeconds':60,'twapEnabled':True})
    m=p.parse_market(raw,'BTC');assert m['tokens']=={'DOWN':'22','UP':'11'}
    try:p.parse_market({**raw,'resolutionSource':'https://example.com/spot'},'BTC')
    except ValueError:pass
    else:raise AssertionError('Wrong resolution source accepted')
    body=dict(market=m['condition_id'],asset_id='11',timestamp=str(int(time.time()*1000)),min_order_size='5',tick_size='.01',
        bids=[{'price':'.1','size':'5'},{'price':'.4','size':'8'}],asks=[{'price':'.9','size':'2'},{'price':'.5','size':'6'},{'price':'.5','size':'4'}])
    p.apply_book(m,body,'UP');assert m['yes_bid_dollars']=='0.4' and m['yes_ask_dollars']=='0.5' and m['yes_ask_size_fp']=='10'
    for bad in [dict(asset_id='wrong'),dict(timestamp='1000'),dict(asks=[{'price':'NaN','size':'1'}])]:
        try:p.apply_book(copy.deepcopy(m),{**body,**bad},'UP')
        except ValueError:pass
        else:raise AssertionError(bad)
    assert p.trade_fee({'fee_rate':'.07'},p.dec(100),p.dec('.5'))==p.dec('1.75000')
    stamp=int(time.time())*1000
    event=dict(topic='crypto_prices_twap_sixty',payload={'symbol':'btc/usd','window_s':60,'timestamp':stamp,'full_accuracy_value':'77233123456789012345678'})
    ticks=p.parse_twap(event);assert ticks==[('BTC',stamp,'77233.123456789012345678')]
    assert p.parse_twap({**event,'topic':'crypto_prices'})==[]
    assert p.parse_twap({**event,'payload':{**event['payload'],'window_s':30}})==[]
    with tempfile.TemporaryDirectory() as folder:
        stream=p.Chainlink.__new__(p.Chainlink);stream.path=Path(folder)/'history.sqlite';stream.error=None
        with sqlite3.connect(stream.path) as db:
            db.execute('CREATE TABLE ticks (asset TEXT,timestamp INTEGER,value TEXT,PRIMARY KEY(asset,timestamp))')
            db.executemany('INSERT INTO ticks VALUES (?,?,?)',ticks)
        opening=p.datetime.fromtimestamp(stamp/1000,p.timezone.utc).isoformat()
        u=stream.underlying('BTC',{'open_time':opening});assert u['open15m']==ticks[0][2] and u['history']['samples']==1
        assert stream.underlying('BTC',{'open_time':(p.parse_time(opening)-timedelta(seconds=1)).isoformat()})['open15m'] is None
    # Closed flag alone or a near-one trading price never establishes a payout.
    f=p.Feed.__new__(p.Feed)
    result={'condition_id':m['condition_id'],'closed':True,'tokens':[{'token_id':'11','winner':False,'price':.999},{'token_id':'22','winner':False,'price':.001}]}
    with patch.object(p,'get_json',side_effect=lambda url:raw if '/markets/slug/' in url else result):
        assert f.market(raw['slug'])['payouts'] is None
        result['tokens'][0]['winner']=True;assert f.market(raw['slug'])['payouts']=={'UP':'1','DOWN':'0'}
        result['is_50_50_outcome']=True;assert f.market(raw['slug'])['payouts']=={'UP':'0.5','DOWN':'0.5'}
    from concurrent.futures import Future
    with tempfile.TemporaryDirectory() as folder,patch.object(p,'DATA',Path(folder)),patch.object(p,'STATE_FILE',Path(folder)/'state.json'):
        c=dict(p.DEFAULTS,size='1');s=p.initial_state('1000');f=FakeFeed();e=p.Engine(s,c,f)
        clock=[p.now()];start=clock[0]
        def decision(action='ENTER_UP',quantity='1',limit_price='.85'):
            return dict(asset='BTC',ticker=f.data['BTC']['ticker'],action=action,quantity=quantity,limit_price=limit_price,reason='fixture')
        def fresh(seconds):
            clock[0]=start+timedelta(seconds=seconds)
            for m in f.data.values():
                m.update(open_time=start.isoformat(),close_time=(start+timedelta(minutes=15)).isoformat(),fee_rate='.07',
                    order_limits={side:{'minimum':'1','tick':'.01'} for side in ['UP','DOWN']},
                    contract_history={'outcomes':{'UP':[{'t':1,'p':'.5'}],'DOWN':[{'t':1,'p':'.5'}]}},
                    received_at=clock[0].isoformat(),yes_ask_size_fp='100',no_ask_size_fp='100',yes_bid_size_fp='100',no_bid_size_fp='100')
                m['underlying'].update(source_at=clock[0].isoformat(),history={'samples':2,'bars':[]})
            e.tick()
        def finish(action='WAIT'):
            e.future.set_result({'decisions':[decision(action)],'reason':'fixture'})
            e.complete_review()
        def apply(d,original=None):e.apply_decision(d,original or e.payload('market_entry_review'))
        with patch.object(p,'now',side_effect=lambda:clock[0]),patch.object(e.pool,'submit',side_effect=lambda *args:Future()) as calls:
            fresh(180);assert calls.call_count==0 and s['paused']
            assert not e.request_review('manual_account_review') and calls.call_count==0
            s['paused']=False;fresh(179);assert calls.call_count==0
            fresh(180);assert calls.call_count==1 and e.future is not None
            assert e.last_codex['payload']['phases']['BTC']=='READY_FOR_REVIEW'
            assert p.load_state('1000')['reviewed_markets']['BTC']==f.data['BTC']['ticker']
            finish();fresh(200);assert calls.call_count==1 and not s['positions'] # WAIT consumes market
            s['paused']=True;s['paused']=False;fresh(220);assert calls.call_count==1
            restored=p.Engine(p.load_state('1000'),c,f);restored.snapshots=copy.deepcopy(e.snapshots)
            assert not restored.review_due('BTC') # restart cannot retry
            restored.pool.shutdown();restored.feed_pool.shutdown()
            # A manual preview never uses the next market's scheduled attempt or executes orders.
            s['reviewed_markets'].clear()
            assert e.request_review('manual_account_review');finish('ENTER_UP');assert not s['positions'] and not s['reviewed_markets']
            fresh(230);assert e.future is not None
            e.future.set_exception(RuntimeError('AI unavailable'));e.complete_review()
            n=calls.call_count;fresh(235);assert calls.call_count==n # errors never retry
            # Missing data through the dispatch window means zero calls, even after data recovers.
            s['reviewed_markets'].clear();f.data['BTC']['underlying']['error']='missing opening'
            fresh(180);assert calls.call_count==n
            f.data['BTC']['underlying'].pop('error');fresh(240);assert calls.call_count==n
            # Preview and pause/resume invalidation cannot grant trading authority.
            fresh(180);original=e.last_codex['payload'];s['paused']=True;finish('ENTER_UP');assert not s['positions']
            s['paused']=False;s['reviewed_markets'].clear();fresh(180);e.epoch+=1;finish('ENTER_UP');assert not s['positions']
            # Entry guards remain active in the shared execution path.
            original=e.payload('market_entry_review');original['at']=(clock[0]-timedelta(seconds=31)).isoformat();apply(decision(),original);assert not s['positions']
            original=e.payload('market_entry_review');original['markets']['BTC']['ticker']='OLD';apply(decision(),original);assert not s['positions']
            apply(decision(),e.payload('manual_account_review'));assert not s['positions']
            apply(decision(quantity='2'));assert not s['positions']
            e.config['max_trade']=p.dec('.1');apply(decision());assert not s['positions'];e.config['max_trade']=p.dec('10')
            e.snapshots['BTC']['yes_ask_size_fp']='0';apply(decision());assert not s['positions']
            fresh(181);e.snapshots['BTC']['underlying']['history']={'samples':0};apply(decision());assert not s['positions']
            fresh(182);e.config['daily_loss']=p.dec('-.01');apply(decision());assert not s['positions'];e.config['daily_loss']=p.dec('-600')
            apply(decision());assert 'BTC' in s['positions']
            cost=p.dec('0.8')+p.trade_fee({'fee_rate':'.07'},p.dec(1),p.dec('.8'));assert p.dec(s['cash'])==1000-cost
            f.data['BTC']['yes_bid_dollars']='.01';fresh(200);assert 'BTC' in s['positions']
            apply(decision('EXIT','0','0'));assert 'BTC' in s['positions'] # no early exit path
            f.data['BTC']['yes_bid_dollars']='.99';fresh(250);assert 'BTC' in s['positions']
            # Official settlement, including while paused, pays exactly once.
            ticker=s['positions']['BTC']['ticker'];s['paused']=True;fresh(900);assert ticker in s['pending']
            f.results[ticker]={'UP':'1','DOWN':'0'};e.settle_checked.clear();fresh(901);assert not s['pending'] and s['trades']==1
            cash=s['cash'];fresh(902);assert s['cash']==cash and p.dec(cash)==1001-cost
            # The next market gets one new review and may enter again.
            start=clock[0];f.data['BTC']['ticker']='btc-updown-15m-1789263900';s['paused']=False
            fresh(180);assert e.future is not None;finish('ENTER_DOWN');assert s['positions']['BTC']['side']=='DOWN'
            n=calls.call_count;fresh(200);assert calls.call_count==n
            s['paused']=True;fresh(900);ticker=next(iter(s['pending']));f.results[ticker]={'UP':'1','DOWN':'0'}
            e.settle_checked.clear();fresh(901);assert s['trades']==2 and s['losses']==1
        # A real local subprocess stands in for Codex; pause must stop it and block previews.
        launched=threading.Event();processes=[];popen=subprocess.Popen
        def fake_codex(*args,**kwargs):
            assert args[0][args[0].index('--model')+1]=='gpt-6-astra'
            assert 'model_reasoning_effort="high"' in args[0]
            process=popen([sys.executable,'-c','import time; time.sleep(60)'],**kwargs)
            processes.append(process);launched.set();return process
        s['paused']=False
        with patch.object(p.subprocess,'Popen',side_effect=fake_codex):
            assert e.request_review('manual_account_review')
            assert launched.wait(3)
            e.pause()
            assert processes[0].poll() is not None and e.future.done()
            e.complete_review()
            assert not e.request_review('manual_account_review')
            assert not e.request_review('market_entry_review')
        e.pool.shutdown(wait=True);e.feed_pool.shutdown(wait=True)
        for bad in [dict(size='NaN'),dict(size='1.001'),dict(daily_loss='1'),dict(codex_interval='60')]:
            try:p.validate({**c,**bad})
            except ValueError:pass
            else:raise AssertionError(bad)
        for actions in [[decision(),decision()],[decision('EXIT')],[decision('HOLD')]]:
            try:p.validate_decisions({'decisions':actions,'reason':'bad'})
            except ValueError:pass
            else:raise AssertionError('Invalid actions accepted')
    from urllib.error import HTTPError
    with patch.object(p.urllib.request,'urlopen',side_effect=HTTPError('https://example.test/book',403,'Forbidden',{},None)) as request:
        for _ in range(2):
            try:p.get_json('https://example.test/book?token_id=test')
            except ValueError as exc:assert 'example.test/book' in str(exc)
            else:raise AssertionError('Blocked requests must not succeed')
        assert request.call_count==1
    p.HTTP_BLOCKED_UNTIL.clear()
    import dashboard as dashboard
    with patch.object(dashboard.subprocess,'run',return_value=subprocess.CompletedProcess([],0)),patch.object(dashboard.time,'monotonic',return_value=100):
        dashboard.refresh_login();assert dashboard.login['authenticated']
    with patch.object(dashboard.subprocess,'run',return_value=subprocess.CompletedProcess([],1)),patch.object(dashboard.time,'monotonic',return_value=200):
        dashboard.refresh_login();assert not dashboard.login['authenticated']
    print('Passed parsing, once-per-market scheduling, restart/error/preview isolation, entry limits and hold-to-settlement accounting')

if __name__=='__main__':run()
