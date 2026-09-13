"""Market validation, book parsing, fees and price signals."""

from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timezone

from . import common


def quote(m, side, kind="ask"):
    v = m.get(("yes" if side == "UP" else "no") + "_" + kind + "_dollars")
    if v is None or v == "":
        return None
    n = common.dec(v)
    if not n.is_finite() or not 0 <= n <= 1:
        raise ValueError("Invalid quote")
    return n


def apply_book(m, body, side, require_fresh=True):
    token = m["tokens"][side]
    if body.get("market") != m["condition_id"] or body.get("asset_id") != token:
        raise ValueError("Orderbook token/market mismatch")
    stamp = common.dec(body["timestamp"]) / 1000
    if (
        not stamp.is_finite()
        or stamp <= 0
        or common.dec(time.time()) - stamp < -2
        or (require_fresh and common.dec(time.time()) - stamp > 5)
    ):
        raise ValueError("Orderbook source timestamp is stale or invalid")
    prefix = "yes" if side == "UP" else "no"
    for kind, levels in [("bid", body["bids"]), ("ask", body["asks"])]:
        totals = {}
        for row in levels:
            price, size = common.dec(row["price"]), common.dec(row["size"])
            if (
                not price.is_finite()
                or not size.is_finite()
                or not 0 <= price <= 1
                or size < 0
            ):
                raise ValueError("Invalid Polymarket book level")
            if size:
                totals[price] = totals.get(price, common.dec(0)) + size
        best = (max(totals) if kind == "bid" else min(totals)) if totals else None
        m[prefix + "_" + kind + "_dollars"] = str(best) if best is not None else None
        m[prefix + "_" + kind + "_size_fp"] = (
            str(totals[best]) if best is not None else "0"
        )
    bid, ask = quote(m, side, "bid"), quote(m, side)
    if bid is not None and ask is not None and bid >= ask:
        raise ValueError("Crossed or locked Polymarket orderbook")
    minimum, tick = common.dec(body["min_order_size"]), common.dec(body["tick_size"])
    if (
        not minimum.is_finite()
        or minimum <= 0
        or not tick.is_finite()
        or not 0 < tick < 1
    ):
        raise ValueError("Invalid market order limits")
    m.setdefault("order_limits", {})[side] = {
        "minimum": str(minimum),
        "tick": str(tick),
    }
    m.setdefault("orderbook", {})[side] = body
    m["received_at"] = (
        datetime.fromtimestamp(float(stamp), timezone.utc).isoformat()
        if "received_at" not in m
        else min(
            m["received_at"],
            datetime.fromtimestamp(float(stamp), timezone.utc).isoformat(),
        )
    )
    return m


def parse_market(raw, asset):
    slug = raw["slug"]
    if not re.fullmatch(asset.lower() + r"-updown-(5|15)m-\d+", slug):
        raise ValueError("Unexpected market slug")
    start = int(slug.rsplit("-", 1)[1])
    duration = int(slug.split("-")[2][:-1])
    if (
        common.parse_time(raw["endDate"]).timestamp() != start + duration * 60
        or common.parse_time(raw["eventStartTime"]).timestamp() != start
    ):
        raise ValueError("Market window does not match its market slug")
    cfg = raw.get("cryptoMarketConfig", {})
    source = (
        "https://data.chain.link/streams/" + asset.lower() + "-usd-twap-60s-streams"
    )
    if (
        raw.get("resolutionSource") != source
        or cfg.get("twapLookbackSeconds") != 60
        or cfg.get("twapEnabled") is not True
    ):
        raise ValueError("Unsupported resolution source; no proxy substitution")
    outcomes = (
        json.loads(raw["outcomes"])
        if isinstance(raw["outcomes"], str)
        else raw["outcomes"]
    )
    ids = (
        json.loads(raw["clobTokenIds"])
        if isinstance(raw["clobTokenIds"], str)
        else raw["clobTokenIds"]
    )
    if (
        len(ids) != 2
        or set(outcomes) != {"Up", "Down"}
        or len(set(ids)) != 2
        or not all(re.fullmatch(r"\d+", i) for i in ids)
    ):
        raise ValueError("Invalid outcome/token mapping")
    condition = raw["conditionId"]
    if not re.fullmatch(r"0x[0-9a-f]{64}", condition):
        raise ValueError("Invalid condition ID")
    return dict(
        venue="polymarket",
        asset=asset,
        market_minutes=duration,
        ticker=slug,
        condition_id=condition,
        tokens={o.upper(): t for o, t in zip(outcomes, ids)},
        open_time=raw["eventStartTime"],
        close_time=raw["endDate"],
        resolution_source=source,
        description=raw["description"],
        raw_market=raw,
    )


