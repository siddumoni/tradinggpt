import pandas as pd
import pytest

from scripts.audit_atr_warmup import (
    _completed_15m_regime,
    complete_session_dates,
    legacy_session_reset_atr,
)
from src.execution_model import session_aware_atr


def _session(day: str, count: int = 72) -> pd.DataFrame:
    index = pd.date_range(
        f"{day} 09:15", periods=count, freq="5min", tz="Asia/Kolkata"
    )
    close = [100.0 + i * 0.2 for i in range(count)]
    return pd.DataFrame(
        {
            "open": [value - 0.05 for value in close],
            "high": [value + 0.3 for value in close],
            "low": [value - 0.3 for value in close],
            "close": close,
            "volume": [1000.0] * count,
        },
        index=index,
    )


def test_audit_compares_new_seed_with_previous_fourteen_bar_reset():
    previous = _session("2026-10-15", 14)
    current = _session("2026-10-16", 9)
    combined = pd.concat([previous, current])
    new = session_aware_atr(combined).loc[current.index]
    old = legacy_session_reset_atr(combined).loc[current.index]
    assert new.notna().all()
    assert old.isna().all()


def test_audit_requires_full_session_grid():
    complete = _session("2026-10-15")
    partial = _session("2026-10-16", 71)
    assert complete_session_dates(pd.concat([complete, partial])) == {
        complete.index[0].date()
    }


def test_audit_15m_regime_uses_only_completed_bars():
    current = _session("2026-10-16", 9)
    as_of = current.index[-1] + pd.Timedelta(minutes=5)
    assert _completed_15m_regime(current, as_of) == "BULLISH"
    before_third_bar_close = current.index[-1]
    assert _completed_15m_regime(current, before_third_bar_close) == "UNKNOWN"
