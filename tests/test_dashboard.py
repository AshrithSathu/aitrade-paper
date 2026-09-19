"""Deterministic paper-engine scenarios; never changes the real paper account."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import ai, common, feeds, jev, market, storage
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
                signals={
                    "books": {
                        "UP": {"breakeven_win_probability": ".54"},
                        "DOWN": {"breakeven_win_probability": ".23"},
                    }
                },
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

    jev_payload = {
        "review_assets": ["BTC"],
        "markets": {
            "BTC": {
                "market_minutes": 5,
                "ticker": "btc-updown-5m-1789263000",
                "yes_ask_dollars": ".45",
                "no_ask_dollars": ".57",
                "signals": {
                    "books": {
                        "UP": {"breakeven_win_probability": ".47"},
                        "DOWN": {"breakeven_win_probability": ".59"},
                    },
                    "opening_distance_context": {"one_minute_rms_move_usd": 40},
                },
            }
        },
    }

    class JevResponse:
        def __init__(self, probability, choice):
            self.value = {
                "answers": {
                    "up": {"type": "boolean", "probability": probability},
                    "entry": {"type": "choice", "choice": choice},
                }
            }

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps(self.value).encode()

    def fake_jev_request(request, timeout):
        assert timeout == 20
        assert request.full_url == "https://ai-gateway.vercel.sh/v1/evaluate"
        sent = json.loads(request.data)
        assert sent["model"] == "typesafe-ai/jev"
        assert sent["state"] == jev_payload
        assert sent["providerOptions"]["gateway"]["zeroDataRetention"] is True
        return JevResponse(0.60, "ENTER_UP")

    with (
        patch.dict("os.environ", {"AI_GATEWAY_API_KEY": "test-key"}),
        patch("backend.jev.urllib.request.urlopen", side_effect=fake_jev_request),
    ):
        planned = jev.decide(jev_payload)
        ai.validate_decisions(planned)
        assert planned["decisions"][0]["action"] == "ENTER_UP"
        assert planned["decisions"][0]["max_underlying_drift_usd"] == "4"
    with (
        patch.dict("os.environ", {"AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "backend.jev.urllib.request.urlopen",
            return_value=JevResponse(0.60, "ENTER_DOWN"),
        ),
    ):
        skipped = jev.decide(jev_payload)
        ai.validate_decisions(skipped)
        assert skipped["decisions"][0]["action"] == "WAIT"

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
    from collections import deque

    stream.book_samples = deque(maxlen=301)
    stream.trade_samples = deque(maxlen=5000)
    stream.flow_started = time.time() - 60
    stream.last_message = time.monotonic() - 20
    stream.lock = threading.Lock()
    stream.market = copy.deepcopy(m)
    stream.books = {}
    stream.reported_best = {}
    stream.metadata = {"UP": body}
    stream.error = None
    stream.update({"event_type": "heartbeat", "received_at": int(time.time() * 1000)})
    assert time.monotonic() - stream.last_message < 1
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
    stream.books["DOWN"] = {**copy.deepcopy(body), "asset_id": "22"}
    live = copy.deepcopy(m)
    stream.snapshot(live)
    assert live["book_source_at"] and live["received_at"] != live["book_source_at"]
    assert live["book_source"].endswith("live WebSocket updates")
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
    terminated = []
    stream.process = SimpleNamespace(terminate=lambda: terminated.append(True))
    stream.reported_best["UP"]["best_bid"] = common.dec(".2")
    try:
        stream.snapshot(copy.deepcopy(m))
    except ValueError:
        pass
    else:
        raise AssertionError("Desynchronized WebSocket book accepted")
    assert terminated
    try:
        stream.update(
            {
                "event_type": "price_change",
                "market": m["condition_id"],
                "timestamp": str(int((time.time() + 10) * 1000)),
                "price_changes": [],
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Future orderbook timestamp accepted")
    trade = {
        **body,
        "event_type": "last_trade_price",
        "side": "BUY",
        "price": ".5",
        "size": "4",
    }
    stream.update(trade)
    flow = stream.flow_context(time.time())
    assert flow["windows"]["60s"]["outcomes"]["UP"]["buy_contracts"] == 4
    assert flow["windows"]["60s"]["outcomes"]["UP"]["vwap_usd"] == 0.5
    stream.update({**trade, "size": "NaN"})
    assert len(stream.trade_samples) == 1
    assert ai.market_brief({**m, "flow": flow})["flow"] == flow
    stream.update({"error": "disconnected"})
    assert not stream.trade_samples and not stream.book_samples
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
    assert detailed["contract_history"]["summary"]["UP"]["points"] == 61
    assert set(
        detailed["contract_history"]["summary"]["UP"]["live_mid_change_from"]
    ) == {"1m", "5m", "15m"}
    activity = market.public_trade_activity(
        [
            {
                "condition_id": m["condition_id"],
                "timestamp": 990,
                "outcome": "Up",
                "side": "BUY",
                "price": ".6",
                "size": "5",
            },
            {
                "condition_id": m["condition_id"],
                "timestamp": 980,
                "outcome": "Down",
                "side": "SELL",
                "price": ".4",
                "size": "2",
            },
            {
                "condition_id": m["condition_id"],
                "timestamp": 990,
                "outcome": None,
                "side": "BUY",
                "price": ".6",
                "size": "5",
            },
            {"condition_id": "wrong", "timestamp": 990},
        ],
        m["condition_id"],
        at=1000,
        truncated=True,
    )
    assert activity["rows"] == 2 and activity["truncated_to_latest_1000"]
    assert activity["windows"]["30s"]["contracts"] == "7"
    assert activity["windows"]["30s"]["outcomes"]["UP"]["vwap_dollars"] == "0.6"
    signals = market.trading_signals(signal_market)
    assert signals["opening_distance_context"]["one_minute_rms_move_usd"] == 1
    assert (
        signals["opening_distance_context"]["signed_opening_distance_in_rms_moves"]
        == -2
    )
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
        patch.dict(os.environ, {"CODEX_HOME": str(Path(folder) / "codex")}),
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
        old_at = at - 2 * 86400
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
        stale_temp = root / "codex" / "tmp" / "arg0" / "codex-arg0-stale"
        stale_temp.mkdir(parents=True)
        os.utime(stale_temp, (at - 600, at - 600))
        recent_temp = stale_temp.parent / "codex-arg0-recent"
        recent_temp.mkdir()
        for name in [
            "state.json",
            "codex-latest.json",
            "auth.json",
            "state-backup.json",
        ]:
            (root / name).write_text("preserve")
        result = storage.prune_storage(at, active)
        assert result == {
            "removed_ticks": 0,
            "removed_reviews": 1,
            "removed_codex_temp": 1,
        }
        assert not expired.exists() and active.exists() and recent.exists()
        assert not stale_temp.exists() and recent_temp.exists()
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
            "removed_codex_temp": 0,
        }
    # Only exact terminal Gamma prices or explicit CLOB winner flags establish a payout.
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
    gamma = {**raw, "closed": True, "outcomePrices": '["0.999", "0.001"]'}
    result["is_50_50_outcome"] = False
    result["tokens"][0]["winner"] = False
    with patch.object(
        feeds,
        "get_json",
        side_effect=lambda url: gamma if "/markets/slug/" in url else result,
    ):
        assert f.market(raw["slug"])["payouts"] is None
    gamma["outcomePrices"] = '["0", "1"]'
    with patch.object(feeds, "get_json", return_value=gamma) as request:
        assert f.market(raw["slug"])["payouts"] == {"UP": "1", "DOWN": "0"}
        assert request.call_count == 1
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

        def decision(action="ENTER_UP", limit_price=None):
            entering = action.startswith("ENTER")
            return dict(
                asset="BTC",
                ticker=f.data["BTC"]["ticker"],
                action=action,
                estimated_up_probability=".05"
                if action == "ENTER_DOWN"
                else ".95"
                if entering
                else ".55",
                limit_price=limit_price or (".85" if entering else "0"),
                valid_for_seconds="30" if entering else "0",
                max_underlying_drift_usd="5" if entering else "0",
                max_contract_drift=".03" if entering else "0",
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
            fresh(5)
            assert calls.call_count == 0 and s["paused"]
            assert (
                not e.request_review("manual_account_review") and calls.call_count == 0
            )
            s["paused"] = False
            fresh(4)
            assert calls.call_count == 0
            fresh(5)
            assert calls.call_count == 1 and e.future is not None
            assert e.last_codex["payload"]["phases"]["BTC"] == "READY_FOR_REVIEW"
            assert (
                common.load_state("1000")["reviewed_markets"]["BTC"]
                == f.data["BTC"]["ticker"]
            )
            finish()
            fresh(25)
            assert calls.call_count == 1 and not s["positions"]  # WAIT consumes market
            s["paused"] = True
            s["paused"] = False
            fresh(45)
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
            fresh(55)
            assert e.future is not None
            e.future.set_exception(RuntimeError("AI unavailable"))
            e.complete_review()
            n = calls.call_count
            fresh(60)
            assert calls.call_count == n  # errors never retry
            # Missing data through the dispatch window means zero calls, even after data recovers.
            s["reviewed_markets"].clear()
            f.data["BTC"]["underlying"]["error"] = "missing opening"
            fresh(15)
            assert calls.call_count == n
            f.data["BTC"]["underlying"].pop("error")
            fresh(75)
            assert calls.call_count == n
            # Preview and pause/resume invalidation cannot grant trading authority.
            fresh(15)
            original = e.last_codex["payload"]
            s["paused"] = True
            finish("ENTER_UP")
            assert not s["positions"]
            s["paused"] = False
            s["reviewed_markets"].clear()
            fresh(15)
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
            e.config["size"] = common.dec(".5")
            apply(decision())
            assert not s["positions"]
            e.config["size"] = common.dec("1")
            original = e.payload("market_entry_review")
            e.snapshots["BTC"]["underlying"]["price"] = "95"
            apply(decision(), original)
            assert not s["positions"]
            assert "BTC moved $" in s["events"][-1]["reason"]
            e.snapshots["BTC"]["underlying"]["price"] = "95.6"
            apply(decision(), original)
            assert "BTC" in s["positions"]  # Small response-time drift is absorbed.
            s["positions"].clear()
            s["cash"] = "1000"
            e.snapshots["BTC"]["underlying"]["price"] = "101"
            original = e.payload("market_entry_review")
            e.snapshots["BTC"]["yes_ask_dollars"] = ".94"
            apply(decision(), original)
            assert not s["positions"]
            assert "above the buffered maximum" in s["events"][-1]["reason"]
            apply(decision(limit_price=".83"), original)
            assert not s["positions"]
            assert "above the buffered maximum" in s["events"][-1]["reason"]
            e.snapshots["BTC"]["yes_ask_dollars"] = ".88"
            apply(decision(), original)
            assert (
                "BTC" in s["positions"]
            )  # The agreed 10% contract-price buffer is absorbed.
            s["positions"].clear()
            s["cash"] = "1000"
            no_live_edge = decision()
            no_live_edge["estimated_up_probability"] = ".88"
            apply(no_live_edge, original)
            assert not s["positions"]
            assert "positive fee-adjusted edge" in s["events"][-1]["reason"]
            original = e.payload("market_entry_review")
            e.snapshots["BTC"]["underlying"]["price"] = "107"
            e.snapshots["BTC"]["yes_ask_dollars"] = ".79"
            apply(decision(), original)
            assert "BTC" in s["positions"]
            s["positions"].clear()
            s["cash"] = "1000"
            e.snapshots["BTC"]["underlying"]["price"] = "101"
            e.snapshots["BTC"]["yes_ask_dollars"] = ".80"
            e.config["max_trade"] = common.dec(".1")
            apply(decision())
            assert not s["positions"]
            e.config["max_trade"] = common.dec("10")
            e.snapshots["BTC"]["yes_ask_size_fp"] = "0"
            apply(decision())
            assert not s["positions"]
            fresh(16)
            original = e.payload("market_entry_review")
            e.snapshots["BTC"]["underlying"]["history"] = {"samples": 0}
            apply(decision(), original)
            assert "BTC" in s["positions"]  # AI already reviewed the saved history.
            s["positions"].clear()
            s["cash"] = "1000"
            e.snapshots["BTC"]["underlying"]["source_at"] = (
                clock[0] - timedelta(seconds=31)
            ).isoformat()
            apply(decision(), original)
            assert not s["positions"]
            assert "did not remain current" in s["events"][-1]["reason"]
            fresh(17)
            e.config["max_drawdown_percent"] = common.dec(".01")
            apply(decision())
            assert not s["positions"]
            e.config["max_drawdown_percent"] = common.dec("10")
            apply(decision())
            assert "BTC" in s["positions"]
            cost = common.dec("0.8") + market.trade_fee(
                {"fee_rate": ".07"}, common.dec(1), common.dec(".8")
            )
            assert common.dec(s["cash"]) == 1000 - cost
            f.data["BTC"]["yes_bid_dollars"] = ".01"
            fresh(25)
            assert "BTC" in s["positions"]
            apply(decision("EXIT", "0"))
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
            assert any(event["kind"] == "review_outcome" for event in s["events"])
            cash = s["cash"]
            fresh(902)
            assert s["cash"] == cash and common.dec(cash) == 1001 - cost
            # The next market gets one new review and may enter again.
            start = clock[0]
            f.data["BTC"]["ticker"] = "btc-updown-15m-1789263900"
            s["paused"] = False
            fresh(15)
            assert e.future is not None
            finish("ENTER_DOWN")
            assert s["positions"]["BTC"]["side"] == "DOWN"
            n = calls.call_count
            fresh(25)
            assert calls.call_count == n
            s["paused"] = True
            fresh(900)
            ticker = next(iter(s["pending"]))
            f.results[ticker] = {"UP": "1", "DOWN": "0"}
            e.settle_checked.clear()
            fresh(901)
            assert s["trades"] == 2 and s["losses"] == 1
            # Reverse mode keeps the AI answer intact, executes the other side and
            # shrinks the contracts when that side costs more.
            s.clear()
            s.update(common.initial_state("1000"))
            s["paused"] = False
            e.config.update(
                reverse_decisions=True,
                size=common.dec("50"),
                max_trade=common.dec("10"),
            )
            reversed_market = copy.deepcopy(f.data["BTC"])
            reversed_market.update(
                open_time=clock[0].isoformat(),
                close_time=(clock[0] + timedelta(minutes=15)).isoformat(),
                received_at=clock[0].isoformat(),
                yes_ask_dollars=".80",
                no_ask_dollars=".80",
            )
            reversed_market["underlying"]["source_at"] = clock[0].isoformat()
            e.snapshots = {"BTC": reversed_market}
            original = e.payload("market_entry_review")
            assert "reverse_decisions" not in original["strategy"]
            e.last_codex = {"status": "running", "payload": original, "response": None}
            e.review_path = Path(folder) / "codex-reviews" / "reverse.json"
            e.review_epoch = e.epoch
            e.future = Future()
            e.future.set_result(
                {
                    "decisions": [decision("ENTER_UP", ".85")],
                    "reason": "fixture",
                }
            )
            e.complete_review()
            assert "BTC" in s["positions"], s["events"]
            position = s["positions"]["BTC"]
            assert position["side"] == "DOWN" and position["ai_side"] == "UP"
            assert common.dec(position["size"]) > 1
            assert common.dec(s["events"][-1]["cost"]) <= 10
            assert position["nav_at_entry"] == "1000"
            assert position["nav_allocation_percent"] == "1"
            s["positions"].clear()
            s["cash"] = "500"
            e.config["max_drawdown_percent"] = common.dec("0")
            original = e.payload("market_entry_review")
            e.apply_decision(
                decision("ENTER_UP", ".85"),
                original,
            )
            assert s["positions"]["BTC"]["trade_budget"] == "5"
            assert common.dec(s["events"][-1]["cost"]) <= 5
            codex_event = next(
                event for event in s["events"] if event["kind"] == "codex"
            )
            assert codex_event["decisions"][0]["action"] == "ENTER_UP"
            assert codex_event["decisions"][0]["execution_action"] == "ENTER_DOWN"
            e.reset()
            assert s["paused"] and not s["events"] and s["cash"] == "1000"
            assert e.config["reverse_decisions"] is True
        # A real local subprocess stands in for Codex; pause must stop it and block previews.
        launched = threading.Event()
        processes = []
        popen = subprocess.Popen

        def fake_codex(*args, **kwargs):
            assert args[0][args[0].index("--model") + 1] == "gpt-5.6-terra"
            assert 'model_reasoning_effort="medium"' in args[0]
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
        launched.clear()
        e.config["ai_model"] = "jev"

        def fake_jev(*args, **kwargs):
            assert args[0] == [sys.executable, "-m", "backend.jev"]
            assert kwargs["env"] == {"AI_GATEWAY_API_KEY": "test-key"}
            process = popen(
                [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
            )
            processes.append(process)
            launched.set()
            return process

        s["paused"] = False
        with (
            patch.dict("os.environ", {"AI_GATEWAY_API_KEY": "test-key"}),
            patch.object(subprocess, "Popen", side_effect=fake_jev),
        ):
            assert e.request_review("manual_account_review")
            assert launched.wait(3)
            e.pause()
            assert processes[-1].poll() is not None and e.future.done()
            e.complete_review()
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
            dict(nav_allocation_percent="0"),
            dict(nav_allocation_percent="101"),
            dict(max_drawdown_percent="-1"),
            dict(max_drawdown_percent="101"),
            dict(reverse_decisions="yes"),
            dict(ai_model="unknown"),
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
            [{**decision(), "valid_for_seconds": "31"}],
            [{**decision(), "max_underlying_drift_usd": "0"}],
            [{**decision(), "max_contract_drift": "0"}],
            [{**decision(), "estimated_up_probability": "1.1"}],
            [{**decision("WAIT"), "limit_price": ".5"}],
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
        five.config["ai_model"] = "jev"
        with (
            patch.object(common, "now", return_value=t),
            patch.object(five, "ready", return_value=True),
            patch.object(five.pool, "submit", return_value=Future()) as submit,
        ):
            assert five.request_review("market_entry_review")
            assert submit.call_args.args[0] is ai.jev_decision
            assert five.last_codex["model"] == "jev"
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
        five = dashboard.engines["5"]
        with patch.object(common, "atomic_json") as write:
            five.save_state()
            five.save_state()
            assert write.call_count == 1
            five.state["cash"] = "999"
            five.save_state()
            assert write.call_count == 2
        five.state["cash"] = "1000"
        five.start_run(1)
        dashboard.restore_run(five)
        assert not five.state["paused"]
        five.pause()
        dashboard.restore_run(five)
        assert five.state["paused"]
        five.state = common.initial_state("1000")
        five.saved_state = None
        dashboard.publish(five, dashboard.views["5"])
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
                ("/manifest.webmanifest", "application/manifest+json"),
                ("/service-worker.js", "text/javascript"),
                ("/icon-192.png", "image/png"),
                ("/icon-512.png", "image/png"),
            ]:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{route}",
                    headers={"Host": "127.0.0.1:8765"},
                )
                with urllib.request.urlopen(req) as response:
                    assert response.headers["Content-Type"].startswith(content_type)
                    content = response.read()
                    assert content
                    if route == "/":
                        assert b'id="decisionhistory"' in content
                        assert b'id="newerdecisions"' in content
                        assert b'id="olderdecisions"' in content
                        assert b'rel="manifest"' in content
                        assert b'id="exporthistory"' in content
                        assert b'id="clear5"' in content
                        assert b'name="reverse_decisions"' in content
                        assert b'name="ai_model"' in content
                    if route == "/dashboard.js":
                        assert b'$("pause" + mode).disabled' not in content
                        assert b'$("pause" + m).disabled' not in content
                        assert (
                            b'return durations.includes(value) ? value : "5"' in content
                        )
                        assert b'url.searchParams.set("minutes", minutes)' in content
                        assert b"window.onpopstate" in content
                        assert b"https://polymarket.com/event/" in content
                        assert b"history.outcomes" in content
                        assert b"history.decisions" in content
                        assert b'"&page=" + page' in content
                        assert b'addEventListener("visibilitychange"' in content
                        assert b"navigator.serviceWorker.register" in content
                        assert b"body.reverse_decisions" in content
                    if route == "/manifest.webmanifest":
                        manifest = json.loads(content)
                        assert manifest["display"] == "standalone"
                        assert {icon["sizes"] for icon in manifest["icons"]} == {
                            "192x192",
                            "512x512",
                        }
                    if route == "/service-worker.js":
                        assert b'url.pathname.startsWith("/api/")' in content
                    if route.endswith(".png"):
                        assert content.startswith(b"\x89PNG\r\n\x1a\n")
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/status?minutes=5",
                headers={"Host": "127.0.0.1:8765"},
            )
            with urllib.request.urlopen(req) as response:
                content = response.read()
                status = json.loads(content)
                assert "events" not in status["state"]
                assert status["event_version"] == 0
                assert len(content) < 10000
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/history?minutes=5",
                headers={"Host": "127.0.0.1:8765"},
            )
            with urllib.request.urlopen(req) as response:
                assert json.loads(response.read()) == {
                    "version": 0,
                    "page": 1,
                    "pages": 1,
                    "total": 0,
                    "decisions": [],
                    "entries": [],
                    "results": [],
                    "rejections": {},
                    "outcomes": {},
                    "trades": [],
                }
            events = dashboard.engines["5"].state["events"]
            events.extend(
                {
                    "at": f"2026-01-01T00:00:{number:02d}Z",
                    "kind": "codex",
                    "decisions": [{"ticker": f"market-{number}"}],
                }
                for number in range(21)
            )
            events.extend(
                [
                    {"kind": "entry", "ticker": "market-0", "cost": "2.50"},
                    {"kind": "exit", "ticker": "market-0", "pnl": "2.50"},
                    {
                        "kind": "review_outcome",
                        "ticker": "market-0",
                        "payouts": {"UP": "1", "DOWN": "0"},
                    },
                ]
            )
            dashboard.publish(dashboard.engines["5"], dashboard.views["5"])
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/history?minutes=5&page=2",
                headers={"Host": "127.0.0.1:8765"},
            )
            with urllib.request.urlopen(req) as response:
                history = json.loads(response.read())
                assert history["page"] == history["pages"] == 2
                assert history["total"] == 21
                assert history["decisions"][0]["decisions"][0]["ticker"] == "market-0"
                assert history["entries"][0]["ticker"] == "market-0"
                assert history["results"][0]["pnl"] == "2.50"
                assert history["rejections"] == {}
                assert history["outcomes"]["market-0"]["UP"] == "1"
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/export?minutes=5",
                headers={"Host": "127.0.0.1:8765"},
            )
            with urllib.request.urlopen(req) as response:
                exported = response.read().decode("utf-8-sig")
                assert response.headers["Content-Disposition"] == (
                    'attachment; filename="btc-5-minute-history.csv"'
                )
                assert "decision_at,ai_model,ticker,market_url" in exported
                assert "market-0,https://polymarket.com/event/market-0" in exported
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
            post(
                "settings",
                "5",
                dict(
                    common.DEFAULTS, market_minutes="5", balance="1200", ai_model="jev"
                ),
            )
            assert dashboard.engines["5"].config["ai_model"] == "jev"
            assert dashboard.engines["15"].config["ai_model"] == "codex"
            with patch.dict("os.environ", {"AI_GATEWAY_API_KEY": ""}):
                try:
                    post("start", "5")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 400
                    assert b"AI_GATEWAY_API_KEY" in exc.read()
                else:
                    raise AssertionError("Jev run started without Gateway key")
            assert dashboard.engines["5"].state["paused"]
            post("reset", "5", {"confirm": "CLEAR"})
            assert dashboard.engines["5"].state["events"] == []
            assert dashboard.engines["5"].state["cash"] == "1200"
            assert dashboard.engines["5"].state["paused"]

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
