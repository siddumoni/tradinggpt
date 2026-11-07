# TradingGPT delivery phases

This document is the current phase index. Historical phase notes are retained under `docs/archive/` and must not override the current README or implementation.

| Phase | Scope | Status |
|---|---|---|
| 1 | Authentication/session foundation and core risk/cost settings | Complete |
| 2 | Backtesting, execution assumptions, risk/cost validation and reproducible research settings | Complete |
| 3 | Statistical validation, paper-report adapters and multi-day report analysis | Complete; historical evidence remains inconclusive |
| 4 | Telegram paper alerts, NSE calendar, circuit-band checks, relative-volume and MIS margin safeguards | Complete; merged to main as `87ac62e`; main CI: 400 passed |
| 5 | Repository hygiene, named account profiles, scanner consistency, secure optional token storage and cross-platform quality gates | Complete; merged to main as `07ab215`; Ubuntu and Windows Python 3.13 CI passed |
| 6 | Evidence-led README/results refresh after adequate prospective paper sessions | Groundwork complete; awaiting evidence gate |

## November 2026 research-foundation sequence

| Step | Scope | Status |
|---|---|---|
| 0 | Advisory-only broker API tripwire | Complete; PR #39 merged |
| A | Shared structural-stop selection and replay parity | Complete; PR #40 merged |
| B | Prior-session ATR seed with strict session completeness | Complete; PR #41 merged |
| C | Validated pre-entry Telegram trade plans and EOD digest | Complete; PR #42 merged |
| D | Signal-journal cost feasibility, profile-aware forward sizing, paired challenger comparison and results gate | Complete; PR #43 merged as `6b6d607`; Ubuntu and Windows Python 3.13 CI passed |
| E | Prior-day level proximity filter, MIS eligibility/surveillance checks, 45-day relative-volume cache and calendar rollover warning | Implementation and tests in progress on `foundation/phase-e-market-eligibility-cache-filters` |

All foundation steps preserve the advisory-only boundary. On merge of Phase E, new paper entries use the frozen `nse_champion_v2` version on the first supported normal NSE cash session. The earlier `nse_champion_v2_foundation_candidate` and `champion_v1` remain frozen archives; their trade rows and daily reports are explicitly labelled `CHAMPION_ARCHIVE` and must not be pooled with v2. See [Market Safety](MARKET_SAFETY.md) for the Phase E operational contract.

## Phase 6 status and evidence gate

Phase 6 is intentionally evidence-gated. The statistical pipeline and promotion thresholds are implemented, but the public performance summary must not be refreshed from an under-sized sample.

As of 2026-11-06, the available archived daily paper reports cover 2026-11-03 through 2026-11-05 only (at most three independent sessions per track). This is below the configured minimum of 20 sessions and 100 accepted, completed trades. The current status is therefore **insufficient prospective evidence**, not a positive or negative strategy result.

See [the Phase 6 readiness guide](PHASE6_READINESS.md) for data handling, per-track analysis commands, the acceptance gate and the final documentation checklist.

## Safety and research status

- The repository remains advisory-only. No real broker order placement, modification or cancellation is in scope.
- Fail-closed data, quote, circuit, margin, risk and cost gates remain mandatory.
- The minimum after-cost net reward/risk gate for approved scanner, paper, backtest and sensitivity entries is a hard 1.5; lower CLI/config overrides are rejected. The independent candidate-outcome tracker retains an internal 0.01 sizing sentinel only to simulate signals already rejected by the 1.5 scanner gate; it is research-only, cannot approve an entry, and is never included in Champion performance. The per-side slippage floor is hard 5 bps, and performance promotion is evaluated at 8 bps.
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
