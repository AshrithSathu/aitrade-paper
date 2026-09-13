"""Shared settings, paths and basic value helpers."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data" / "polymarket"

STATE_FILE = DATA / "state.json"

SETTINGS_FILE = DATA / "settings.json"

GAMMA = "https://gamma-api.polymarket.com"

CLOB = "https://clob.polymarket.com"

DATA_API = "https://data-api.polymarket.com"

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"]

DEFAULTS = dict(
    assets=["BTC"],
    market_minutes="15",
    reverse_decisions=False,
    balance="1000",
    size="5",
    daily_loss="-600",
    max_trade="10",
    interval="0.7",
    jitter="0.25",
)


def now():
    return datetime.now(timezone.utc)


def dec(v):
    return Decimal(str(v))


def parse_time(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def minutes_left(m):
    return dec((parse_time(m["close_time"]) - now()).total_seconds()) / 60


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
        json.dump(value, f, default=str, indent=2)
        f.flush()
        os.fsync(f.fileno())
        name = f.name
    os.replace(name, path)


def validate(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Invalid settings fields")
    assets = values["assets"]
    if assets != ["BTC"]:
        raise ValueError("Only BTC is enabled")
    if not isinstance(values["reverse_decisions"], bool):
        raise ValueError("Reverse AI direction must be on or off")
    try:
        n = {
            k: dec(v)
            for k, v in values.items()
            if k not in ("assets", "reverse_decisions")
        }
    except (InvalidOperation, TypeError):
        raise ValueError("Enter valid decimal numbers") from None
    if not all(v.is_finite() for v in n.values()):
        raise ValueError("Numbers must be finite")
    if n["market_minutes"] not in (5, 15):
        raise ValueError("Choose a 5-minute or 15-minute market")
    if (
        n["size"] <= 0
        or n["size"] * 100 % 1
        or n["balance"] <= 0
        or n["max_trade"] <= 0
    ):
        raise ValueError(
            "Positive cash/cap and maximum contracts with at most two decimals required"
        )
    if n["daily_loss"] > 0:
        raise ValueError("Loss budget must be negative; 0 disables it")
    if not dec(".1") <= n["interval"] <= 60 or not 0 <= n["jitter"] <= 5:
        raise ValueError("Poll: 0.1–60 seconds; jitter: 0–5 seconds")
    return {**values, **n}


def initial_state(balance):
    return dict(
        version=3,
        venue="polymarket",
        initial_balance=str(balance),
        cash=str(balance),
        realized_pnl="0",
        trades=0,
        wins=0,
        losses=0,
        gross_profit="0",
        gross_loss="0",
        positions={},
        pending={},
        sessions={},
        reviewed_markets={},
        evaluations={},
        phases={},
        events=[],
        halted=None,
        paused=True,
    )


def load_state(balance):
    if not STATE_FILE.exists():
        return initial_state(balance)
    s = json.loads(STATE_FILE.read_text())
    if s.get("venue") != "polymarket":
        raise ValueError("Refusing to load another venue into the Polymarket account")
    return s


def save_state(s):
    atomic_json(STATE_FILE, s)
