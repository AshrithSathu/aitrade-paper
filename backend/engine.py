"""Paper account lifecycle, review scheduling and simulated execution."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from . import ai, common, feeds, market, storage


def account(s):
    exposure = list(s["positions"].values()) + list(s["pending"].values())
    equity = common.dec(s["cash"]) + sum(
        (common.dec(p["last_mark"]) * common.dec(p["size"]) for p in exposure),
        common.dec(0),
    )
    return dict(
        cash=s["cash"],
        equity=str(equity),
        live_pnl=str(equity - common.dec(s["initial_balance"])),
        realized_pnl=s["realized_pnl"],
        unrealized_pnl=str(
            equity - common.dec(s["initial_balance"]) - common.dec(s["realized_pnl"])
        ),
        wins=s["wins"],
        losses=s["losses"],
        trades=s["trades"],
        average_profit=str(common.dec(s["gross_profit"]) / s["wins"])
        if s["wins"]
        else None,
        average_loss=str(common.dec(s["gross_loss"]) / s["losses"])
        if s["losses"]
        else None,
    )


class Engine:
    def __init__(self, state, settings, feed=None, data_dir=None):
        self.data_dir = data_dir or common.DATA
        self.state_path = self.data_dir / "state.json"
        self.state = state
        self.config = common.validate(settings)
        self.feed = feed or feeds.Feed()
        self.snapshots = {}
        self.errors = {}
        self.last_codex = None
        self.future = None
        self.epoch = 0
        self.review_epoch = 0
        self.settle_checked = {}
        self.last_cleanup = 0
        self.review_lock = threading.RLock()
        self.cancel_review = threading.Event()
        self.saved_state = None
        self.state.setdefault("reviewed_markets", {})
        self.state.setdefault("evaluations", {})
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.feed_pool = ThreadPoolExecutor(max_workers=7)
        if (self.data_dir / "codex-latest.json").exists():
            self.last_codex = json.loads(
                (self.data_dir / "codex-latest.json").read_text()
            )
            if self.last_codex["status"] == "running":
                self.last_codex["status"] = "interrupted"

    def save_state(self, force=False):
        current = json.dumps(self.state, default=str, sort_keys=True)
        if force or current != self.saved_state:
            common.atomic_json(self.state_path, self.state)
            self.saved_state = current

    def emit(self, kind, **fields):
        self.state["events"].append(
            dict(at=common.now().isoformat(), kind=kind, **fields)
        )

    def payload(self, trigger, assets=None):
        return copy.deepcopy(
            dict(
                trigger=trigger,
                at=common.now().isoformat(),
                execution_allowed=not self.state["paused"]
                and trigger == "market_entry_review",
                review_assets=list(self.config["assets"] if assets is None else assets),
                review_policy="One conditional-plan review starting 15 seconds after market opening (60-second dispatch window); WAIT/error skips market; hold entries to official settlement",
                strategy={
                    k: v
                    for k, v in self.config.items()
                    if k
                    in ("assets", "market_minutes", "size", "max_trade", "daily_loss")
                },
                account=account(self.state),
                positions=self.state["positions"],
                pending_settlements=self.state["pending"],
                phases=self.state["phases"],
                markets={a: ai.market_brief(m) for a, m in self.snapshots.items()},
                feed_errors=self.errors,
                run_controls={
                    k: self.state.get(k)
                    for k in (
                        "run_until",
                        "profit_target_percent",
                        "run_start_equity",
                        "run_start_realized",
                    )
                },
                recent_events=[
                    {
                        k: (v[:500] if k == "reason" and isinstance(v, str) else v)
                        for k, v in event.items()
                    }
                    for event in self.state["events"][-10:]
                ],
                limitations=[
                    "AI-only Polymarket paper decisions; asks/bids with estimated taker fees, no slippage beyond displayed top size",
                    "Underlying history is locally recorded Chainlink 60s TWAP; gaps and warm-up are explicit",
                    "CLOB contract probability history is separate from underlying USD prices",
                    "Loss budget blocks new exposure; it does not automatically sell positions",
                ],
                paused=self.state["paused"],
                halted=self.state["halted"],
            )
        )

    def review_due(self, asset):
        m = self.snapshots.get(asset)
        return bool(
            m
            and m.get("market_minutes", 15) == int(self.config["market_minutes"])
            and asset not in self.state["positions"]
            and self.state["reviewed_markets"].get(asset) != m["ticker"]
            and 15
            <= (common.now() - common.parse_time(m["open_time"])).total_seconds()
            < 15 + 60
            and self.ready(asset, history=True)
        )

    def pause(self):
        with self.review_lock:
            self.state["paused"] = True
            self.epoch += 1
            self.cancel_review.set()
            if self.future:
                self.future.cancel()
                try:
                    self.future.result(timeout=5)
                except Exception:
                    pass

    def start_run(self, hours=12, profit_percent=0):
        hours, profit = common.dec(hours), common.dec(profit_percent)
        if not hours.is_finite() or not common.dec(".25") <= hours <= 168:
            raise ValueError("Choose between 0.25 and 168 hours")
        if not profit.is_finite() or profit < 0:
            raise ValueError("Profit target must be zero or positive")
        equity = common.dec(account(self.state)["equity"])
        if equity <= 0:
            raise ValueError("Account balance must be positive")
        self.state.update(
            run_until=(common.now() + timedelta(hours=float(hours))).isoformat(),
            run_hours=str(hours),
            profit_target_percent=str(profit),
            run_start_equity=str(equity),
            run_start_realized=self.state["realized_pnl"],
            run_started_at=common.now().isoformat(),
            stop_reason=None,
            paused=False,
        )
        self.epoch += 1
        self.emit(
            "run_started",
            reason=f"Paper run started for {hours} hours",
            profit_target_percent=str(profit),
        )

    def expire_run(self):
        s = self.state
        if s["paused"]:
            return
        reason = None
        if s.get("run_until") and common.now() >= common.parse_time(s["run_until"]):
            reason = "Scheduled finish time reached"
        target = common.dec(s.get("profit_target_percent", "0"))
        if (
            target > 0
            and common.dec(s["realized_pnl"]) - common.dec(s["run_start_realized"])
            >= common.dec(s["run_start_equity"]) * target / 100
        ):
            reason = "Profit target reached"
        if reason:
            self.pause()
            s["stop_reason"] = reason
            self.emit("run_finished", reason=reason)

    def request_review(self, trigger):
        with self.review_lock:
            return self._request_review(trigger)

    def _request_review(self, trigger):
        self.expire_run()
        if self.state["paused"] or self.future or self.limits():
            return False
        if trigger == "market_entry_review":
            if self.state["paused"] or self.state["halted"] or self.limits():
                return False
            assets = [a for a in self.config["assets"] if self.review_due(a)]
            if not assets:
                return False
            # Persist the attempt before launching Codex: restart/error must never retry this market.
            for a in assets:
                self.state["reviewed_markets"][a] = self.snapshots[a]["ticker"]
            self.save_state()
        elif trigger == "manual_account_review":
            assets = list(self.config["assets"])
        else:
            raise ValueError("Unknown review trigger")
        self.last_codex = {
            "status": "running",
            "payload": self.payload(trigger, assets),
            "response": None,
        }
        self.review_epoch = self.epoch
        self.review_path = (
            self.data_dir / "codex-reviews" / (str(uuid.uuid4()) + ".json")
        )
        common.atomic_json(self.review_path, self.last_codex)
        common.atomic_json(self.data_dir / "codex-latest.json", self.last_codex)
        self.cancel_review = threading.Event()
        self.future = self.pool.submit(
            ai.codex_decision, self.last_codex["payload"], self.cancel_review
        )
        return True

    def complete_review(self):
        if not self.future or not self.future.done():
            return
        try:
            response = self.future.result()
            ai.validate_decisions(response)
            self.last_codex.update(status="complete", response=response)
            original = self.last_codex["payload"]
            self.emit(
                "codex", reason=response["reason"], decisions=response["decisions"]
            )
            if original["execution_allowed"]:
                for d in response["decisions"]:
                    snapshot = original["markets"][d["asset"]]
                    self.state["evaluations"][d["ticker"]] = {
                        "asset": d["asset"],
                        "action": d["action"],
                        "reviewed_at": original["at"],
                        "close_time": snapshot["close_time"],
                        "yes_ask": snapshot.get("yes_ask_dollars"),
                        "no_ask": snapshot.get("no_ask_dollars"),
                    }
            if (
                original["execution_allowed"]
                and not self.state["paused"]
                and self.review_epoch == self.epoch
            ):
                for d in response["decisions"]:
                    self.apply_decision(d, original)
        except Exception as exc:
            self.last_codex.update(
                status="error", response={"decisions": [], "reason": str(exc)}
            )
            self.emit("codex_error", reason=str(exc))
        common.atomic_json(self.review_path, self.last_codex)
        common.atomic_json(self.data_dir / "codex-latest.json", self.last_codex)
        self.future = None

    def close(self, container, key, price, reason, fee=Decimal(0)):
        s = self.state
        pos = s[container].pop(key)
        qty = common.dec(pos["size"])
        pnl = (
            (price - common.dec(pos["entry"])) * qty
            - common.dec(pos.get("entry_fee", "0"))
            - fee
        )
        s["cash"] = str(common.dec(s["cash"]) + price * qty - fee)
        s["realized_pnl"] = str(common.dec(s["realized_pnl"]) + pnl)
        s["trades"] += 1
        s["wins" if pnl > 0 else "losses"] += 1
        k = "gross_profit" if pnl > 0 else "gross_loss"
        s[k] = str(common.dec(s[k]) + abs(pnl))
        if container == "positions":
            s["phases"][pos["asset"]] = "AI_WAIT"
        self.emit(
            "exit",
            **pos,
            exit=str(price),
            exit_fee=str(fee),
            pnl=str(pnl),
            reason=reason,
        )

    def limits(self):
        s = self.state
        if (
            self.config["daily_loss"] < 0
            and common.dec(s["realized_pnl"]) <= self.config["daily_loss"]
        ):
            s["halted"] = "Total loss limit reached"
            if not s["paused"]:
                self.pause()
            s["stop_reason"] = s["halted"]
            return True
        return False

    def readiness_error(self, asset, history=False):
        m = self.snapshots.get(asset)
        if asset in self.errors:
            return self.errors[asset]
        if not m:
            return "Waiting for market data"
        if (
            not -2
            <= (common.now() - common.parse_time(m["received_at"])).total_seconds()
            <= 5
        ):
            return "Waiting for fresh Polymarket books"
        if common.minutes_left(m) <= 0:
            return "Waiting for the next market"
        if history:
            u = m.get("underlying", {})
            if (
                u.get("price") is None
                or not u.get("source_at")
                or not -2
                <= (common.now() - common.parse_time(u["source_at"])).total_seconds()
                <= 5
            ):
                return "Waiting for live Chainlink TWAP"
            if u.get("error"):
                return u["error"]
            if u.get("opening_price") is None:
                return "Opening tick missing; waiting for the next market"
            if u.get("history", {}).get("samples", 0) < 2:
                return "Collecting Chainlink history"
            ch = m.get("contract_history", {})
            if ch.get("error") or not all(
                ch.get("outcomes", {}).get(side) for side in ["UP", "DOWN"]
            ):
                return "Waiting for Polymarket contract history"
        return None

    def ready(self, asset, history=False):
        return self.readiness_error(asset, history) is None

    def apply_decision(self, d, original):
        self.expire_run()
        s = self.state
        a = d["asset"]
        action = d["action"]

        def reject(reason):
            self.emit(
                "decision_rejected",
                asset=a,
                ticker=d.get("ticker"),
                action=action,
                reason=reason,
            )

        if s["paused"]:
            return reject("Trading is paused")
        if (
            original.get("trigger") != "market_entry_review"
            or not original.get("execution_allowed")
            or a not in original.get("review_assets", [])
        ):
            return reject("No scheduled entry authority")
        if action == "WAIT":
            return
        if action not in ["ENTER_UP", "ENTER_DOWN"]:
            return reject(
                "Only entry decisions are allowed; positions hold to settlement"
            )
        age = (common.now() - common.parse_time(original["at"])).total_seconds()
        if not 0 <= age <= float(common.dec(d["valid_for_seconds"])):
            return reject("AI plan expired")
        if a not in self.config["assets"] or not self.ready(a):
            return reject("Market data unavailable or stale")
        m = self.snapshots[a]
        if (
            m["ticker"] != d["ticker"]
            or original["markets"].get(a, {}).get("ticker") != d["ticker"]
        ):
            return reject("Market changed")
        pos = s["positions"].get(a)
        prior = original["positions"].get(a)
        if (pos and prior and pos["opened_at"] != prior["opened_at"]) or bool(
            pos
        ) != bool(prior):
            return reject("Position changed since review")
        if pos or prior:
            return reject("Existing position is held to settlement")
        if s["halted"] or self.limits():
            return reject("Account loss limit blocks entry")
        if not self.ready(a, history=True):
            return reject(
                "Fresh Chainlink TWAP, opening tick and historical data required"
            )
        side = "UP" if action == "ENTER_UP" else "DOWN"
        price = market.quote(m, side)
        snapshot = original["markets"][a]
        estimated_up = common.dec(d["estimated_up_probability"])
        estimated_side = estimated_up if side == "UP" else 1 - estimated_up
        breakeven = common.dec(
            snapshot["signals"]["books"][side]["breakeven_win_probability"]
        )
        if estimated_side <= breakeven:
            return reject("AI probability estimate does not clear fees")
        old_price = common.dec(
            snapshot["yes_ask_dollars" if side == "UP" else "no_ask_dollars"]
        )
        underlying_change = common.dec(m["underlying"]["price"]) - common.dec(
            snapshot["underlying"]["price"]
        )
        adverse_underlying_drift = (
            -underlying_change if side == "UP" else underlying_change
        )
        adverse_contract_drift = price - old_price if price is not None else None
        underlying_limit = common.dec(d["max_underlying_drift_usd"])
        contract_limit = common.dec(d["max_contract_drift"])
        # Absorb tiny moves while the AI response is in flight. The price cap below
        # remains strict, so this never pays more than the AI approved.
        underlying_cap = underlying_limit + min(
            Decimal("2"), underlying_limit * Decimal(".1")
        )
        if adverse_underlying_drift > underlying_cap:
            return reject(
                f"BTC moved ${adverse_underlying_drift:.2f} against {side}; maximum allowed was ${underlying_cap:.2f}"
            )
        if adverse_contract_drift is None:
            return reject(f"{side} ask became unavailable")
        price_limit = common.dec(d["limit_price"])
        contract_cap = min(contract_limit + Decimal(".01"), price_limit - old_price)
        if adverse_contract_drift > contract_cap:
            return reject(
                f"{side} ask rose ${adverse_contract_drift:.2f}; maximum allowed was ${contract_cap:.2f}"
            )
        qty = common.dec(d["quantity"])
        if price is None or not 0 < price < 1 or price > price_limit:
            return reject("Ask above AI entry limit or unavailable")
        depth = m.get(("yes" if side == "UP" else "no") + "_ask_size_fp")
        if depth is None or common.dec(depth) < qty:
            return reject("Insufficient displayed entry size")
        fee = market.trade_fee(m, qty, price)
        cost = price * qty + fee
        c = self.config
        if qty < common.dec(m["order_limits"][side]["minimum"]) or common.dec(
            d["limit_price"]
        ) % common.dec(m["order_limits"][side]["tick"]):
            return reject("AI quantity or limit price violates market minimum/tick")
        if qty > c["size"] or cost > c["max_trade"] or cost > common.dec(s["cash"]):
            return reject("Quantity, spending cap or cash exceeded")
        exposure = sum(
            (
                common.dec(p["entry"]) * common.dec(p["size"])
                + common.dec(p.get("entry_fee", "0"))
                for p in list(s["positions"].values()) + list(s["pending"].values())
            ),
            common.dec(0),
        )
        if (
            c["daily_loss"] < 0
            and common.dec(s["realized_pnl"]) - exposure - cost < c["daily_loss"]
        ):
            return reject("Worst-case exposure exceeds remaining loss budget")
        s["cash"] = str(common.dec(s["cash"]) - cost)
        pos = dict(
            asset=a,
            ticker=d["ticker"],
            side=side,
            entry=str(price),
            entry_fee=str(fee),
            size=str(qty),
            last_mark=str(market.quote(m, side, "bid") or 0),
            opened_at=common.now().isoformat(),
            close_time=m["close_time"],
            mark_at=m["received_at"],
        )
        s["positions"][a] = pos
        s["phases"][a] = "IN_POSITION"
        self.emit("entry", **pos, cost=str(cost), reason=d["reason"])

    def tick(self):
        self.expire_run()
        if time.monotonic() - self.last_cleanup >= 3600:
            self.last_cleanup = time.monotonic()
            try:
                removed = storage.prune_storage(
                    active_review=getattr(self, "review_path", None)
                    if self.future
                    else None,
                    history=getattr(
                        getattr(self.feed, "chainlink", None), "history", None
                    ),
                    data_dir=self.data_dir,
                )
                self.errors.pop("storage", None)
                if any(removed.values()):
                    self.emit("storage_cleanup", **removed)
            except Exception:
                self.errors["storage"] = (
                    "Storage cleanup failed; next attempt in one hour"
                )
        s = self.state
        c = self.config
        assets = list(dict.fromkeys(c["assets"] + list(s["positions"])))
        jobs = {a: self.feed_pool.submit(self.feed.snapshot, a, c) for a in assets}
        for a, f in jobs.items():
            try:
                self.snapshots[a] = f.result()
                self.errors.pop(a, None)
            except Exception as exc:
                self.errors[a] = str(exc)
        for a, pos in list(s["positions"].items()):
            if not pos.get("close_time"):
                try:
                    pos["close_time"] = self.feed.market(pos["ticker"])["close_time"]
                except Exception as exc:
                    self.errors[a] = str(exc)
                    continue
            if common.parse_time(pos["close_time"]) <= common.now():
                s["pending"][pos["ticker"]] = s["positions"].pop(a)
                self.emit("pending_settlement", asset=a, ticker=pos["ticker"])
                continue
            m = self.snapshots.get(a)
            if self.ready(a) and m["ticker"] == pos["ticker"]:
                price = market.quote(m, pos["side"], "bid")
                if price is not None:
                    pos.update(last_mark=str(price), mark_at=m["received_at"])
        for ticker, pos in list(s["pending"].items()):
            if time.monotonic() - self.settle_checked.get(ticker, 0) < 10:
                continue
            self.settle_checked[ticker] = time.monotonic()
            try:
                result = self.feed.market(ticker).get("payouts")
                if result is not None:
                    payout = common.dec(result[pos["side"]])
                    if not payout.is_finite() or not 0 <= payout <= 1:
                        raise ValueError("Invalid resolved payout")
                    self.close("pending", ticker, payout, "held_to_resolution")
                    self.settle_checked.pop(ticker, None)
                self.errors.pop("settlement:" + ticker, None)
            except Exception as exc:
                self.errors["settlement:" + ticker] = str(exc)
        for ticker, evaluation in list(s["evaluations"].items()):
            if ticker in s["positions"] or ticker in s["pending"]:
                continue
            check = "evaluation:" + ticker
            if time.monotonic() - self.settle_checked.get(check, 0) < 10:
                continue
            self.settle_checked[check] = time.monotonic()
            try:
                if common.parse_time(evaluation["close_time"]) > common.now():
                    continue
                result = self.feed.market(ticker)
                payouts = result.get("payouts")
                if payouts is None:
                    continue
                s["evaluations"].pop(ticker)
                self.settle_checked.pop(check, None)
                self.emit(
                    "review_outcome", ticker=ticker, **evaluation, payouts=payouts
                )
            except Exception:
                pass
        for a in c["assets"]:
            m = self.snapshots.get(a)
            if not m or a in self.errors:
                s["phases"][a] = "WAIT_DATA"
                continue
            if s["sessions"].get(a) != m["ticker"]:
                s["sessions"][a] = m["ticker"]
                self.emit("session", asset=a, ticker=m["ticker"])
            elapsed = (common.now() - common.parse_time(m["open_time"])).total_seconds()
            s["phases"][a] = (
                "HOLD_TO_SETTLEMENT"
                if a in s["positions"]
                else "REVIEW_USED"
                if s["reviewed_markets"].get(a) == m["ticker"]
                else "SKIPPED_WINDOW"
                if elapsed >= 15 + 60
                else "WAIT_REVIEW_TIME"
                if elapsed < 15
                else "READY_FOR_REVIEW"
                if self.ready(a, history=True)
                else "WAIT_DATA"
            )
        self.limits()
        self.complete_review()
        self.request_review("market_entry_review")
        self.save_state()
