# TradingGPT delivery phases

This document is the current phase index. Historical phase notes are retained under `docs/archive/` and must not override the current README or implementation.

| Phase | Scope | Status |
|---|---|---|
| 1 | Authentication/session foundation and core risk/cost settings | Complete |
| 2 | Backtesting, execution assumptions, risk/cost validation and reproducible research settings | Complete |
| 3 | Statistical validation, paper-report adapters and multi-day report analysis | Complete; historical evidence remains inconclusive |
| 4 | Telegram paper alerts, NSE calendar, circuit-band checks, relative-volume and MIS margin safeguards | Complete; merged to main as `87ac62e`; main CI: 400 passed |
| 5 | Repository hygiene, named account profiles, scanner consistency, secure optional token storage and cross-platform quality gates | In progress on `feature/phase5-repository-hygiene` |
| 6 | Evidence-led README/results refresh after adequate prospective paper sessions | Not started |

## Safety and research status

- The repository remains advisory-only. No real broker order placement, modification or cancellation is in scope.
- Fail-closed data, quote, circuit, margin, risk and cost gates remain mandatory.
- The minimum after-cost net reward/risk gate remains 1.5 unless a research-only CLI override is explicitly supplied; the paper profile defaults to 1.5.
- Historical or paper results do not establish a repeatable edge. Do not promote a strategy solely because of a small-sample backtest.
- The 2026 NSE holiday calendar currently fails closed outside its verified coverage ending 2026-12-31.

## Phase 5 acceptance criteria

1. Runtime databases, candle archives, logs, local secrets and generated coverage artifacts are ignored by Git.
2. Research, paper and live-advisory profiles resolve from one configuration source; explicit CLI values override profile defaults.
3. Existing paper databases are never reset or silently re-capitalized when a different profile is selected.
4. Every scanner early exit constructs a consistent zero-quantity `NO_TRADE` candidate.
5. Paper broker and service modules pass Ruff formatting checks without changing trading logic.
6. CI runs compile, Ruff lint/format, lenient MyPy and pytest coverage on Ubuntu and Windows with Python 3.13.
7. Optional keyring storage is opt-in and fails closed if its dependency/backend is unavailable.
8. No real-order API path is introduced; existing risk, slippage, charges, quote and 1.5 net R:R rules remain intact.
