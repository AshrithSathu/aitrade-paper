"""Deterministic paper-engine scenarios; never changes the real paper account."""
import copy
import tempfile
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
    with tempfile.TemporaryDirectory() as folder,patch.object(p,'DATA',Path(folder)),patch.object(p,'STATE_FILE',Path(folder)/'state.json'),patch.object(p,'codex_decision',return_value={'decisions':[],'reason':'fixture'}):
        c=dict(p.DEFAULTS,size='1');s=p.initial_state('1000');f=FakeFeed();e=p.Engine(s,c,f)
        def fresh():
            for m in f.data.values():
                m.update(fee_rate='.07',order_limits={side:{'minimum':'1','tick':'.01'} for side in ['UP','DOWN']},contract_history={'outcomes':{'UP':[{'t':1,'p':'.5'}],'DOWN':[{'t':1,'p':'.5'}]}},received_at=p.now().isoformat(),yes_ask_size_fp='100',no_ask_size_fp='100',yes_bid_size_fp='100',no_bid_size_fp='100')
                m['underlying'].update(source_at=p.now().isoformat(),history={'samples':2,'bars':[]})
            e.tick()
        def decision(action='ENTER_UP',quantity='1',limit_price='.85'):
            return dict(asset='BTC',ticker=f.data['BTC']['ticker'],action=action,quantity=quantity,limit_price=limit_price,reason='fixture')
        def apply(d,original=None):
            p.validate_decisions({'decisions':[d],'reason':'fixture'})
            e.apply_decision(d,original or e.payload('test'))
        fresh();assert not s['positions'] and s['paused']
        # Rules never enter, even when old entry conditions would have matched.
        fresh();assert not s['positions']
        s['paused']=False;e.sent_at=p.time.monotonic()
        apply(decision());assert 'BTC' in s['positions']
        # No price-based stop or take-profit: only AI closes the position.
        f.data['BTC']['yes_bid_dollars']='.1';fresh();assert 'BTC' in s['positions']
        f.data['BTC']['yes_bid_dollars']='.99';fresh();assert 'BTC' in s['positions']
        apply(decision('HOLD','0','0'));assert 'BTC' in s['positions']
        apply(decision('EXIT','0','.995'));assert 'BTC' in s['positions']
        apply(decision('EXIT','0','.9'));assert not s['positions'] and s['trades']==1
        # AI can re-enter the same market; there is no one-trade-per-market rule.
        apply(decision('ENTER_DOWN'));assert s['positions']['BTC']['side']=='DOWN'
        apply(decision('EXIT','0','0'));assert s['trades']==2
        # No entry window or fixed bands.
        f.data['BTC']['close_time']=(p.now()+timedelta(minutes=14)).isoformat()
        f.data['BTC']['yes_ask_dollars']='.3';fresh();apply(decision(limit_price='.4'));assert 'BTC' in s['positions']
        # Changed position, stale review and market rollover reject old decisions.
        original=e.payload('test');original['positions']['BTC']['opened_at']='old'
        apply(decision('EXIT','0','0'),original);assert 'BTC' in s['positions']
        apply(decision('EXIT','0','0'));assert not s['positions']
        original=e.payload('test');original['at']=(p.now()-timedelta(seconds=31)).isoformat()
        apply(decision(),original);assert not s['positions']
        original=e.payload('test');original['markets']['BTC']['ticker']='OLD'
        apply(decision(),original);assert not s['positions']
        # Spend, quantity, depth, history and cumulative worst-case loss controls.
        apply(decision(quantity='2'));assert not s['positions']
        e.config['max_trade']=p.dec('.1');apply(decision());assert not s['positions'];e.config['max_trade']=p.dec('10')
        e.snapshots['BTC']['underlying']['history']={'error':'missing'};apply(decision());assert not s['positions']
        fresh();e.snapshots['BTC']['yes_ask_size_fp']='0';apply(decision());assert not s['positions']
        fresh();saved_pnl=s['realized_pnl'];s['realized_pnl']='0';e.config['daily_loss']=p.dec('-.01');apply(decision());assert not s['positions'];s['realized_pnl']=saved_pnl
        e.config['daily_loss']=p.dec('-600');apply(decision());assert 'BTC' in s['positions']
        # Settlement remains exchange-defined, even when paused, and pays only once.
        ticker=s['positions']['BTC']['ticker'];s['positions']['BTC']['close_time']=(p.now()-timedelta(seconds=1)).isoformat();s['paused']=True
        fresh();assert ticker in s['pending']
        f.results[ticker]={'UP':'1','DOWN':'0'};e.settle_checked.clear();fresh();assert ticker not in s['pending']
        cash=s['cash'];fresh();assert s['cash']==cash and p.load_state('1000')==s
        # Async preview and pause invalidate trading authority.
        from concurrent.futures import Future
        def completed(execute,epoch):
            original=e.payload('test');original['execution_allowed']=execute
            e.last_codex={'status':'running','payload':original,'response':None};e.review_path=Path(folder)/'review.json'
            e.review_epoch=epoch;e.future=Future();e.future.set_result({'decisions':[decision()],'reason':'fixture'})
            e.complete_review()
        completed(True,e.epoch);assert not s['positions'] # paused
        s['paused']=False
        completed(False,e.epoch);assert not s['positions'] # manual preview
        completed(True,e.epoch-1);assert not s['positions'] # paused then resumed
        completed(True,e.epoch);assert 'BTC' in s['positions']
        # WAIT can be reviewed again on the next configured interval.
        apply(decision('EXIT','0','0'))
        e.sent_at=0;fresh();assert e.future is not None
        e.pool.shutdown(wait=True);e.complete_review();assert not s['positions']
        e.feed_pool.shutdown(wait=True)
        for bad in [dict(size='NaN'),dict(size='1.001'),dict(daily_loss='1'),dict(codex_mode='gate')]:
            try:p.validate({**c,**bad})
            except ValueError:pass
            else:raise AssertionError(bad)
        try:p.validate_decisions({'decisions':[decision(),decision()],'reason':'bad'})
        except ValueError:pass
        else:raise AssertionError('Duplicate actions accepted')
    print('Passed Polymarket public data parsing; AI-only enter/hold/exit, repeated reviews, limits, stale decisions, pause/preview isolation and settlement')

if __name__=='__main__':run()
