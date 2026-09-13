# BTC paper trading

Independent 5-minute and 15-minute Polymarket paper accounts. Codex CLI (Terra Medium) reviews each market once; no real orders or wallet connection.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `backend/dashboard.py` | HTTP API, live SSE dashboard updates, account workers |
| `backend/engine.py` | Account lifecycle, review timing, limits, fills and settlement |
| `backend/feeds.py` | Public market discovery and live feed lifecycle |
| `backend/market.py` | Market/book validation, fees and price signals |
| `backend/storage.py` | PostgreSQL price history and review retention |
| `backend/ai.py` | Structured AI briefing, Codex subprocess, response validation |
| `backend/common.py` | Shared paths, settings, value helpers and atomic JSON writes |
| `web/` | Dashboard HTML, CSS and JavaScript; no build step |
| `feeds/chainlink.mjs` | Native Node WebSocket relay for TWAP and order books |
| `infra/railway_start.py` | Private dashboard and Cloudflare Tunnel supervisor |
| `.railway/railway.ts` | Railway infrastructure configuration |
| `tests/` | Isolated engine, HTTP/SSE and PostgreSQL checks |
| `docs/` | AI context, deployment and original venue research |

Dependencies flow from the dashboard to the engine, then to AI, feeds, market validation and storage. Shared helpers do not import those modules. The browser communicates only with dashboard routes.

## Development

Requires Python 3.11+, psycopg2, PostgreSQL, Node 22+ and Codex CLI 0.154.0 or newer. Set `DATABASE_URL` to your development database and sign in with `codex login`. Docker includes the Python database driver and pinned CLI.

```sh
bun run start
bun run test
bun run check
bun run format
```

Open http://127.0.0.1:8765. Formatting/check commands use Bun and uv with pinned formatter versions; they are development tools, not runtime dependencies.

Run database checks **only against a disposable database**:

```sh
DATABASE_URL=... python3 -m tests.test_postgres
```

## Runtime behavior

- A stopped account stays stopped after restart. An active timed run resumes only until its existing deadline. Each account has its own balance, limits and review history.
- Start enables a timed session, including waiting for data. AI reviews and entries still require fresh validated data.
- Reviews start 15 seconds after opening for both durations, with a 60-second dispatch window (15–under 75 seconds) to wait for validated data. Codex returns a short-lived conditional plan; the current BTC price and selected contract ask must remain inside its limits before entry. Missed windows, WAIT decisions and errors skip that market.
- Codex has a 25-second timeout. Decisions older than 30 seconds are rejected. Pause cancels AI; open trades continue to official settlement.
- Every scheduled review is labeled with the eventual Up/Down settlement. This includes WAIT decisions, so later analysis can compare skipped opportunities with executed trades.
- Account loss limits and profit targets stop new entries. There is no per-position early-exit stop-loss.
- The visible dashboard uses a compact SSE connection, capped at one update per second and closed while its browser tab is hidden. Start/Stop and settings use HTTP POST; trade and AI history load by HTTP only when the event version changes.

## Data and persistence

PostgreSQL stores received BTC Chainlink 60-second TWAP observations for 24 hours. There is no guaranteed historical backfill. AI receives structured recent candles, broader history, live and public market activity, book depth, fees and account context; see [AI context](docs/AI_CONTEXT.md).

The volume paths remain unchanged:
- `data/polymarket/`: 15-minute account and reviews.
- `data/polymarket/5m/`: 5-minute account and reviews.
- `/app/data/codex/`: Railway Codex login.

Detailed review files expire after 30 days. Trade/account history remains. Live books stay in memory. Account writes are atomic and occur only after a state change; price history is shared between accounts.

The browser never receives raw order books, provider metadata, signal history or AI input payloads. Those remain inside the trading service. The browser caches one 20-decision history page for each tab; PostgreSQL remains the only database, so Redis is unnecessary at this traffic level.

See [deployment](docs/RAILWAY_DEPLOYMENT.md) and [venue research](docs/POLYMARKET_RESEARCH.md).
