# Polymarket migration research — 13 September 2026

## Findings and implementation choices

1. **Public data needs no wallet authentication.** Gamma discovers markets; CLOB supplies outcome books, market parameters and probability history. Live reads from this Mac succeeded with ordinary HTTP requests. No VPN settings were changed. Sources: [market data overview](https://docs.polymarket.com/market-data/overview), [orderbook schema](https://docs.polymarket.com/api-reference/market-data/get-order-book).

2. **These markets now specify Chainlink 60-second TWAP.** Live Gamma results for BTC, ETH, SOL, XRP, DOGE, HYPE and BNB contained `cryptoMarketConfig.twapLookbackSeconds=60` and the asset's Chainlink TWAP resolution URL. The adapter rejects a different source/window instead of silently using spot prices or CFB. Example: [BTC market rules](https://polymarket.com/event/btc-updown-15m-1789263000).

3. **RTDS provides the matching stream without credentials.** Subscribe to `crypto_prices_twap_sixty`; send `PING` every five seconds. We verified live updates and preserve `full_accuracy_value` as an exact E18 decimal, using the observation timestamp for freshness. Native Node 22 WebSocket avoids adding a package. Source: [Chainlink TWAP integration](https://docs.polymarket.com/market-data/chainlink-twap).

4. **Do not promise historical TWAP backfill.** The documentation says RTDS has no snapshot/history/replay guarantee. A filtered live probe returned a short subscription burst, but the implementation does not depend on this behavior. It records received observations in SQLite, reports coverage and gaps, and sends one-minute OHLC bars of those TWAP observations to Codex. Bars are not exchange-trade candles. An exact opening-time tick must exist; a mid-market cold start waits for the next captured opening. Missing ticks are never interpolated.

5. **CLOB historical prices describe outcome shares, not BTC/USD.** `/prices-history` with the outcome token ID, `interval=1h`, `fidelity=1` returned minute-level points. The app sends separate histories for UP and DOWN, including their timestamps; pre-window prices are retained as such. Source: [history endpoint](https://docs.polymarket.com/api-reference/markets/get-prices-history).

6. **Map outcomes by their labels and token IDs.** Never assume token zero means UP. Market discovery uses the observed recurring slug and verifies its timestamp against `eventStartTime` and `endDate`. Gamma `startDate` is creation/listing time; CLOB `end_date_iso` was date-only in the live sample and is not used as the 15-minute close. Books must match both condition ID and token ID; levels are sorted by price, not presumed ordering.

7. **Fee/minimum-size parameters matter to paper results.** The live CLOB info returned fee rate 0.07, exponent 1, and minimum size 5 for these markets. The adapter reads current parameters and rejects unsupported exponents. It estimates taker fees as shares × rate × price × (1 − price), rounded to five decimals, charged as paper cash at entry in the current hold-to-settlement mode. Quotes, displayed depth, minimum size and price tick are checked. No maker fills or rebates are assumed. Sources: [fees](https://docs.polymarket.com/trading/fees), [CLOB market parameters](https://docs.polymarket.com/api-reference/markets/get-clob-market-info).

8. **Closing time is not proof of a winning payout.** Positions enter a settlement queue at expiry. The adapter requires a closed CLOB market with an explicit winning token, or its explicit 50/50 flag. Near-one prices and closed=true alone do not produce payouts. It never calculates its own winner from Chainlink. Source: [resolution](https://docs.polymarket.com/concepts/resolution).

## Practical limits

Completed verification: the paused app received live BTC books and 61 history points per outcome, captured the 01:45 UTC opening TWAP, retained it after restart, and passed the real snapshot to Codex CLI. The preview returned WAIT with execution disabled. An actual closed historical market returned UP=1 / DOWN=0 through the settlement reader. The paper account stayed at $1,000 with zero trades. Isolated tests cover the execution paths; a trading session has intentionally not been run.

Live verification also caught a discovery/settlement distinction: `/markets?slug=...` omitted a closed market unless `closed=true` was included. Settlement therefore uses the documented [exact market-by-slug endpoint](https://docs.polymarket.com/api-reference/markets/get-market-by-slug), which returns closed markets as well.

- This is an AI-only local paper simulator, not a connected Polymarket trading account. There are no wallet, signing, order submission or redemption calls.
- New data is observed while paused, but AI orders cannot execute. Manual CLI reviews are previews only.
- Underlying history is only as complete as the observations collected; REST outcome history does not replace it. A fresh installation has a warm-up period.
- Top-of-book fills with estimated cash fees still omit additional slippage, market impact, execution latency and exact live wallet fee accounting. Paper profit is not evidence of live profitability.
- Data/schema/rule changes surface as errors and block affected entries. The existing Kalshi account and backups remain separate from `data/polymarket/`.
