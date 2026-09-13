"""PostgreSQL observation history and review retention."""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import common


def prune_storage(at=None, active_review=None, history=None, data_dir=None):
    """Retain 24h of useful data and remove abandoned Codex temporary files."""
    at = time.time() if at is None else at
    removed_ticks = history.prune(int((at - 86400) * 1000)) if history else 0
    removed_reviews = 0
    for path in ((data_dir or common.DATA) / "codex-reviews").glob("*.json"):
        if (
            path == active_review
            or path.is_symlink()
            or not re.fullmatch(r"[0-9a-f-]{36}\.json", path.name)
        ):
            continue
        if path.stat().st_mtime >= at - 86400:
            continue
        review = json.loads(path.read_text())
        if review.get("status") not in ("complete", "error", "interrupted", "running"):
            continue
        if common.parse_time(review["payload"]["at"]).timestamp() < at - 86400:
            path.unlink()
            removed_reviews += 1
    removed_codex_temp = 0
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        temporary = Path(codex_home) / "tmp" / "arg0"
        for path in temporary.glob("codex-arg0*"):
            try:
                if (
                    path.is_dir()
                    and not path.is_symlink()
                    and path.stat().st_mtime < at - 300
                ):
                    shutil.rmtree(path)
                    removed_codex_temp += 1
            except FileNotFoundError:
                pass
    return dict(
        removed_ticks=removed_ticks,
        removed_reviews=removed_reviews,
        removed_codex_temp=removed_codex_temp,
    )


class TickStore:
    def __init__(self):
        from psycopg2.pool import ThreadedConnectionPool

        self.pool = ThreadedConnectionPool(
            1,
            10,
            os.environ["DATABASE_URL"],
            connect_timeout=5,
            options="-c statement_timeout=10000",
        )
        with self.connection() as db:
            with db.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS ticks (asset TEXT NOT NULL,timestamp BIGINT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(asset,timestamp))"
                )
                cur.execute("CREATE INDEX IF NOT EXISTS ticks_time ON ticks(timestamp)")
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS history_migrations (name TEXT PRIMARY KEY)"
                )
                cur.execute("SELECT 1 FROM history_migrations WHERE name='sqlite-v1'")
                migrated = cur.fetchone()
                legacy = common.DATA / "chainlink.sqlite"
                if not migrated and legacy.exists():
                    from psycopg2.extras import execute_values

                    with sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True) as old:
                        rows = old.execute(
                            "SELECT asset,timestamp,value FROM ticks"
                        ).fetchall()
                    if rows:
                        execute_values(
                            cur,
                            "INSERT INTO ticks VALUES %s ON CONFLICT DO NOTHING",
                            rows,
                            page_size=1000,
                        )
                        cur.execute("SELECT asset,timestamp,value FROM ticks")
                        if not set(rows).issubset(set(cur.fetchall())):
                            raise ValueError("History migration verification failed")
                    cur.execute("INSERT INTO history_migrations VALUES ('sqlite-v1')")
        atexit.register(self.close)

    def close(self):
        if not self.pool.closed:
            self.pool.closeall()

    @contextmanager
    def connection(self):
        db = self.pool.getconn()
        try:
            with db:
                yield db
        finally:
            self.pool.putconn(db, close=bool(db.closed))

    def append(self, rows):
        from psycopg2.extras import execute_values

        with self.connection() as db, db.cursor() as cur:
            execute_values(
                cur, "INSERT INTO ticks VALUES %s ON CONFLICT DO NOTHING", rows
            )

    def window(self, asset, start):
        with self.connection() as db, db.cursor() as cur:
            cur.execute(
                "SELECT timestamp,value FROM ticks WHERE asset=%s AND timestamp>=%s ORDER BY timestamp",
                (asset, int(time.time() // 60) * 60000 - 3600000),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT value FROM ticks WHERE asset=%s AND timestamp=%s",
                (asset, start),
            )
            return rows, cur.fetchone()

    def context24(self, asset):
        with self.connection() as db, db.cursor() as cur:
            cur.execute(
                """WITH observations AS (
                SELECT timestamp,value, timestamp-lag(timestamp) OVER (ORDER BY timestamp) AS gap
                FROM ticks WHERE asset=%s AND timestamp>=%s)
                SELECT (timestamp/900000)*900000 AS bucket,min(timestamp),max(timestamp),
                (array_agg(value ORDER BY timestamp))[1],(array_agg(value ORDER BY timestamp DESC))[1],
                min(value::numeric)::text,max(value::numeric)::text,count(*),max(gap)
                FROM observations GROUP BY bucket ORDER BY bucket""",
                (asset, int((time.time() - 86400) * 1000)),
            )
            bars = [
                dict(
                    t=r[0],
                    first_at=r[1],
                    last_at=r[2],
                    open=r[3],
                    close=r[4],
                    low=r[5],
                    high=r[6],
                    samples=r[7],
                    max_gap_ms=r[8],
                )
                for r in cur.fetchall()
            ]
        return dict(
            requested_hours=24,
            resolution="15-minute OHLC of recorded TWAP observations",
            bars=bars,
            available_hours=(bars[-1]["last_at"] - bars[0]["first_at"]) / 3600000
            if bars
            else 0,
            limitation="Recorded history only; no older backfill. Boundary bars may be partial; sample counts and gaps are explicit.",
        )

    def prune(self, cutoff):
        with self.connection() as db, db.cursor() as cur:
            cur.execute("DELETE FROM ticks WHERE timestamp<%s", (cutoff,))
            return cur.rowcount
