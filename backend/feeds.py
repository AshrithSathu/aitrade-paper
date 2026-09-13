"""Public market discovery and live feed lifecycle."""

from __future__ import annotations

import atexit
import copy
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

from . import common, market, storage

HTTP_BLOCKED_UNTIL = {}


def get_json(url):
    endpoint = urllib.parse.urlsplit(url)
    label = endpoint.netloc + endpoint.path
    if time.monotonic() < HTTP_BLOCKED_UNTIL.get(label, 0):
        raise ValueError(label + ": access denied; waiting 30 seconds before retry")
    req = urllib.request.Request(
        url, headers={"User-Agent": "local-paper-desk/3", "Cache-Control": "no-cache"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            HTTP_BLOCKED_UNTIL[label] = time.monotonic() + 30
        raise ValueError(f"{label}: HTTP {exc.code} {exc.reason}") from exc


class Chainlink:
    def __init__(self):
        self.history = storage.TickStore()
        self.error = "Waiting for Chainlink TWAP updates"
        self.process = subprocess.Popen(
            ["node", str(common.ROOT / "feeds" / "chainlink.mjs")],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={
                k: v
                for k, v in os.environ.items()
                if k != "DATABASE_URL"
                and not k.startswith(("KALSHI_", "POLYMARKET_", "CHAINLINK_"))
            },
        )
        atexit.register(self.close)
        threading.Thread(target=self.collect, daemon=True).start()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def collect(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                    if message.get("error"):
                        self.error = message["error"]
                        continue
                    ticks = [
                        tick for tick in market.parse_twap(message) if tick[0] == "BTC"
                    ]
                    if ticks:
                        self.history.append(ticks)
                        self.error = None
                except Exception:
                    self.error = "Chainlink history write failed"
            self.error = "Chainlink reader stopped"
        except Exception:
            self.error = "Chainlink reader failed"

    def underlying(self, asset, m):
        start = int(common.parse_time(m["open_time"]).timestamp() * 1000)
        rows, opening = self.history.window(asset, start)
        bars = {}
        for stamp, value in rows:
            minute = stamp // 60000 * 60000
            b = bars.setdefault(
                minute,
                dict(
                    t=minute, open=value, high=value, low=value, close=value, samples=0
                ),
            )
            b.update(
                high=str(max(common.dec(b["high"]), common.dec(value))),
                low=str(min(common.dec(b["low"]), common.dec(value))),
                close=value,
                samples=b["samples"] + 1,
            )
        data = dict(
            source="Chainlink 60s TWAP via Polymarket RTDS",
            price=rows[-1][1] if rows else None,
            source_at=datetime.fromtimestamp(
                rows[-1][0] / 1000, timezone.utc
            ).isoformat()
            if rows
            else None,
            opening_price=opening[0] if opening else None,
            opening_source="Exact Chainlink TWAP tick at market start",
            delta=None,
            history=dict(
                source="Locally recorded Chainlink TWAP",
                resolution="1-minute OHLC of received TWAP observations",
                samples=len(rows),
                bars=list(bars.values()),
                first_at=rows[0][0] if rows else None,
                last_at=rows[-1][0] if rows else None,
                max_gap_ms=max(
                    (b[0] - a[0] for a, b in zip(rows, rows[1:])), default=0
                ),
                limitation="No guaranteed backfill or replay; raw observations retained in PostgreSQL for 24 hours",
            ),
        )
        cached = getattr(self, "context_cache", {}).get(asset)
        if not cached or time.monotonic() - cached[0] >= 60:
            cached = (time.monotonic(), self.history.context24(asset))
            if not hasattr(self, "context_cache"):
                self.context_cache = {}
            self.context_cache[asset] = cached
        data["history_24h"] = copy.deepcopy(cached[1])
        if rows and opening:
            data["delta"] = str(common.dec(rows[-1][1]) - common.dec(opening[0]))
        if not rows or time.time() - rows[-1][0] / 1000 > 5:
            data["error"] = self.error or "Chainlink source data is stale"
        elif not opening:
            data["error"] = (
                "Opening tick not recorded: wait for the next market; no opening price is fabricated"
            )
        return data


class BookStream:
    def __init__(self, m):
        self.market = copy.deepcopy(m)
        self.books = {}
        self.reported_best = {}
        self.book_samples = deque(maxlen=301)
        self.trade_samples = deque(maxlen=5000)
        self.flow_started = time.time()
        self.last_message = time.monotonic()
        self.lock = threading.Lock()
        self.error = "Waiting for WebSocket books"
        self.metadata = {
            side: get_json(
                common.CLOB + "/book?" + urllib.parse.urlencode({"token_id": token})
            )
            for side, token in m["tokens"].items()
        }
        validation_market = {
            key: self.market[key] for key in ("tokens", "condition_id")
        }
        for side, body in self.metadata.items():
            market.apply_book(
                copy.deepcopy(validation_market), body, side, require_fresh=False
            )
            self.books[side] = copy.deepcopy(body)
        self.process = subprocess.Popen(
            [
                "node",
                str(common.ROOT / "feeds" / "chainlink.mjs"),
                *m["tokens"].values(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"},
        )
        threading.Thread(target=self.collect, daemon=True).start()

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def update(self, event):
        if event.get("error"):
            self.books.clear()
            self.reported_best.clear()
            self.book_samples.clear()
            self.trade_samples.clear()
            self.flow_started = time.time()
            self.error = event["error"]
            return
        if event.get("event_type") == "heartbeat":
            received_at = event.get("received_at")
            if (
                isinstance(received_at, int)
                and 0 <= time.time() * 1000 - received_at <= 5000
            ):
                self.last_message = time.monotonic()
                self.error = None
            return
        if event.get("market") != self.market["condition_id"]:
            return
        kind = event.get("event_type")
        if kind == "last_trade_price":
            side = next(
                (
                    s
                    for s, token in self.market["tokens"].items()
                    if token == event.get("asset_id")
                ),
                None,
            )
            if side is None:
                return
            try:
                stamp, price, size = (
                    common.dec(event[k]) for k in ("timestamp", "price", "size")
                )
                if (
                    not all(v.is_finite() for v in (stamp, price, size))
                    or not 0 < price < 1
                    or size <= 0
                    or not 0 <= time.time() * 1000 - float(stamp) <= 300000
                    or event.get("side") not in ("BUY", "SELL")
                ):
                    return
            except (KeyError, ValueError, ArithmeticError):
                return
            self.trade_samples.append(
                (float(stamp) / 1000, side, event["side"], float(price), float(size))
            )
            return
        if kind not in ("book", "price_change", "tick_size_change"):
            return
        stamp = common.dec(event["timestamp"]) / 1000
        if not stamp.is_finite() or stamp <= 0 or common.dec(time.time()) - stamp < -2:
            raise ValueError("Orderbook source timestamp is invalid")
        changes = event.get("price_changes", []) if kind == "price_change" else [event]
        pending = self.books.copy()
        copied = set()
        for change in changes:
            side = next(
                (
                    side
                    for side, token in self.market["tokens"].items()
                    if token == change.get("asset_id")
                ),
                None,
            )
            if side is None:
                continue
            if kind == "book":
                if side in pending and common.dec(event["timestamp"]) < common.dec(
                    pending[side]["timestamp"]
                ):
                    raise ValueError("Out-of-order book snapshot")
                body = {**self.metadata[side], **event}
                self.reported_best.pop(side, None)
            else:
                if side not in pending:
                    continue
                if side not in copied:
                    pending[side] = copy.deepcopy(pending[side])
                    copied.add(side)
                body = pending[side]
                if common.dec(event["timestamp"]) < common.dec(body["timestamp"]):
                    raise ValueError("Out-of-order book update")
                if kind == "tick_size_change":
                    body["tick_size"] = change["new_tick_size"]
                    self.metadata[side]["tick_size"] = change["new_tick_size"]
                else:
                    if change["side"] not in ("BUY", "SELL"):
                        raise ValueError("Invalid book side")
                    key = "bids" if change["side"] == "BUY" else "asks"
                    price, size = (
                        common.dec(change["price"]),
                        common.dec(change["size"]),
                    )
                    if (
                        not price.is_finite()
                        or not size.is_finite()
                        or not 0 <= price <= 1
                        or size < 0
                    ):
                        raise ValueError("Invalid book delta")
                    body[key] = [
                        row for row in body[key] if common.dec(row["price"]) != price
                    ]
                    if size:
                        body[key].append({"price": str(price), "size": str(size)})
                body["timestamp"] = event["timestamp"]
            if kind == "price_change":
                reported = {}
                for key in ("best_bid", "best_ask"):
                    value = common.dec(change[key])
                    if not value.is_finite() or not 0 <= value <= 1:
                        raise ValueError("Invalid reported best price")
                    reported[key] = value
                body["bids"] = [
                    row
                    for row in body["bids"]
                    if common.dec(row["price"]) <= reported["best_bid"]
                ]
                body["asks"] = [
                    row
                    for row in body["asks"]
                    if common.dec(row["price"]) >= reported["best_ask"]
                ]
                self.reported_best[side] = reported
            else:
                market.apply_book(
                    {
                        key: copy.deepcopy(self.market[key])
                        for key in ("tokens", "condition_id")
                    },
                    body,
                    side,
                    require_fresh=False,
                )
            pending[side] = body
        self.books = pending
        self.last_message = time.monotonic()
        self.error = None

    def collect(self):
        try:
            for line in self.process.stdout:
                events = json.loads(line)
                with self.lock:
                    for event in events if isinstance(events, list) else [events]:
                        self.update(event)
        except Exception as exc:
            with self.lock:
                self.books.clear()
                self.error = str(exc)
            self.process.terminate()
        finally:
            with self.lock:
                self.books.clear()
                self.error = "Orderbook stream stopped"

    def snapshot(self, m):
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            if time.monotonic() - self.last_message > 10:
                raise ValueError("Waiting for live Polymarket connection")
            for side in m["tokens"]:
                if side not in self.books:
                    raise ValueError("Waiting for complete WebSocket books")
                body = self.books[side]
                reported = self.reported_best.get(side, {})
                actual = {
                    "best_bid": max(
                        (common.dec(row["price"]) for row in body["bids"]),
                        default=None,
                    ),
                    "best_ask": min(
                        (common.dec(row["price"]) for row in body["asks"]),
                        default=None,
                    ),
                }
                if any(actual[key] != value for key, value in reported.items()):
                    raise ValueError("Waiting for synchronized WebSocket book")
                market.apply_book(m, copy.deepcopy(body), side, require_fresh=False)
            stamp = time.time()
            if not self.book_samples or stamp - self.book_samples[-1][0] >= 1:
                point = {}
                for side, book in m["orderbook"].items():
                    bid, ask = market.quote(m, side, "bid"), market.quote(m, side)
                    depths = [
                        sum(
                            float(row["size"])
                            for row in sorted(
                                book[key],
                                key=lambda row: float(row["price"]),
                                reverse=reverse,
                            )[:5]
                        )
                        for key, reverse in (("bids", True), ("asks", False))
                    ]
                    point[side] = {
                        "mid": float((bid + ask) / 2)
                        if bid is not None and ask is not None
                        else None,
                        "bid_depth": depths[0],
                        "ask_depth": depths[1],
                    }
                self.book_samples.append((stamp, point))
            m["flow"] = self.flow_context(stamp)
        m["book_source_at"] = m.pop("received_at")
        m["received_at"] = common.now().isoformat()
        m["book_source"] = "Validated Polymarket book with live WebSocket updates"

    def flow_context(self, stamp):
        result = {
            "source": "Observed public Polymarket WebSocket events",
            "observed_seconds": round(stamp - self.flow_started, 1),
            "windows": {},
            "limitations": "Memory only; resets on reconnect/restart. Book samples at most once per second. Trade events are provider-reported BUY/SELL, not independently verified aggressor flow; no guaranteed complete tape or replay.",
        }
        for seconds in (30, 60, 180):
            points = [
                row for row in self.book_samples if stamp - seconds <= row[0] <= stamp
            ]
            trades = [
                row for row in self.trade_samples if stamp - seconds <= row[0] <= stamp
            ]
            result["windows"][str(seconds) + "s"] = {
                "book_samples": len(points),
                "book_coverage_seconds": round(points[-1][0] - points[0][0], 1)
                if points
                else 0,
                "max_sample_gap_seconds": round(
                    max((b[0] - a[0] for a, b in zip(points, points[1:])), default=0), 1
                ),
                "trade_buffer_capped": len(self.trade_samples)
                == self.trade_samples.maxlen,
                "outcomes": {},
            }
            for side in self.market["tokens"]:
                selected = [t for t in trades if t[1] == side]
                volume = sum(t[4] for t in selected)
                first, last = (
                    (points[0][1].get(side), points[-1][1].get(side))
                    if points
                    else (None, None)
                )
                result["windows"][str(seconds) + "s"]["outcomes"][side] = {
                    "mid_change_usd": last["mid"] - first["mid"]
                    if first
                    and last
                    and first["mid"] is not None
                    and last["mid"] is not None
                    else None,
                    "top5_bid_depth_change": last["bid_depth"] - first["bid_depth"]
                    if first and last
                    else None,
                    "top5_ask_depth_change": last["ask_depth"] - first["ask_depth"]
                    if first and last
                    else None,
                    "observed_trade_count": len(selected),
                    "contracts": volume,
                    "buy_contracts": sum(t[4] for t in selected if t[2] == "BUY"),
                    "sell_contracts": sum(t[4] for t in selected if t[2] == "SELL"),
                    "vwap_usd": sum(t[3] * t[4] for t in selected) / volume
                    if volume
                    else None,
                    "last_trade_age_seconds": round(
                        stamp - max(t[0] for t in selected), 1
                    )
                    if selected
                    else None,
                }
        return result


class Feed:
    def __init__(self):
        self.markets = {}
        self.histories = {}
        self.activities = {}
        self.chainlink = Chainlink()
        self.streams = {}
        atexit.register(self.close)

    def close(self):
        for stream in self.streams.values():
            stream.close()

    def market(self, ticker):
        if not re.fullmatch(r"[a-z]+-updown-(5|15)m-\d+", ticker):
            raise ValueError("Invalid Polymarket slug")
        raw = get_json(common.GAMMA + "/markets/slug/" + ticker)
        if raw.get("slug") != ticker:
            raise ValueError("Polymarket settlement slug mismatch")
        m = market.parse_market(raw, ticker.split("-")[0].upper())
        result = get_json(common.CLOB + "/markets/" + m["condition_id"])
        if result.get("condition_id") != m["condition_id"]:
            raise ValueError("Settlement condition mismatch")
        payouts = None
        tokens = result.get("tokens", [])
        if (
            result.get("closed") is True
            and len(tokens) == 2
            and {t.get("token_id") for t in tokens} == set(m["tokens"].values())
        ):
            if result.get("is_50_50_outcome") is True:
                payouts = {"UP": "0.5", "DOWN": "0.5"}
            elif sum(t.get("winner") is True for t in tokens) == 1:
                payouts = {
                    side: str(
                        int(
                            next(t for t in tokens if t["token_id"] == token).get(
                                "winner"
                            )
                            is True
                        )
                    )
                    for side, token in m["tokens"].items()
                }
        return {**m, "payouts": payouts, "resolution": result}

    def snapshot(self, asset, c):
        duration = int(c["market_minutes"])
        seconds = duration * 60
        key = (asset, duration)
        slug = (
            asset.lower()
            + f"-updown-{duration}m-"
            + str(int(time.time()) // seconds * seconds)
        )
        cached = self.markets.get(key)
        if not cached or cached[1]["ticker"] != slug:
            raw = get_json(common.GAMMA + "/markets/slug/" + slug)
            if raw.get("slug") != slug:
                raise ValueError("No current Polymarket market")
            m = market.parse_market(raw, asset)
            if (
                not raw.get("active")
                or raw.get("closed")
                or not raw.get("acceptingOrders")
                or not raw.get("enableOrderBook")
            ):
                raise ValueError("Market not accepting orders")
            info = get_json(common.CLOB + "/clob-markets/" + m["condition_id"])
            if info.get("c") != m["condition_id"] or info.get("ao") is not True:
                raise ValueError("CLOB market not accepting orders")
            fd = info.get("fd")
            if not isinstance(fd, dict) or fd.get("e") != 1:
                raise ValueError("Unsupported or missing fee schedule")
            rate = common.dec(fd["r"])
            if not rate.is_finite() or not 0 <= rate <= 1:
                raise ValueError("Invalid fee rate")
            m.update(fee_rate=str(rate), fee_details=fd, clob_info=info)
            self.markets[key] = (time.monotonic(), m)
        else:
            m = cached[1]
        m = copy.deepcopy(m)
        elapsed = (common.now() - common.parse_time(m["open_time"])).total_seconds()
        # Start the live book first so slower history reads do not shorten flow coverage.
        stream = self.streams.get(key)
        if (
            not stream
            or stream.market["ticker"] != slug
            or stream.process.poll() is not None
        ):
            if stream:
                stream.close()
            stream = BookStream(m)
            self.streams[key] = stream
        # History is contract probability history, never mislabeled as BTC/USD history.
        h = self.histories.get(slug)
        if not h or (not h[2] and elapsed >= 10):
            try:
                history = {
                    side: get_json(
                        common.CLOB
                        + "/prices-history?"
                        + urllib.parse.urlencode(
                            dict(market=token, interval="1h", fidelity=1)
                        )
                    )["history"]
                    for side, token in m["tokens"].items()
                }
                for points in history.values():
                    if not isinstance(points, list):
                        raise ValueError("Invalid probability history")
                    for point in points:
                        if (
                            not common.dec(point["p"]).is_finite()
                            or not 0 <= common.dec(point["p"]) <= 1
                            or not 0 < int(point["t"]) <= time.time() + 2
                        ):
                            raise ValueError("Invalid probability history point")
                h = (
                    time.monotonic(),
                    dict(
                        source="Polymarket CLOB outcome prices, not underlying asset prices",
                        received_at=common.now().isoformat(),
                        outcomes=history,
                    ),
                    elapsed >= 10,
                )
            except Exception as exc:
                h = (
                    (h[0], {**h[1], "refresh_error": str(exc)}, True)
                    if h and not h[1].get("error")
                    else (time.monotonic(), {"error": str(exc)}, elapsed >= 10)
                )
            self.histories = {
                key: value
                for key, value in self.histories.items()
                if time.monotonic() - value[0] < 3600
            }
            self.histories[slug] = h
        m["contract_history"] = copy.deepcopy(h[1])
        stream.snapshot(m)
        activity = self.activities.get(slug)
        if not activity or (not activity[2] and elapsed >= 10):
            try:
                response = get_json(
                    common.DATA_API
                    + "/v2/trades?"
                    + urllib.parse.urlencode(
                        {
                            "condition": m["condition_id"],
                            "limit": 1000,
                            "taker_only": "true",
                        }
                    )
                )
                rows, paging = response["data"], response["pagination"]
                if not isinstance(rows, list) or not isinstance(paging, dict):
                    raise ValueError("Invalid public trade activity")
                result = market.public_trade_activity(
                    rows,
                    m["condition_id"],
                    truncated=paging.get("has_more") is True,
                )
            except Exception as exc:
                result = (
                    {**activity[1], "refresh_error": str(exc)}
                    if activity and not activity[1].get("error")
                    else {"error": str(exc)}
                )
            activity = (time.monotonic(), result, elapsed >= 10)
            self.activities = {
                key: value
                for key, value in self.activities.items()
                if time.monotonic() - value[0] < 3600
            }
            self.activities[slug] = activity
        m["public_trade_activity"] = copy.deepcopy(activity[1])
        m["underlying"] = self.chainlink.underlying(asset, m)
        m["floor_strike"] = m["underlying"]["opening_price"]
        m["signals"] = market.trading_signals(m)
        return m
