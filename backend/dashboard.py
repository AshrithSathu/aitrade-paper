"""Local UI and lifecycle owner for the Polymarket paper engine."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import random
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from backend import common, feeds
from backend import engine as trading

lock = threading.Lock()
idle = threading.Condition(lock)
engines = {}
views = {}
settings_by = {}
login = {
    "running": False,
    "output": "",
    "authenticated": False,
    "status": "Checking Codex login",
    "checked_at": 0,
}


def refresh_login():
    if login["running"] or time.monotonic() - login["checked_at"] < 60:
        return
    try:
        result = subprocess.run(
            ["codex", "login", "status"], capture_output=True, text=True, timeout=5
        )
        login.update(
            authenticated=result.returncode == 0,
            status="Codex connected"
            if result.returncode == 0
            else "Codex sign-in required",
        )
    except (OSError, subprocess.TimeoutExpired):
        login.update(authenticated=False, status="Could not check Codex login")
    login["checked_at"] = time.monotonic()


def codex_login():
    try:
        process = subprocess.Popen(
            ["codex", "login", "--device-auth"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        timer = threading.Timer(600, process.kill)
        timer.start()
        try:
            for line in process.stdout:
                with lock:
                    login["output"] = (login["output"] + line)[-4000:]
            process.wait()
        finally:
            timer.cancel()
    except Exception:
        with lock:
            login["output"] = "Login failed. Retry login."
    finally:
        with lock:
            login["running"] = False
            login["checked_at"] = 0
            refresh_login()


def allowed_origins(public_origin):
    origins = [None, "http://127.0.0.1:8765", "http://localhost:8765"]
    if public_origin:
        url = urlsplit(public_origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.port not in [None, 443]
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "PUBLIC_ORIGIN must be an exact HTTPS origin without a path or credentials"
            )
        origins.append(public_origin)
    return origins


ORIGINS = allowed_origins(os.environ.get("PUBLIC_ORIGIN", ""))


def restore_run(engine):
    if engine.state.get("paused") is False and engine.state.get("run_until"):
        engine.expire_run()
    else:
        engine.pause()


def publish(engine, view):
    state = engine.state
    market = engine.snapshots.get("BTC") or {}
    underlying = market.get("underlying") or {}
    review = engine.last_codex or {}
    payload = review.get("payload") or {}
    review_market = (payload.get("markets") or {}).get("BTC") or {}
    view.update(
        state=copy.deepcopy(
            {
                key: state.get(key)
                for key in (
                    "halted",
                    "stop_reason",
                    "positions",
                    "pending",
                    "phases",
                    "run_until",
                )
            }
        ),
        account=trading.account(state),
        markets={
            "BTC": copy.deepcopy(
                {
                    **{
                        key: market.get(key)
                        for key in (
                            "ticker",
                            "open_time",
                            "close_time",
                            "received_at",
                            "yes_ask_dollars",
                            "no_ask_dollars",
                        )
                    },
                    "underlying": {
                        key: underlying.get(key)
                        for key in (
                            "price",
                            "opening_price",
                            "delta",
                            "source_at",
                            "error",
                        )
                    },
                }
            )
            if market
            else {},
        },
        errors=copy.deepcopy(engine.errors),
        codex=copy.deepcopy(
            {
                "status": review.get("status"),
                "response": review.get("response"),
                "payload": {
                    "at": payload.get("at"),
                    "markets": {"BTC": {"ticker": review_market.get("ticker")}},
                },
            }
        )
        if review
        else None,
        event_version=len(state["events"]),
        revision=view.get("revision", 0) + 1,
    )


def status(minutes):
    engine = engines[minutes]
    data = copy.deepcopy(views[minutes])
    data.update(
        settings=settings_by[minutes],
        paused=engine.state["paused"],
        waiting_for=engine.readiness_error("BTC", history=True),
    )
    data["modes"] = {
        m: {
            k: e.state.get(k)
            for k in ("paused", "halted", "run_hours", "profit_target_percent")
        }
        for m, e in engines.items()
    }
    return data


def worker(engine, view):
    while True:
        with lock:
            view["busy"] = True
        try:
            engine.tick()
            with lock:
                view.update(error=None, updated=common.now().isoformat())
        except Exception as exc:
            engine.pause()
            with lock:
                view["error"] = str(exc)
        finally:
            with lock:
                publish(engine, view)
                view["busy"] = False
                idle.notify_all()
        interval = float(engine.config["interval"])
        if engine.state["paused"] and not any(
            engine.state[key] for key in ("positions", "pending")
        ):
            interval = max(2, interval)
        time.sleep(interval + random.uniform(0, float(engine.config["jitter"])))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data, mime="application/json", cache="no-store"):
        body = (
            data if isinstance(data, bytes) else json.dumps(data, default=str).encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def local(self):
        return self.headers.get("Host") in ["127.0.0.1:8765", "localhost:8765"]

    def context(self):
        minutes = parse_qs(urlsplit(self.path).query).get("minutes", ["15"])[0]
        if minutes not in ("5", "15"):
            raise ValueError("Choose 5 or 15 minutes")
        return minutes, engines[minutes], views[minutes]

    def do_GET(self):
        if not self.local():
            return self.send(403, {"error": "Local requests only"})
        try:
            minutes, engine, view = self.context()
        except ValueError as exc:
            return self.send(400, {"error": str(exc)})
        path = urlsplit(self.path).path
        if path == "/api/codex/login":
            with lock:
                refresh_login()
                return self.send(200, dict(login))
        static = {
            "/": ("dashboard.html", "text/html; charset=utf-8"),
            "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
            "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
            "/manifest.webmanifest": (
                "manifest.webmanifest",
                "application/manifest+json",
            ),
            "/service-worker.js": (
                "service-worker.js",
                "text/javascript; charset=utf-8",
            ),
            "/icon-192.png": ("icon-192.png", "image/png"),
            "/icon-512.png": ("icon-512.png", "image/png"),
        }
        if path in static:
            filename, mime = static[path]
            return self.send(
                200,
                (common.ROOT / "web" / filename).read_bytes(),
                mime,
                "no-cache",
            )
        if path == "/api/status":
            with lock:
                data = status(minutes)
            return self.send(200, data)
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.connection.settimeout(20)
            last_revisions = None
            last_write = 0.0
            try:
                while True:
                    with lock:
                        refresh_login()
                        revisions = tuple(views[m]["revision"] for m in engines)
                        elapsed = time.monotonic() - last_write
                        if last_revisions is None or (
                            revisions != last_revisions and elapsed >= 1
                        ):
                            data = {
                                "accounts": {m: status(m) for m in engines},
                                "login": dict(login),
                            }
                            message = (
                                "data: " + json.dumps(data, default=str) + "\n\n"
                            ).encode()
                            last_revisions = revisions
                            last_write = time.monotonic()
                        elif elapsed >= 15:
                            message = b": keepalive\n\n"
                            last_write = time.monotonic()
                        else:
                            message = None
                            wait = min(15 - elapsed, max(0.05, 1 - elapsed))
                    if message:
                        self.wfile.write(message)
                        self.wfile.flush()
                    with idle:
                        idle.wait(timeout=wait if message is None else 1)
            except (OSError, TimeoutError):
                pass
            return
        if path == "/api/history":
            try:
                page = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
                if page < 1:
                    raise ValueError
            except ValueError:
                return self.send(400, {"error": "Page must be a positive number"})
            with lock:
                events = engine.state["events"]
                decisions = [event for event in events if event.get("kind") == "codex"]
                pages = max(1, (len(decisions) + 19) // 20)
                page = min(page, pages)
                selected = list(reversed(decisions))[(page - 1) * 20 : page * 20]
                tickers = {
                    decision.get("ticker")
                    for event in selected
                    for decision in event.get("decisions", [])
                }
                data = {
                    "version": len(events),
                    "page": page,
                    "pages": pages,
                    "total": len(decisions),
                    "decisions": copy.deepcopy(selected),
                    "entries": [
                        copy.deepcopy(event)
                        for event in events
                        if event.get("kind") == "entry"
                        and event.get("ticker") in tickers
                    ],
                    "results": [
                        copy.deepcopy(event)
                        for event in events
                        if event.get("kind") == "exit"
                        and event.get("ticker") in tickers
                    ],
                    "rejections": {
                        event["ticker"]: event["reason"]
                        for event in events
                        if event.get("kind") == "decision_rejected"
                        and event.get("ticker") in tickers
                    },
                    "outcomes": {
                        event["ticker"]: copy.deepcopy(event["payouts"])
                        for event in events
                        if event.get("kind") == "review_outcome"
                        and event.get("ticker") in tickers
                    },
                    "trades": copy.deepcopy(
                        [event for event in events if event.get("kind") == "exit"][
                            -100:
                        ]
                    ),
                }
            return self.send(200, data)
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        if (
            not self.local()
            or self.headers.get("Origin") not in ORIGINS
            or self.headers.get("Content-Type") != "application/json"
        ):
            return self.send(403, {"error": "Local JSON requests only"})
        try:
            minutes, engine, view = self.context()
            path = urlsplit(self.path).path
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size < 12000:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(size))
            with lock:
                s = engine.state
                if path == "/api/codex/login":
                    if (
                        any(not e.state["paused"] or e.future for e in engines.values())
                        or login["running"]
                    ):
                        raise ValueError(
                            "Pause and wait for the current review or login"
                        )
                    login.update(
                        running=True, output="Starting separate cloud Codex login..."
                    )
                    threading.Thread(target=codex_login, daemon=True).start()
                elif path == "/api/pause":
                    engine.pause()
                elif path == "/api/start":
                    if view["error"]:
                        raise ValueError("Resolve the worker error before starting")
                    if s["halted"]:
                        raise ValueError(s["halted"])
                    if not idle.wait_for(lambda: not view["busy"], timeout=30):
                        raise ValueError("Wait for the current data refresh")
                    if body.get("resume_run") is True:
                        if not s.get("run_until") or common.now() >= common.parse_time(
                            s["run_until"]
                        ):
                            raise ValueError("The previous run has ended")
                        s["paused"] = False
                        engine.epoch += 1
                        engine.expire_run()
                    else:
                        engine.start_run(
                            body.get("duration_hours", 12),
                            body.get("profit_target_percent", 0),
                        )
                elif path == "/api/review":
                    if (
                        not idle.wait_for(lambda: not view["busy"], timeout=30)
                        or engine.future
                    ):
                        raise ValueError(
                            "A feed update or Codex review is running. Try again shortly."
                        )
                    if not engine.request_review("manual_account_review"):
                        raise ValueError(
                            "Resume paper trading before requesting an AI review"
                        )
                elif path == "/api/settings":
                    if (
                        not idle.wait_for(lambda: not view["busy"], timeout=30)
                        or not s["paused"]
                        or engine.future
                        or s["positions"]
                        or s["pending"]
                    ):
                        raise ValueError(
                            "Pause, wait for open positions/settlements and the current review to finish, then save."
                        )
                    if str(body.get("market_minutes")) != minutes:
                        raise ValueError("Settings belong to another tab")
                    checked = common.validate(body)
                    if checked["balance"] != common.dec(s["initial_balance"]):
                        if s["trades"]:
                            raise ValueError(
                                "Starting cash cannot change after trading begins"
                            )
                        s["initial_balance"] = s["cash"] = str(checked["balance"])
                    common.atomic_json(engine.data_dir / "settings.json", body)
                    settings_by[minutes] = body
                    engine.config = checked
                    engine.epoch += 1
                    engine.snapshots = {}
                    engine.errors = {}
                    if s["halted"] and not engine.limits():
                        s["halted"] = None
                else:
                    raise ValueError("Unknown action")
                if not view["busy"]:
                    engine.save_state()
                    publish(engine, view)
                idle.notify_all()
            self.send(200, {"ok": True})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {"error": str(exc)})


if __name__ == "__main__":
    common.DATA.mkdir(parents=True, exist_ok=True)
    process_lock = (common.DATA / "paper_trader.lock").open("w")
    try:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A paper trader is already running")
    reset_marker = common.DATA / ".reset_requested"
    if reset_marker.exists():
        import shutil

        import psycopg2

        with psycopg2.connect(os.environ["DATABASE_URL"]) as db, db.cursor() as cur:
            cur.execute("TRUNCATE ticks, history_migrations")
        for path in common.DATA.iterdir():
            if path.name in ("paper_trader.lock", ".reset_requested"):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        reset_marker.unlink()
    feed = feeds.Feed()
    for minutes in ("15", "5"):
        directory = common.DATA if minutes == "15" else common.DATA / "5m"
        directory.mkdir(parents=True, exist_ok=True)
        settings = copy.deepcopy(common.DEFAULTS)
        if (directory / "settings.json").exists():
            previous = json.loads((directory / "settings.json").read_text())
            settings.update({k: v for k, v in previous.items() if k in settings})
        settings.update(assets=["BTC"], market_minutes=minutes)
        state = (
            json.loads((directory / "state.json").read_text())
            if (directory / "state.json").exists()
            else common.initial_state(settings["balance"])
        )
        if state.get("venue") != "polymarket":
            raise ValueError("Wrong paper account venue")
        engine = trading.Engine(state, settings, feed, data_dir=directory)
        restore_run(engine)
        view = {"busy": False, "error": None, "updated": None}
        engines[minutes] = engine
        views[minutes] = view
        settings_by[minutes] = settings
        engine.save_state(force=True)
        common.atomic_json(directory / "settings.json", settings)
        publish(engine, view)
    for minutes, engine in engines.items():
        threading.Thread(
            target=worker, args=(engine, views[minutes]), daemon=True
        ).start()
    print("Paper console: http://127.0.0.1:8765", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
    except KeyboardInterrupt:
        for engine in engines.values():
            engine.pause()
