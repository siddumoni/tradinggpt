# Volume Profile research module

## What it does

TradingGPT can now build a session Volume Profile from 5-minute OHLCV candles. It reports:

- **POC (Point of Control):** price level with the largest allocated volume.
- **VAH / VAL:** upper and lower edges of the 70% value area by default.
- **HVN / LVN:** local high/low-volume nodes using a transparent neighbour-ratio heuristic.
- The per-price-level volume distribution and the percentage of session volume at each level.

## Important accuracy boundary

A 5-minute candle only tells us its open, high, low, close and total volume. It does not tell us how many shares traded at each price inside that range.

For OHLCV input, TradingGPT therefore splits each candle's volume equally across every price tick touched between its low and high. The output is explicitly labelled UNIFORM_OHLC_RANGE_ESTIMATE, approximate=true and exact_volume_at_price=false. POC, VAH, VAL, HVNs and LVNs from this method are estimates. They must not be presented as exact exchange volume-at-price.

The module also provides build_trade_volume_profile() for trade-print inputs. Its result is marked exact only when the caller explicitly attests that the input contains the complete session trade feed. A partial feed is still labelled approximate.

## Run it on a saved candle file

Run from the repository root in PowerShell. Use the actual tick size for the instrument; do not assume every instrument has the same tick size.

~~~powershell
python -m src.volume_profile --candles .local\paper_candles_kite\SBIN.csv --tick-size 0.05 --symbol SBIN --as-of 2026-11-10T15:20:00+05:30 --output reports\microstructure\SBIN-volume-profile.json
~~~

The as-of value is optional. When supplied, any candle whose full interval has not ended by that time is excluded. Input should contain only one regular trading session; split multi-day CSV files by date first.

## Python API

~~~python
from src.volume_profile import build_volume_profile

profile = build_volume_profile(
    completed_5m_candles,
    tick_size=0.05,
    value_area_pct=0.70,
    symbol="SBIN",
)
print(profile["poc"], profile["value_area"])
~~~

## Research and safety boundaries

This is a descriptive market-context tool, not an entry/exit strategy. It is not connected to market_scanner, nse_champion_v2, the paper broker, position sizing, stops, targets or the candidate acceptance gate. It cannot change a Champion decision.

Do not compare estimated candle profiles to trade-print profiles as if their source data were equivalent. Do not tune profile parameters on the same dates used to claim out-of-sample performance. Preserve tick size, session, source method and date range with every research result.
