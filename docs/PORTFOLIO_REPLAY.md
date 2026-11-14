# Shared-capital portfolio replay

## Purpose

The individual backtests each use their own capital budget. Adding their P&L does not model a single ₹20,000 account, because two symbols may signal at the same time and compete for the same cash.

This replay combines **one selected saved trade log per intended candidate stream** and applies one capital pool chronologically. It closes positions from prior simulated trades as their saved exit times are reached, reserves capital for active position notional plus a stop-cost buffer, scales quantity down to respect both available capital and the per-trade risk budget, blocks overlapping positions in the same symbol, and records any candidate it must reject. It recalculates P&L at the allocated quantity with the selected slippage and the configured transaction-charge estimate.

## Replay archived scanner decisions through the paper account

The repository also provides a replay for the private `tradinggpt-research-data` archive:

```powershell
python -m src.paper_archive_replay --archive-root ../tradinggpt-research-data --output-dir reports/paper_archive_replay --from-date 2026-11-11 --to-date 2026-11-13
```

This replays manifested `daily/YYYY-MM-DD` folders in timestamp order. Each selected daily report must explicitly identify the active `nse_champion_v2` version; the current archive also contains earlier, unversioned Champion days, so the date range is explicit and mixed versions fail closed. It verifies each scan/candle input against the daily SHA-256 manifest, accepts only research-only Kite scan reports that explicitly assert no broker orders, applies archived quote snapshots to open positions, and passes each completed 5-minute candle through the shared `PaperBroker` exit rules. The account starts with one shared cash/risk pool and carries realized equity across days. Output files are named with a deterministic digest of the archived input manifests/data, replay-engine and strategy versions, selected dates, and account/cost assumptions, so sensitivity runs do not overwrite one another.

For every symbol that has a directional candidate, the replay requires all 72 five-minute bars whose starts run from 09:15 through 15:10 IST; the final bar ends at the configured 15:15 square-off. NSE's regular equity session closes at 15:30, so this is the strategy simulation window rather than a complete exchange session. The JSON summary records these boundaries. Missing window bars or hash mismatches stop the run before performance is reported. If a position remains open at day end, the summary is marked `INCOMPLETE_OPEN_POSITIONS` and the position is not carried into the next day. No prices are synthesized. Kite staff has said its historical API data is provided for personal backtesting only ([staff response](https://kite.trade/forum/discussion/15036/app-with-access-to-historical-data-only)). That statement does not resolve local retention, replay through a paper account, or derived trade-output rights. The current [Kite API terms](https://kite.trade/terms/) prohibit API/live data use in virtual/mock trading apps and restrict permanent copies or redistribution absent express permission. A recent [Kite forum question](https://kite.trade/forum/discussion/16353/using-zeroda-data-while-complying-with-data-usage-terms-and-conditions-fully) asks about this same combination of historical storage, live paper trading, and derived results; the public response directs the user to email Kite Connect support and does not answer those permissions. Obtain written confirmation for the intended use before running this replay on Kite-derived archives. Otherwise, use data with a license that covers the replay, retention, and derived outputs. See the [NSE data policy](https://www.nseindia.com/static/market-data/nse-data-policy) for agreement-specific use limits.

**NSE simulation restriction:** clause 7.4 of NSE's current [Data Usage and Data Sharing Policy](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NSE_Data_Sharing%26Usage_Policy.pdf) says market data shall not be provided to platforms or apps engaging in virtual trading or simulation. Because this project includes a paper-trading account, do not assume an NSE historical-data subscription authorizes archive replay in this tool. Confirm the exact private research, retention, and paper-replay design with NSE in writing before using NSE-sourced data.

Archived scanner snapshots preserve the point-in-time candidate ranking and quote checks for those recorded sessions. Candle-close marks between scans use the candle close for both bid and ask; this is an explicit approximation. The replay is therefore a reproducible paper-archive portfolio replay, not a multi-year historical strategy backtest, live-fill calibration, or evidence of profitability. Its sample is limited to the dates actually present in the archive.

## PowerShell

```powershell
python -m src.portfolio_replay --trades reports/backtests/RELIANCE_vwap_trend_trades.csv reports/backtests/INFY_vwap_pullback_trades.csv --initial-capital 20000 --risk-per-trade 200 --slippage-bps 5 --max-concurrent-positions 3
```

A directory containing `*_trades.csv` files can also be passed. The tool writes a JSON summary and a `*_trade_details.csv` with accepted/rejected candidates, allocated quantity, available/reserved capital, and net P&L.

## Interpretation and limitations

- The simulation is a **portfolio allocation replay of saved per-symbol trades**, not a full portfolio backtest: it does not regenerate signals while capital is occupied or test alternative signal priority policies.
- For simultaneous entry timestamps, input file order and then row order decides priority. Keep that order fixed and report it when comparing runs.
- Feed one selected baseline log per intended candidate stream. Do not include overlapping logs from alternative slippage levels or regime-filter variants as if they were independent signals.
- The saved exit path is reused; positions are not marked to market between bars. The reported drawdown is based on realized trade outcomes and omits intratrade mark-to-market drawdown.
- When an OHLC replay entry occurs mid-candle, the first partial candle cannot earn a target from its full high/low range. A stop touch in that candle is booked at the planned stop; the candle's open is not used to infer a gap because it may predate entry. This is deliberately conservative about favorable outcomes, but OHLC still cannot reconstruct the post-entry path or stop slippage; quote/tick data is needed for that.
- Capital reservation is cash-only and reserves entry notional plus an estimated stop-cost buffer. It deliberately does not assume MIS leverage.
- The loss budget is an estimate. Price gaps and actual fills can exceed the planned risk; estimated charges/slippage differ from actual contract notes.
- This remains an offline advisory research tool. It does not place, change, or cancel orders, and its result is not proof of a tradable edge.

Use the [research protocol](RESEARCH_PROTOCOL.md) and freeze the candidate list before unseen-date testing.
