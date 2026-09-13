# What Terra receives

The prompt is a structured decision brief, not a provider response dump. Prices used for execution retain their original precision; historical table prices are rounded to USD cents. JSON field names and table columns identify units. Summaries do not guarantee a correct trade.

Each decision must include Terra's estimated Up probability. Down is its complement. Terra compares both estimates with the supplied fee-adjusted breakeven probabilities and enters the positive-edge side when one exists. A modest estimated edge can use a small paper position; WAIT is reserved for two non-positive edges, genuinely balanced or contradictory evidence, or missing required live data.

| Collected information | Sent to AI | Reason |
| --- | --- | --- |
| Market rules, window, resolution source | Full rules and exact timestamps | Defines the actual bet |
| Current market eligibility | Active/closed/accepting orders/restricted status | Avoid assumptions about tradability |
| Underlying current/opening price | Full precision, signed difference and source time | Critical settlement reference |
| One hour of recorded TWAP | All available one-minute candles, counts and gaps | Preserve recent price path |
| Up to 24 hours of recorded TWAP | All available 15-minute candles with observed boundaries/counts/gaps | Longer context without per-second repetition |
| 1/5/15/30/60-minute signals | Changes, ranges, averages, volatility, RSI and limitations | Descriptive context; never automatic rules |
| Both full order books | Top 10 nonzero levels per bid/ask, whole-book totals, total/omitted level counts, depth within 1/3/5 cents | Preserve local liquidity shape and reveal what was omitted |
| Best quotes and quantities | Exact prices and sizes, timestamps | Paper execution is limited to the displayed best ask |
| Fees and minimums | Fee details, current rate, contract minimum and tick size | Trade cost and executable limits |
| Both contract-price histories | All returned points in compact timestamp/price tables, refreshed after opening before the review | Preserve probability movements separately from BTC/USD |
| Provider volume/liquidity | Selected original aggregate metrics, labeled as possibly lagging | Participation context, not underlying traded volume |
| Public market trades | Aggregated 30-second, 1/3/15-minute taker-side counts, contracts, buys, sells and VWAP by outcome | Adds pre-opening and recent participation without trader identities or a raw trade dump |
| Account | Cash, equity, results, open/pending positions, spending limits and timed-run/profit controls | Existing exposure and constraints |
| Recent decisions/events | Latest ten events, including actions; long reason text capped at 500 characters | Recent execution feedback without recursive prompt growth |
| Provider IDs, images, nested event copies, operational flags | Omitted | Duplicated or unrelated to entry decisions; execution still validates identifiers internally |

Not collected: independently verified aggressor intent, exchange spot volume, or an external calibrated probability baseline from matched resolved markets. These limitations are explicit in the brief but do not automatically veto a trade. Terra must estimate direction from the combined supplied evidence and may still WAIT when that evidence does not clear the fee-adjusted price.

Full books remain in engine memory for validation. Database price observations have 24-hour retention. Saved review files contain the exact decision brief sent to AI. A summary is not a backup of every provider field.

## Recent market activity

The briefing includes 30/60/180-second summaries of observed book mid-price and top-five depth changes, and public WebSocket trade-event counts, BUY/SELL contract totals and volume-weighted prices. These are provider-reported sides, not verified aggressor attribution. Book samples are at most once per second; at most 301 samples and 5,000 trade events are held in memory per market. Coverage, sample gaps and trade-buffer saturation are explicit. Reconnects and restarts clear this context; missing activity is not proof of no trading.

Opening distance is also expressed in units of the RMS of up to 30 contiguous one-minute TWAP changes (at least 15 required). This is descriptive context, not a calibrated probability, expected future move or entry rule. Existing 24-hour recording continues unchanged; older data cannot be created instantly.

The CLOB contract-history REST series has one-minute resolution and commonly has no post-open point at the early review. The brief labels this as expected publication lag and directs Terra to use the fresh WebSocket quotes, depth and flow for current contract state. Partial long-term history and isolated gaps reduce confidence without automatically forcing WAIT when current books, opening TWAP and recent history are present.

## Conditional execution

An entry response includes its maximum ask, lifetime from the snapshot, maximum adverse BTC/USD movement and maximum adverse selected-contract ask increase. The backend reads the current WebSocket-backed snapshot after Codex returns and rejects a reversal against the chosen outcome or a more expensive contract outside those bounds. Movement further in the predicted direction and a cheaper ask remain valid. These bounds come from the AI for that market; account limits, data freshness and the 30-second hard ceiling remain non-negotiable safety checks. WAIT uses zero values and consumes the market's single review.

Every scheduled decision stores a small pending evaluation containing its ticker, action, snapshot asks and review time. After official settlement the account emits a `review_outcome` event with both payouts, then removes the pending evaluation. This labels WAIT decisions without adding a database table or duplicating the full saved review.
