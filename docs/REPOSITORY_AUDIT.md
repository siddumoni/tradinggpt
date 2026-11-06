# TradingGPT repository audit

Audit date: 2026-11-06

## Current status

TradingGPT is an NSE equity research and paper-trading system with a strict advisory-only boundary. The current `main` branch includes Phases 1–4. Phase 4 was merged as commit `87ac62e`; the post-merge Windows/Python 3.13 workflow completed with **400 passed, 0 failed**.

Phase 5 is the current repository-hygiene milestone. Its work is isolated on `feature/phase5-repository-hygiene` until the full test and quality matrix passes.

## Implemented safeguards on main

- Authenticated Kite market-data and read-only MIS margin estimation; no order submission.
- Executable-side quote validation, slippage and estimated NSE charges.
- Minimum after-cost net R:R gate of 1.5.
- NSE 2026 equity holiday calendar, verified only through 2026-12-31; unknown/out-of-range dates fail closed.
- Circuit-limit data validation and near-band rejection, including final executable bid/ask.
- Time-of-day relative-volume comparison using completed 5-minute bars and prior complete sessions; missing/insufficient history fails closed.
- Persistent SQLite Champion paper account, paper-only manual exits, automatic risk exits, and Telegram paper events.
- Research-only candidate and shadow outcome ledgers.

## Evidence limitations

A passing test suite establishes software behaviour, not strategy profitability. Historical candidate results and small independent paper samples are not enough to establish a robust positive edge. Continue the prospective paper test with frozen candidate rules and preserve all daily reports. Do not change strategy thresholds based on a handful of observations.

The scanner's independent candidate outcome ledger is not a shared-capital portfolio. Overlapping signal outcomes must not be summed and described as portfolio P&L. Use the shared-capital replay for allocation diagnostics and label its limitations.

## Current open risks / operational limitations

1. The 2026 NSE holiday calendar ends on 2026-12-31 and must be extended from the official exchange calendar before 2027 scans.
2. Relative-volume coverage depends on the available prior-session history; insufficient sessions intentionally block live candidates.
3. Margin estimates, spread and slippage are estimates and cannot guarantee broker acceptance or execution.
4. RSS keyword triage is not a substitute for exchange filings or company announcements.
5. Paper fills are simulations; real execution, gaps and charges may differ.
6. Strategy performance remains inconclusive until enough unseen prospective sessions and trades have been observed.

## Phase 5 and later

See [the delivery phase index](PHASES.md) for Phase 5 acceptance criteria and the Phase 6 evidence-led documentation milestone. Historical early-scanner instructions have been moved to [the archive](archive/PHASE6_README_legacy.md) and are not current operating instructions.