def parse_twap(message):
    if message.get("topic") != "crypto_prices_twap_sixty":
        return []
    payload = message.get("payload", {})
    asset = str(payload.get("symbol", "")).split("/")[0].upper()
    if (
        asset not in common.ASSETS
        or payload.get("symbol") != asset.lower() + "/usd"
        or payload.get("window_s") != 60
    ):
        return []
    rows = payload.get("data", [payload])
    result = []
    for row in rows:
        raw = row.get("full_accuracy_value")
        if not isinstance(raw, str) or not re.fullmatch(r"\d{1,40}", raw):
            raise ValueError("Invalid exact Chainlink price")
        stamp = row.get("timestamp")
        if (
            not isinstance(stamp, int)
            or not 0 < stamp <= int(time.time() * 1000) + 2000
        ):
            raise ValueError("Invalid Chainlink timestamp")
        value = common.dec(raw) / common.dec(10**18)
        if value <= 0:
            raise ValueError("Invalid Chainlink price")
        result.append((asset, stamp, str(value)))
    return result


def trading_signals(m):
    u = m["underlying"]
    bars = u["history"]["bars"]
    end = int(common.parse_time(u["source_at"]).timestamp() * 1000) // 60000 * 60000
    windows = {}
    # Signals describe received TWAP observations, not exchange spot candles or traded volume.
    for minutes in (1, 5, 15, 30, 60):
        selected = [b for b in bars if end - minutes * 60000 <= b["t"] < end]
        values = [float(b["close"]) for b in selected]
        complete = bool(
            selected
            and u["history"]["first_at"] <= end - minutes * 60000
            and len(selected) == minutes
        )
        changes = [(b / a - 1) * 100 for a, b in zip(values, values[1:])]
        windows[str(minutes) + "m"] = dict(
            complete=complete,
            minute_bars=len(selected),
            change_pct=(values[-1] / float(selected[0]["open"]) - 1) * 100
            if values
            else None,
            high=max((float(b["high"]) for b in selected), default=None),
            low=min((float(b["low"]) for b in selected), default=None),
            sma=sum(values) / len(values) if values else None,
            return_stddev_pct=statistics.pstdev(changes) if len(changes) >= 2 else None,
            note="Available portion only"
            if not complete
            else "Completed minute bars; current live price supplied separately",
        )
    closed = [b for b in bars if b["t"] < end // 60000 * 60000]
    rsi = None
    if len(closed) >= 15 and all(
        b["t"] - a["t"] == 60000 for a, b in zip(closed[-15:], closed[-14:])
    ):
        differences = [
            float(b["close"]) - float(a["close"])
            for a, b in zip(closed[-15:], closed[-14:])
        ]
        gain = sum(max(x, 0) for x in differences)
        loss = sum(max(-x, 0) for x in differences)
        rsi = 100 * gain / (gain + loss) if gain + loss else 50
    recent = closed[-31:]
    differences = [
        float(b["close"]) - float(a["close"])
        for a, b in zip(recent, recent[1:])
        if b["t"] - a["t"] == 60000
    ]
    contiguous = len(differences) == len(recent) - 1 and len(differences) >= 15
    rms = (
        (sum(x * x for x in differences) / len(differences)) ** 0.5
        if contiguous
        else None
    )
    distance = float(u["delta"]) if u.get("delta") is not None else None
    opening_context = {
        "one_minute_rms_move_usd": rms,
        "observations": len(differences),
        "contiguous": contiguous,
        "signed_opening_distance_in_rms_moves": distance / rms
        if distance is not None and rms
        else None,
        "definition": "Signed opening distance divided by RMS of up to 30 contiguous completed one-minute TWAP changes; requires at least 15 changes. Descriptive scale only, not a settlement probability or forecast.",
    }
    books = {}
    for side, book in m["orderbook"].items():
        bid, ask = quote(m, side, "bid"), quote(m, side)
        depth = {}
        for key, reverse in [("bids", True), ("asks", False)]:
            levels = sorted(
                book[key], key=lambda row: common.dec(row["price"]), reverse=reverse
            )[:5]
            depth[key] = sum((common.dec(row["size"]) for row in levels), common.dec(0))
        total = depth["bids"] + depth["asks"]
        books[side] = dict(
            spread=str(ask - bid) if bid is not None and ask is not None else None,
            top5_bid_contracts=str(depth["bids"]),
            top5_ask_contracts=str(depth["asks"]),
            top5_imbalance=str((depth["bids"] - depth["asks"]) / total)
            if total
            else None,
            breakeven_win_probability=str(ask + trade_fee(m, common.dec(1), ask))
            if ask is not None
            else None,
        )
    return dict(
        windows=windows,
        opening_distance_context=opening_context,
        rsi14_simple_closed_minutes=rsi,
        books=books,
        seconds_remaining=float(common.minutes_left(m) * 60),
        opening_delta_usd=u.get("delta"),
        source_age_seconds=(
            common.now() - common.parse_time(u["source_at"])
        ).total_seconds(),
        max_gap_ms=u["history"]["max_gap_ms"],
        limitations=[
            "TWAP-derived indicators are smoothed and are not independent evidence of edge",
            "RSI uses simple gains/losses over 14 completed minute changes, not Wilder smoothing",
            "Window values can contain gaps; inspect complete, bar counts and max_gap_ms",
            "Orderbook depth is displayed liquidity, not traded volume; no volume indicator is inferred",
        ],
    )


def public_trade_activity(rows, condition_id, at=None, truncated=False):
    """Compact public taker fills without sending trader identities to Codex."""
    at = time.time() if at is None else at
    valid = []
    for row in rows:
        try:
            stamp = int(row["timestamp"])
            price, size = common.dec(row["price"]), common.dec(row["size"])
            outcome, side = row["outcome"].upper(), row["side"].upper()
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            continue
        if (
            row.get("condition_id") != condition_id
            or outcome not in ("UP", "DOWN")
            or side not in ("BUY", "SELL")
            or not price.is_finite()
            or not size.is_finite()
            or not 0 < price < 1
            or size <= 0
            or not 0 < stamp <= at + 2
        ):
            continue
        valid.append((stamp, outcome, side, price, size))
    result = {
        "source": "Polymarket Data API v2 taker-side fills",
        "received_at": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "rows": len(valid),
        "truncated_to_latest_1000": bool(truncated),
        "windows": {},
        "limitations": "Each fill is reported once on its taker side. Activity is market participation context, not verified trader intent or a probability forecast.",
    }
    for seconds in (30, 60, 180, 900):
        selected = [row for row in valid if at - seconds <= row[0] <= at]
        window = {
            "trade_count": len(selected),
            "contracts": str(sum((row[4] for row in selected), common.dec(0))),
            "outcomes": {},
        }
        for outcome in ("UP", "DOWN"):
            trades = [row for row in selected if row[1] == outcome]
            volume = sum((row[4] for row in trades), common.dec(0))
            window["outcomes"][outcome] = {
                "trade_count": len(trades),
                "buy_contracts": str(
                    sum(
                        (row[4] for row in trades if row[2] == "BUY"),
                        common.dec(0),
                    )
                ),
                "sell_contracts": str(
                    sum(
                        (row[4] for row in trades if row[2] == "SELL"),
                        common.dec(0),
                    )
                ),
                "vwap_dollars": str(
                    sum((row[3] * row[4] for row in trades), common.dec(0)) / volume
                )
                if volume
                else None,
            }
        result["windows"][str(seconds) + "s"] = window
    return result


def trade_fee(m, quantity, price):
    rate = common.dec(m["fee_rate"])
    if not rate.is_finite() or not 0 <= rate <= 1:
        raise ValueError("Invalid trading fee")
    return (quantity * rate * price * (1 - price)).quantize(common.dec(".00001"))
