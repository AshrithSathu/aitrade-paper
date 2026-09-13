"""Deterministic paper-engine scenarios; never changes the real paper account."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend import ai, common, feeds, market, storage
from backend import engine as trading


class FakeFeed:
    def __init__(self):
        self.data = {
            a: dict(
                asset=a,
                ticker=f"{a.lower()}-updown-15m-1789263000",
                close_time=(common.now() + timedelta(minutes=4)).isoformat(),
                received_at=common.now().isoformat(),
                yes_ask_dollars="0.80",
                no_ask_dollars="0.22",
                yes_bid_dollars="0.79",
                no_bid_dollars="0.20",
                underlying=dict(
                    price="101", opening_price="100", delta="1", source="fixture"
                ),
            )
            for a in ["BTC", "ETH"]
        }
        self.results = {}
        self.failed = set()

    def snapshot(self, a, c):
        if a in self.failed:
            raise OSError("feed unavailable")
        return copy.deepcopy(self.data[a])

    def market(self, ticker):
        return {"payouts": self.results.get(ticker)}


def run():
    from backend import dashboard

    assert "https://paper.example.com" in dashboard.allowed_origins(
        "https://paper.example.com"
    )
    for origin in [
        "http://paper.example.com",
        "https://paper.example.com/path",
        "https://user:pass@paper.example.com",
        "https://paper.example.com?x=1",
    ]:
        try:
            dashboard.allowed_origins(origin)
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe deployment origin accepted")
    import json
    import sqlite3
    import time

    raw = dict(
        slug="btc-updown-15m-1789263000",
        conditionId="0x" + "a" * 64,
        eventStartTime="2026-09-13T01:30:00Z",
        endDate="2026-09-13T01:45:00Z",
        outcomes='["Down","Up"]',
        clobTokenIds='["22","11"]',
        description="fixture",
        resolutionSource="https://data.chain.link/streams/btc-usd-twap-60s-streams",
        cryptoMarketConfig={"twapLookbackSeconds": 60, "twapEnabled": True},
    )
    five = {
        **raw,
        "slug": "btc-updown-5m-1789263000",
        "endDate": "2026-09-13T01:35:00Z",
    }
    assert market.parse_market(five, "BTC")["market_minutes"] == 5
    m = market.parse_market(raw, "BTC")
    assert m["tokens"] == {"DOWN": "22", "UP": "11"}
    try:
        market.parse_market(
            {**raw, "resolutionSource": "https://example.com/spot"}, "BTC"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Wrong resolution source accepted")
    body = dict(
        market=m["condition_id"],
        asset_id="11",
        timestamp=str(int(time.time() * 1000)),
        min_order_size="5",
        tick_size=".01",
        bids=[{"price": ".1", "size": "5"}, {"price": ".4", "size": "8"}],
        asks=[
            {"price": ".9", "size": "2"},
            {"price": ".5", "size": "6"},
            {"price": ".5", "size": "4"},
        ],
    )
    market.apply_book(m, body, "UP")
    assert (
        m["yes_bid_dollars"] == "0.4"
        and m["yes_ask_dollars"] == "0.5"
        and m["yes_ask_size_fp"] == "10"
    )
    for bad in [
        dict(asset_id="wrong"),
        dict(timestamp="1000"),
        dict(asks=[{"price": "NaN", "size": "1"}]),
    ]:
        try:
            market.apply_book(copy.deepcopy(m), {**body, **bad}, "UP")
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    stream = feeds.BookStream.__new__(feeds.BookStream)
    stream.market = copy.deepcopy(m)
    stream.books = {}
    stream.metadata = {"UP": body}
    stream.error = None
    stream.update(
        {
            **body,
            "timestamp": str(int(time.time() * 1000) - 10000),
            "event_type": "book",
        }
    )
    assert stream.books and stream.error is None
    try:
        market.apply_book(copy.deepcopy(m), stream.books["UP"], "UP")
    except ValueError:
        pass
    else:
        raise AssertionError("Stale stream book became tradable")
    stream.update({**body, "event_type": "book"})
    stream.update(
        {
            "event_type": "price_change",
            "market": m["condition_id"],
            "timestamp": body["timestamp"],
            "price_changes": [
                {
                    "asset_id": "11",
                    "side": "BUY",
                    "price": ".4",
                    "size": "0",
                    "best_bid": ".1",
                    "best_ask": ".5",
                }
            ],
        }
    )
    assert all(
        common.dec(row["price"]) != common.dec(".4")
        for row in stream.books["UP"]["bids"]
    )
    stream.update({"error": "disconnected"})
    assert not stream.books
    stream.update(
        {
            "event_type": "price_change",
            "market": m["condition_id"],
            "timestamp": body["timestamp"],
            "price_changes": [
                {"asset_id": "11", "side": "BUY", "price": ".3", "size": "9"}
            ],
        }
    )
    assert not stream.books  # deltas cannot seed a book after disconnect
    stream.update({**body, "event_type": "book"})
    try:
        stream.update(
            {
                "event_type": "price_change",
                "market": m["condition_id"],
                "timestamp": "1",
                "price_changes": [
                    {"asset_id": "11", "side": "BUY", "price": ".3", "size": "9"}
                ],
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Out-of-order delta accepted")
    end = int(time.time() // 60) * 60000
    signal_market = copy.deepcopy(m)
    signal_market.update(
        fee_rate=".07", close_time=(common.now() + timedelta(minutes=10)).isoformat()
    )
    signal_market["underlying"] = dict(
        source_at=datetime.fromtimestamp(end / 1000, timezone.utc).isoformat(),
        delta="-2",
        history=dict(
            first_at=end - 30 * 60000,
            max_gap_ms=1000,
            bars=[
                dict(
                    t=end - (30 - i) * 60000,
                    samples=60,
                    open=str(100 + i),
                    close=str(100 + i),
                    high=str(100 + i),
                    low=str(100 + i),
                )
                for i in range(31)
            ],
        ),
    )
    signal_market["raw_market"] = {"irrelevant": "provider dump"}
    brief = ai.market_brief(signal_market)
    assert "raw_market" not in brief and "orderbook" not in brief
    assert brief["depth"]["outcomes"]["UP"]["asks"]["levels"][0] == ["0.5", "10"]
    assert brief["depth"]["outcomes"]["UP"]["asks"]["total_contracts"] == "12"
    assert (
        len(brief["history"]["recent_1m"]) == 31
        and brief["ticker"] == signal_market["ticker"]
    )
    deep = copy.deepcopy(signal_market)
    deep["orderbook"]["UP"]["asks"] = [
        {"price": str(common.dec(".5") + common.dec(i) / 100), "size": "1"}
        for i in range(20)
    ]
    deep["contract_history"] = {
        "outcomes": {"UP": [{"t": i, "p": ".5"} for i in range(61)]}
    }
    detailed = ai.market_brief(deep)
    assert len(detailed["depth"]["outcomes"]["UP"]["asks"]["levels"]) == 10
    assert detailed["depth"]["outcomes"]["UP"]["asks"]["omitted_levels"] == 10
    assert detailed["depth"]["outcomes"]["UP"]["asks"]["total_contracts"] == "20"
    assert len(detailed["contract_history"]["outcomes"]["UP"]) == 61
    signals = market.trading_signals(signal_market)
    assert (
        signals["windows"]["5m"]["complete"]
        and not signals["windows"]["60m"]["complete"]
    )
    assert (
        signals["rsi14_simple_closed_minutes"] == 100
        and signals["opening_delta_usd"] == "-2"
    )
    assert common.dec(signals["books"]["UP"]["breakeven_win_probability"]) > common.dec(
        ".5"
    )
    assert market.trade_fee(
        {"fee_rate": ".07"}, common.dec(100), common.dec(".5")
    ) == common.dec("1.75000")
    stamp = int(time.time()) * 1000
    event = dict(
        topic="crypto_prices_twap_sixty",
        payload={
            "symbol": "btc/usd",
            "window_s": 60,
            "timestamp": stamp,
            "full_accuracy_value": "77233123456789012345678",
        },
    )
    ticks = market.parse_twap(event)
    assert ticks == [("BTC", stamp, "77233.123456789012345678")]
    assert market.parse_twap({**event, "topic": "crypto_prices"}) == []
    assert (
        market.parse_twap({**event, "payload": {**event["payload"], "window_s": 30}})
        == []
    )
    with tempfile.TemporaryDirectory() as folder:
        stream = feeds.Chainlink.__new__(feeds.Chainlink)
        stream.path = Path(folder) / "history.sqlite"
        stream.error = None
        from types import SimpleNamespace

        def window(asset, start):
            with sqlite3.connect(stream.path) as db:
                return db.execute(
                    "SELECT timestamp,value FROM ticks WHERE asset=? ORDER BY timestamp",
                    (asset,),
                ).fetchall(), db.execute(
                    "SELECT value FROM ticks WHERE asset=? AND timestamp=?",
                    (asset, start),
                ).fetchone()

        stream.history = SimpleNamespace(
            window=window, context24=lambda asset: {"requested_hours": 24, "bars": []}
        )
        with sqlite3.connect(stream.path) as db:
            db.execute(
                "CREATE TABLE ticks (asset TEXT,timestamp INTEGER,value TEXT,PRIMARY KEY(asset,timestamp))"
            )
            db.executemany("INSERT INTO ticks VALUES (?,?,?)", ticks)
        opening = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
        u = stream.underlying("BTC", {"open_time": opening})
        assert u["opening_price"] == ticks[0][2] and u["history"]["samples"] == 1
        assert (
            stream.underlying(
                "BTC",
                {
                    "open_time": (
                        common.parse_time(opening) - timedelta(seconds=1)
                    ).isoformat()
                },
            )["opening_price"]
            is None
        )
    # Retention only expires old prices/reviews, preserving boundaries and account/login files.
    import os
    import uuid

    with (
        tempfile.TemporaryDirectory() as folder,
        patch.object(common, "DATA", Path(folder)),
    ):
        root = Path(folder)
        at = time.time()
        cutoff = int((at - 86400) * 1000)
        with sqlite3.connect(root / "chainlink.sqlite") as db:
            db.execute(
                "CREATE TABLE ticks (asset TEXT,timestamp INTEGER,value TEXT,PRIMARY KEY(asset,timestamp))"
            )
            db.executemany(
                "INSERT INTO ticks VALUES (?,?,?)",
                [
                    ("BTC", cutoff - 1, "1"),
                    ("BTC", cutoff, "2"),
                    ("BTC", int(at * 1000), "3"),
                ],
            )
        reviews = root / "codex-reviews"
        reviews.mkdir()
        old_at = at - 31 * 86400
        old = dict(
            status="complete",
            payload={"at": datetime.fromtimestamp(old_at, timezone.utc).isoformat()},
        )
        expired = reviews / (str(uuid.uuid4()) + ".json")
        expired.write_text(json.dumps(old))
        os.utime(expired, (old_at, old_at))
        active = reviews / (str(uuid.uuid4()) + ".json")
        active.write_text(json.dumps({**old, "status": "running"}))
        os.utime(active, (old_at, old_at))
        recent = reviews / (str(uuid.uuid4()) + ".json")
        recent.write_text(json.dumps(old))
        for name in [
            "state.json",
            "codex-latest.json",
            "auth.json",
            "state-backup.json",
        ]:
            (root / name).write_text("preserve")
        result = storage.prune_storage(at, active)
        assert result == {"removed_ticks": 0, "removed_reviews": 1}
        assert not expired.exists() and active.exists() and recent.exists()
        for name in [
            "state.json",
            "codex-latest.json",
            "auth.json",
            "state-backup.json",
        ]:
            assert (root / name).read_text() == "preserve"
        with sqlite3.connect(root / "chainlink.sqlite") as db:
            assert db.execute("SELECT count(*) FROM ticks").fetchone()[0] == 3
        assert storage.prune_storage(at, active) == {
            "removed_ticks": 0,
            "removed_reviews": 0,
        }
    # Closed flag alone or a near-one trading price never establishes a payout.
    f = feeds.Feed.__new__(feeds.Feed)
    result = {
        "condition_id": m["condition_id"],
        "closed": True,
        "tokens": [
            {"token_id": "11", "winner": False, "price": 0.999},
            {"token_id": "22", "winner": False, "price": 0.001},
        ],
    }
    with patch.object(
        feeds,
        "get_json",
        side_effect=lambda url: raw if "/markets/slug/" in url else result,
    ):
        assert f.market(raw["slug"])["payouts"] is None
        result["tokens"][0]["winner"] = True
        assert f.market(raw["slug"])["payouts"] == {"UP": "1", "DOWN": "0"}
        result["is_50_50_outcome"] = True
        assert f.market(raw["slug"])["payouts"] == {"UP": "0.5", "DOWN": "0.5"}
    from concurrent.futures import Future

    with (
        tempfile.TemporaryDirectory() as folder,
        patch.object(common, "DATA", Path(folder)),
        patch.object(common, "STATE_FILE", Path(folder) / "state.json"),
    ):
        c = dict(common.DEFAULTS, size="1")
        s = common.initial_state("1000")
        f = FakeFeed()
        e = trading.Engine(s, c, f)
        clock = [common.now()]
        start = clock[0]

        def decision(action="ENTER_UP", quantity="1", limit_price=".85"):
            return dict(
                asset="BTC",
                ticker=f.data["BTC"]["ticker"],
                action=action,
                quantity=quantity,
                limit_price=limit_price,
                reason="fixture",
            )

        def fresh(seconds):
            clock[0] = start + timedelta(seconds=seconds)
            for m in f.data.values():
                m.update(
                    open_time=start.isoformat(),
                    close_time=(start + timedelta(minutes=15)).isoformat(),
                    fee_rate=".07",
                    order_limits={
                        side: {"minimum": "1", "tick": ".01"} for side in ["UP", "DOWN"]
                    },
                    contract_history={
                        "outcomes": {
                            "UP": [{"t": 1, "p": ".5"}],
                            "DOWN": [{"t": 1, "p": ".5"}],
                        }
                    },
                    received_at=clock[0].isoformat(),
                    yes_ask_size_fp="100",
                    no_ask_size_fp="100",
                    yes_bid_size_fp="100",
                    no_bid_size_fp="100",
                )
                m["underlying"].update(
                    source_at=clock[0].isoformat(), history={"samples": 2, "bars": []}
                )
            e.tick()

        def finish(action="WAIT"):
            e.future.set_result({"decisions": [decision(action)], "reason": "fixture"})
            e.complete_review()

        def apply(d, original=None):
            e.apply_decision(d, original or e.payload("market_entry_review"))

        with (
            patch.object(common, "now", side_effect=lambda: clock[0]),
            patch.object(e.pool, "submit", side_effect=lambda *args: Future()) as calls,
        ):
            fresh(180)
            assert calls.call_count == 0 and s["paused"]
            assert (
                not e.request_review("manual_account_review") and calls.call_count == 0
            )
            s["paused"] = False
            fresh(179)
            assert calls.call_count == 0
            fresh(180)
            assert calls.call_count == 1 and e.future is not None
            assert e.last_codex["payload"]["phases"]["BTC"] == "READY_FOR_REVIEW"
            assert (
                common.load_state("1000")["reviewed_markets"]["BTC"]
                == f.data["BTC"]["ticker"]
            )
            finish()
            fresh(200)
            assert calls.call_count == 1 and not s["positions"]  # WAIT consumes market
            s["paused"] = True
            s["paused"] = False
            fresh(220)
            assert calls.call_count == 1
            restored = trading.Engine(common.load_state("1000"), c, f)
            restored.snapshots = copy.deepcopy(e.snapshots)
            assert not restored.review_due("BTC")  # restart cannot retry
            restored.pool.shutdown()
            restored.feed_pool.shutdown()
            # A manual preview never uses the next market's scheduled attempt or executes orders.
            s["reviewed_markets"].clear()
            assert e.request_review("manual_account_review")
            finish("ENTER_UP")
            assert not s["positions"] and not s["reviewed_markets"]
            fresh(230)
            assert e.future is not None
            e.future.set_exception(RuntimeError("AI unavailable"))
            e.complete_review()
            n = calls.call_count
            fresh(235)
            assert calls.call_count == n  # errors never retry
            # Missing data through the dispatch window means zero calls, even after data recovers.
            s["reviewed_markets"].clear()
            f.data["BTC"]["underlying"]["error"] = "missing opening"
            fresh(180)
            assert calls.call_count == n
            f.data["BTC"]["underlying"].pop("error")
            fresh(240)
            assert calls.call_count == n
            # Preview and pause/resume invalidation cannot grant trading authority.
            fresh(180)
            original = e.last_codex["payload"]
            s["paused"] = True
            finish("ENTER_UP")
            assert not s["positions"]
            s["paused"] = False
            s["reviewed_markets"].clear()
            fresh(180)
            e.epoch += 1
            finish("ENTER_UP")
            assert not s["positions"]
            # Entry guards remain active in the shared execution path.
            original = e.payload("market_entry_review")
            original["at"] = (clock[0] - timedelta(seconds=31)).isoformat()
            apply(decision(), original)
            assert not s["positions"]
            original = e.payload("market_entry_review")
            original["markets"]["BTC"]["ticker"] = "OLD"
            apply(decision(), original)
            assert not s["positions"]
            apply(decision(), e.payload("manual_account_review"))
            assert not s["positions"]
            apply(decision(quantity="2"))
            assert not s["positions"]
            e.config["max_trade"] = common.dec(".1")
            apply(decision())
            assert not s["positions"]
            e.config["max_trade"] = common.dec("10")
            e.snapshots["BTC"]["yes_ask_size_fp"] = "0"
            apply(decision())
            assert not s["positions"]
            fresh(181)
            e.snapshots["BTC"]["underlying"]["history"] = {"samples": 0}
            apply(decision())
            assert not s["positions"]
            fresh(182)
            e.config["daily_loss"] = common.dec("-.01")
            apply(decision())
            assert not s["positions"]
            e.config["daily_loss"] = common.dec("-600")
            apply(decision())
            assert "BTC" in s["positions"]
            cost = common.dec("0.8") + market.trade_fee(
                {"fee_rate": ".07"}, common.dec(1), common.dec(".8")
            )
            assert common.dec(s["cash"]) == 1000 - cost
            f.data["BTC"]["yes_bid_dollars"] = ".01"
            fresh(200)
            assert "BTC" in s["positions"]
            apply(decision("EXIT", "0", "0"))
            assert "BTC" in s["positions"]  # no early exit path
            f.data["BTC"]["yes_bid_dollars"] = ".99"
            fresh(250)
            assert "BTC" in s["positions"]
            # Official settlement, including while paused, pays exactly once.
            ticker = s["positions"]["BTC"]["ticker"]
            s["paused"] = True
            fresh(900)
            assert ticker in s["pending"]
            f.results[ticker] = {"UP": "1", "DOWN": "0"}
            e.settle_checked.clear()
            fresh(901)
            assert not s["pending"] and s["trades"] == 1
            cash = s["cash"]
            fresh(902)
            assert s["cash"] == cash and common.dec(cash) == 1001 - cost
            # The next market gets one new review and may enter again.
            start = clock[0]
            f.data["BTC"]["ticker"] = "btc-updown-15m-1789263900"
            s["paused"] = False
            fresh(180)
            assert e.future is not None
            finish("ENTER_DOWN")
            assert s["positions"]["BTC"]["side"] == "DOWN"
            n = calls.call_count
            fresh(200)
            assert calls.call_count == n
            s["paused"] = True
            fresh(900)
            ticker = next(iter(s["pending"]))
            f.results[ticker] = {"UP": "1", "DOWN": "0"}
            e.settle_checked.clear()
            fresh(901)
            assert s["trades"] == 2 and s["losses"] == 1
        # A real local subprocess stands in for Codex; pause must stop it and block previews.
        launched = threading.Event()
        processes = []
        popen = subprocess.Popen

        def fake_codex(*args, **kwargs):
            assert args[0][args[0].index("--model") + 1] == "gpt-6-astra"
            assert 'model_reasoning_effort="low"' in args[0]
            process = popen(
                [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
            )
            processes.append(process)
            launched.set()
            return process

        s["paused"] = False
        with patch.object(subprocess, "Popen", side_effect=fake_codex):
            assert e.request_review("manual_account_review")
            assert launched.wait(3)
            e.pause()
            assert processes[0].poll() is not None and e.future.done()
            e.complete_review()
            assert not e.request_review("manual_account_review")
            assert not e.request_review("market_entry_review")
        e.start_run(12, 1)
        assert s["profit_target_percent"] == "1"
        s["realized_pnl"] = str(
            common.dec(s["run_start_realized"])
            + common.dec(s["run_start_equity"]) / 100
        )
        e.expire_run()
        assert s["paused"] and s["stop_reason"] == "Profit target reached"
        e.start_run(12, 0)
        s["run_until"] = common.now().isoformat()
        e.expire_run()
        assert s["paused"] and not e.request_review("manual_account_review")
        e.pool.shutdown(wait=True)
        e.feed_pool.shutdown(wait=True)
        for bad in [
            dict(assets=["ETH"]),
            dict(assets=["BTC", "ETH"]),
            dict(size="NaN"),
            dict(size="1.001"),
            dict(daily_loss="1"),
            dict(codex_interval="60"),
        ]:
            try:
                common.validate({**c, **bad})
            except ValueError:
                pass
            else:
                raise AssertionError(bad)
        for actions in [
            [decision(), decision()],
            [decision("EXIT")],
            [decision("HOLD")],
        ]:
            try:
                ai.validate_decisions({"decisions": actions, "reason": "bad"})
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid actions accepted")
    with tempfile.TemporaryDirectory() as folder:
        five = trading.Engine(
            common.initial_state("1000"),
            dict(common.DEFAULTS, market_minutes="5"),
            FakeFeed(),
            data_dir=Path(folder) / "5m",
        )
        fifteen = trading.Engine(
            common.initial_state("1000"),
            common.DEFAULTS,
            FakeFeed(),
            data_dir=Path(folder) / "15m",
        )
        five.state["paused"] = False
        fifteen.state["paused"] = False
        t = common.now()
        five.snapshots = {
            "BTC": dict(
                market_minutes=5,
                ticker="btc-updown-5m-1789263000",
                open_time=(t - timedelta(seconds=60)).isoformat(),
            )
        }
        with (
            patch.object(common, "now", return_value=t),
            patch.object(five, "ready", return_value=True),
            patch.object(five.pool, "submit", return_value=Future()),
        ):
            assert five.request_review("market_entry_review")
            assert not fifteen.state["reviewed_markets"] and fifteen.future is None
            assert (Path(folder) / "5m" / "codex-latest.json").exists()
            assert not (Path(folder) / "15m" / "codex-latest.json").exists()
            assert five.last_codex["payload"]["strategy"]["market_minutes"] == 5
            five.pause()
            assert not fifteen.state["paused"]
        for engine in (five, fifteen):
            engine.pool.shutdown()
            engine.feed_pool.shutdown()
    # Exercise real HTTP routes: each account starts/stops independently without launching AI.
    import json
    import urllib.request
    from http.server import ThreadingHTTPServer

    with tempfile.TemporaryDirectory() as folder:
        dashboard.engines = {}
        dashboard.views = {}
        dashboard.settings_by = {}
        for minutes in ("5", "15"):
            settings = dict(common.DEFAULTS, market_minutes=minutes)
            engine = trading.Engine(
                common.initial_state("1000"),
                settings,
                FakeFeed(),
                data_dir=Path(folder) / minutes,
            )
            engine.ready = lambda *args, **kwargs: False
            dashboard.engines[minutes] = engine
            dashboard.views[minutes] = {"busy": False, "error": None}
            dashboard.settings_by[minutes] = settings
            dashboard.publish(engine, dashboard.views[minutes])
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(action, minutes, body=None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/{action}?minutes={minutes}",
                data=json.dumps(body or {}).encode(),
                headers={"Host": "127.0.0.1:8765", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as response:
                assert response.status == 200

        try:
            for route, content_type in [
                ("/", "text/html"),
                ("/dashboard.css", "text/css"),
                ("/dashboard.js", "text/javascript"),
            ]:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{route}",
                    headers={"Host": "127.0.0.1:8765"},
                )
                with urllib.request.urlopen(req) as response:
                    assert response.headers["Content-Type"].startswith(content_type)
                    assert response.read()
            dashboard.login.update(
                checked_at=dashboard.time.monotonic(), authenticated=True
            )
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/events",
                headers={"Host": "127.0.0.1:8765"},
            )
            stream = urllib.request.urlopen(req, timeout=5)
            assert stream.headers["Content-Type"] == "text/event-stream"
            first = json.loads(stream.readline().decode().removeprefix("data: "))
            stream.readline()
            assert (
                first["accounts"]["5"]["paused"] and first["accounts"]["15"]["paused"]
            )
            post("start", "5")
            updated = json.loads(stream.readline().decode().removeprefix("data: "))
            stream.readline()
            assert (
                not updated["accounts"]["5"]["paused"]
                and updated["accounts"]["15"]["paused"]
            )
            stream.close()
            post("start", "5")
            assert (
                not dashboard.engines["5"].state["paused"]
                and dashboard.engines["15"].state["paused"]
            )
            post("start", "15")
            post("pause", "5")
            assert (
                dashboard.engines["5"].state["paused"]
                and not dashboard.engines["15"].state["paused"]
            )
            post("pause", "15")
            assert dashboard.engines["15"].state["paused"]
            assert all(e.future is None for e in dashboard.engines.values())
            for minutes, balance, maximum in [("5", "1200", "8"), ("15", "1500", "12")]:
                post(
                    "settings",
                    minutes,
                    dict(
                        common.DEFAULTS,
                        market_minutes=minutes,
                        balance=balance,
                        max_trade=maximum,
                    ),
                )
            for minutes, balance, maximum in [("5", "1200", "8"), ("15", "1500", "12")]:
                engine = dashboard.engines[minutes]
                assert (
                    engine.state["cash"] == balance
                    and engine.state["initial_balance"] == balance
                )
                assert engine.config["max_trade"] == common.dec(maximum)
                assert (
                    json.loads((engine.data_dir / "settings.json").read_text())[
                        "balance"
                    ]
                    == balance
                )

        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            for e in dashboard.engines.values():
                e.pool.shutdown()
                e.feed_pool.shutdown()
    from urllib.error import HTTPError

    with patch.object(
        urllib.request,
        "urlopen",
        side_effect=HTTPError("https://example.test/book", 403, "Forbidden", {}, None),
    ) as request:
        for _ in range(2):
            try:
                feeds.get_json("https://example.test/book?token_id=test")
            except ValueError as exc:
                assert "example.test/book" in str(exc)
            else:
                raise AssertionError("Blocked requests must not succeed")
        assert request.call_count == 1
    feeds.HTTP_BLOCKED_UNTIL.clear()
    from backend import dashboard as dashboard

    dashboard.login["checked_at"] = 0
    with (
        patch.object(
            dashboard.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ),
        patch.object(dashboard.time, "monotonic", return_value=100),
    ):
        dashboard.refresh_login()
        assert dashboard.login["authenticated"]
    with (
        patch.object(
            dashboard.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
        ),
        patch.object(dashboard.time, "monotonic", return_value=200),
    ):
        dashboard.refresh_login()
        assert not dashboard.login["authenticated"]
    print(
        "Passed parsing, once-per-market scheduling, restart/error/preview isolation, entry limits and hold-to-settlement accounting"
    )


if __name__ == "__main__":
    run()
