# Polymarket AI paper desk

Run `python3 dashboard.py`, then open http://127.0.0.1:8765. Requires Python 3.11+, Node 22+ (native WebSocket) and a signed-in Codex CLI. No third-party Python or npm packages are required.

**Local paper trading requires no environment variables, wallet, private key, Polymarket API credentials or funds.** Railway uses a tunnel credential and public origin; see [deployment](RAILWAY_DEPLOYMENT.md). Old Kalshi keys are unused. The server always starts paused. Do not start trading until the user explicitly authorizes it.

## Files

- `paper_trader.py`: market data, SQLite history, AI reviews and paper accounting.
- `dashboard.py`: HTTP API and engine lifecycle.
- `dashboard.html` / `dashboard.js`: browser interface, with no build step.
- `chainlink.mjs`: native Node WebSocket feed.
- `railway_start.py`: private dashboard and Cloudflare Tunnel supervisor.
- `test_dashboard.py`: isolated engine checks.

Bun scripts are optional shortcuts (`bun run start`, `bun run test`); the project has no npm dependencies or Wrangler build.

## Architecture

- Public Gamma API: discover the current 15-minute market, rules, window and outcome tokens.
- Public CLOB API: UP/DOWN books, current fee/size/tick parameters, outcome-price history and official settlement.
- Public RTDS: Chainlink 60-second TWAP observations, collected through `chainlink.mjs`.
- Python: record data, construct Codex snapshots, validate responses, enforce budgets and simulate fills.
- Codex CLI: choose ENTER_UP, ENTER_DOWN, WAIT, HOLD or EXIT, including entry quantity and acceptable execution price.
- Local dashboard: controls, positions, PnL, data health, exact AI inputs/outputs and history.

Read [the research and sources](POLYMARKET_RESEARCH.md) for the venue-specific details and verified endpoints.

## AI-only decisions

There are no fixed entry ranges, time windows, price stops, take-profit targets, Observe/Gate switches or one-trade-per-market restrictions. Reviews run at the configured interval (default 60 seconds), and on a new market when the reviewer is free. Only one review runs at a time; the next review uses fresh data instead of a stale queue.

AI decisions expire 30 seconds after their input snapshot. Before execution, code rechecks current market/position identity, data freshness, AI price limits, displayed size, venue minimum quantity/tick and account budgets. WAIT/HOLD can be reconsidered. EXIT closes the entire position. Invalid, stale or failed reviews cause no orders; an existing position stays open pending a later decision or settlement.

Pause blocks all AI orders, including exits, and invalidates in-flight trading decisions. Price marks and official settlement accounting still update. Manual Review now is always a preview, even while enabled. Restart never replays saved decisions.

## Data and warm-up

All seven listed crypto assets had current 15-minute Chainlink 60s TWAP markets during research. Unsupported sources/windows are rejected. The app never substitutes CFB, Coinbase, Binance spot or a locally invented TWAP.

Raw received TWAP observations persist in `data/polymarket/chainlink.sqlite`. Codex receives up to one hour of one-minute OHLC bars of these observations, sample counts, timestamp coverage and largest gap. It also receives separate UP/DOWN CLOB probability histories, full market metadata/rules, books and sizes, fees, all settings, account metrics, positions, pending settlements and recent events.

RTDS does not guarantee historical backfill. At cold start, the current opening tick may be missing: the UI says so and entries wait for the next market whose exact opening timestamp was recorded. Streaming/recording while paused does not place trades. History coverage grows as data arrives; missing observations are not invented.

## Paper execution and limits

Paper buys fill at the ask and sells at the bid, only when top-level displayed size covers the quantity. Taker fees use the market's current parameters and are estimated in cash at entry/exit. PnL includes those fees; open positions are marked at bid before any future exit fee. No additional slippage, market impact, rebates or exact wallet fee accounting is simulated.

The default maximum is five shares, matching the observed venue minimum, with a $10 maximum spend per trade including entry fee. AI can choose a smaller quantity only if the market permits it. Hard limits also include available cash and cumulative loss budget. The worst-case cost of active/pending exposure and the proposed entry must fit the remaining budget. Reaching realized loss budget blocks new entries but does not force exits. Zero disables the budget; it does not reset at midnight.

Expired positions await the official closed-market winning-token result or explicit 50/50 payout. Closing time, last traded price and an AI prediction cannot trigger a settlement payment.

## Verification and storage

`python3 test_dashboard.py` runs isolated temporary-account checks: market/source/window validation, reversed token mappings, book ordering/depth/timestamps, exact TWAP parsing and persistence, missing opening ticks, fee calculation, AI-only execution, limits, pause/preview isolation, stale decisions and settlement exactly once. No real paper account is traded by these tests.

The dashboard owns one process lock and atomically saves account/events in `data/polymarket/state.json`. Exact reviews are archived in `data/polymarket/codex-reviews/`; `/api/history` exposes all account events. The old Kalshi state remains under `data/` and the migration backup under `data/backups/before-polymarket/`. It is not mixed into the Polymarket account.

This is a local simulation. No wallet connection, real-order endpoint, onchain transaction or automatic trading start is implemented.
