# What Astra receives

The prompt is a structured decision brief, not a provider response dump. Prices used for execution retain their original precision; historical table prices are rounded to USD cents. JSON field names and table columns identify units. Summaries do not guarantee a correct trade.

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
| Both contract-price histories | All returned points in compact timestamp/price tables | Preserve probability movements separately from BTC/USD |
| Provider volume/liquidity | Selected original aggregate metrics, labeled as possibly lagging | Participation context, not underlying traded volume |
| Account | Cash, equity, results, open/pending positions, spending limits and timed-run/profit controls | Existing exposure and constraints |
| Recent decisions/events | Latest ten events, including actions; long reason text capped at 500 characters | Recent execution feedback without recursive prompt growth |
| Provider IDs, images, nested event copies, operational flags | Omitted | Duplicated or unrelated to entry decisions; execution still validates identifiers internally |

Not collected: historical book changes, aggressor trade flow, independently verified exchange volume, or a calibrated probability baseline from matched resolved markets. These limitations are explicit in the brief. Static book size must not be treated as guaranteed fills or inferred order flow.

Full books remain in engine memory for validation. Database price observations have 24-hour retention. Saved review files contain the exact decision brief sent to AI. A summary is not a backup of every provider field.

## Recent market activity

The briefing includes 30/60/180-second summaries of observed book mid-price and top-five depth changes, and public WebSocket trade-event counts, BUY/SELL contract totals and volume-weighted prices. These are provider-reported sides, not verified aggressor attribution. Book samples are at most once per second; at most 301 samples and 5,000 trade events are held in memory per market. Coverage, sample gaps and trade-buffer saturation are explicit. Reconnects and restarts clear this context; missing activity is not proof of no trading.

Opening distance is also expressed in units of the RMS of up to 30 contiguous one-minute TWAP changes (at least 15 required). This is descriptive context, not a calibrated probability, expected future move or entry rule. Existing 24-hour recording continues unchanged; older data cannot be created instantly.
