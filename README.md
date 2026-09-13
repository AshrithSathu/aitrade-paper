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
- Codex CLI: choose ENTER_UP, ENTER_DOWN or WAIT once per market, including entry quantity and maximum entry price.
- Local dashboard: controls, positions, PnL, data health, exact AI inputs/outputs and history.

Read [the research and sources](POLYMARKET_RESEARCH.md) for the venue-specific details and verified endpoints.

## AI-only decisions

The only trading mode is one entry review three minutes into each 15-minute market. Dispatch is allowed from 180 to under 240 seconds after opening, allowing for feed/processing delays. It requires fresh books, underlying price, exact opening tick and history. Missing that window skips the market. Selected assets that become ready together share one Codex call; each asset/market gets at most one scheduled attempt.

AI chooses UP, DOWN or WAIT and the quantity/maximum entry price. WAIT, errors, stale decisions or rejected fills skip that market; there are no automatic retries. The attempt is persisted before calling Codex, so pause/resume or restart cannot repeat it. Positions are held until official settlement, without early AI exits, stop-losses or take-profit rules. Continuous market collection and marking do not call AI.

AI decisions expire 30 seconds after their input snapshot. Before entry, code rechecks current market/position identity, freshness, AI price limit, displayed size, venue minimum quantity/tick and account budgets. No order waits around for a later fill.

Pause blocks entries and invalidates in-flight trading decisions. Marks and official settlement still update. Manual Review now is an additional preview-only call; it neither trades nor consumes the scheduled attempt. Boot always starts paused. Old `codex_interval` settings are discarded on startup.

## Data and warm-up

All seven listed crypto assets had current 15-minute Chainlink 60s TWAP markets during research. Unsupported sources/windows are rejected. The app never substitutes CFB, Coinbase, Binance spot or a locally invented TWAP.

Raw received TWAP observations persist in `data/polymarket/chainlink.sqlite`. Codex receives up to one hour of one-minute OHLC bars of these observations, sample counts, timestamp coverage and largest gap. It also receives separate UP/DOWN CLOB probability histories, full market metadata/rules, books and sizes, fees, all settings, account metrics, positions, pending settlements and recent events.

RTDS does not guarantee historical backfill. At cold start, the current opening tick may be missing: the UI says so and entries wait for the next market whose exact opening timestamp was recorded. Streaming/recording while paused does not place trades. History coverage grows as data arrives; missing observations are not invented.

## Paper execution and limits

Paper buys fill at the ask only when displayed size covers the quantity. Taker fees use current market parameters and are estimated in cash at entry. Positions are marked at bid until official settlement, which pays the outcome value without a simulated sell fee. PnL includes the entry fee. No additional slippage, market impact, rebates or exact wallet fee accounting is simulated.

The default maximum is five shares, matching the observed venue minimum, with a $10 maximum spend per trade including entry fee. AI can choose a smaller quantity only if the market permits it. Hard limits also include available cash and cumulative loss budget. The worst-case cost of active/pending exposure and the proposed entry must fit the remaining budget. Reaching realized loss budget blocks new entries but does not force exits. Zero disables the budget; it does not reset at midnight.

Expired positions await the official closed-market winning-token result or explicit 50/50 payout. Closing time, last traded price and an AI prediction cannot trigger a settlement payment.

## Verification and storage

`python3 test_dashboard.py` runs isolated temporary-account checks: market/source/window validation, reversed token mappings, book ordering/depth/timestamps, exact TWAP parsing and persistence, missing opening ticks, fee calculation, one-review scheduling, persisted attempts across restarts, errors and missed windows, limits, pause/preview isolation, stale decisions and hold-to-settlement accounting exactly once. No real paper account is traded by these tests.

The dashboard owns one process lock and atomically saves account/events in `data/polymarket/state.json`. Exact reviews are archived in `data/polymarket/codex-reviews/`; `/api/history` exposes all account events. The old Kalshi state remains under `data/` and the migration backup under `data/backups/before-polymarket/`. It is not mixed into the Polymarket account.

This is a local simulation. No wallet connection, real-order endpoint, onchain transaction or automatic trading start is implemented.
