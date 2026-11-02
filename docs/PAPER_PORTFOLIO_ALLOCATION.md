# Shared-Capital Paper Portfolio Allocation

## Purpose

The integrated Champion paper broker now treats its initial virtual capital as one shared cash pool. The service defaults remain ₹1,00,000 capital, ₹500 maximum planned risk per trade, three open positions, and a ₹2,000 daily loss limit. The aggregate planned open-risk cap defaults to ₹1,500 (₹500 × 3 positions); use `--max-portfolio-risk` to override it explicitly.

This feature applies only to the integrated paper account. The standalone advisory scanner keeps its independent ₹20,000 reference sizing. Scanner candidates remain separate signals; the paper broker decides which eligible candidates fit the virtual portfolio.

## Allocation sequence

1. Retain only `CONDITIONAL_BUY` and `CONDITIONAL_SHORT` candidates. Watch-only and no-trade rows do not enter the Champion account.
2. Rank conditional candidates using a deterministic 0–100 research score:
   - 35% scanner confidence.
   - 35% after-cost Target 1 net reward-to-risk ratio, scaled to a 3.0R cap.
   - 15% quoted bid/ask spread quality.
   - 10% stop-distance-to-spread robustness.
   - 5% company/news and market-context risk quality.
3. Resolve equal scores deterministically using confidence, net R:R, spread and symbol.
4. Process candidates in rank order. For each one, recheck live quote freshness and bid/ask integrity, stop/target ordering, daily loss, duplicate signal, existing symbol, maximum positions and available virtual cash.
5. Calculate share quantity under the lesser of the per-trade risk limit and remaining aggregate portfolio-risk budget. The risk engine also reduces quantity for estimated round-trip charges and slippage.
6. Open only if after-cost net R:R and all other fail-closed checks pass. A position's planned net stop-out risk is reserved until it closes.

The allocator is quality-first and greedy: it is a transparent v1 heuristic, not a mathematical proof of a globally optimal portfolio or a forecast of future returns. The ranking score is an ordering aid only. Hard risk and cash gates always take precedence.

## Shared limits and accounting

- **Virtual cash:** open position notional is reserved against starting capital plus realised P&L. Unrealised gains are not recycled as spendable cash.
- **Per-trade risk:** default maximum ₹500 after estimated stop-out charges and slippage.
- **Aggregate open risk:** default maximum ₹1,500 across Champion open positions. A later entry is sized using only the remaining risk budget; it is rejected if no positive quantity fits.
- **Open positions:** default maximum three.
- **Daily loss:** default ₹2,000; existing guard remains active.
- **Leverage:** disabled in the Champion paper broker; each position's notional must fit available virtual cash.
- **Execution:** only paper fills are simulated. The engine does not submit, modify or cancel real Kite orders.

The database adds `planned_net_risk_inr` to the trade ledger. On first startup after an upgrade, legacy open positions reserve their stored per-trade risk budget conservatively so they are not treated as zero-risk exposure.

## Dashboard and audit trail

The local dashboard displays open planned portfolio risk, remaining portfolio-risk budget, the configured aggregate cap, open exposure and the per-trade limit. Each scan records `ALLOCATION_RANKED` events with rank, score and component breakdown. Each paper entry records allocated risk budget, planned net stop-out risk, aggregate risk after entry, rank and score. Rejections remain visible as signal-rejection events.

## Configuration

Defaults for the existing ₹1 lakh paper service are unchanged. An optional override can be supplied:

```powershell
python -m src.paper_trading_service --max-portfolio-risk 1500
```

To change per-trade risk, maximum positions or starting paper capital, use the existing `--risk-per-trade`, `--max-open-positions` and `--account-capital` options. If `--max-portfolio-risk` is omitted, it defaults to `risk_per_trade × max_open_positions`.

## Validation

Offline tests cover ranking independently of report order, combined planned-risk enforcement, default aggregate-cap derivation, and rejection of missing or crossed executable bid/ask quotes. GitHub Actions runs the full regression suite. A passing test suite validates software behavior, not strategy profitability or production execution quality.
