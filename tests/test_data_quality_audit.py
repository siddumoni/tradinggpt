import pandas as pd
import pytest

from src.data_quality_audit import audit_symbol, load_candles


def _frame():
    idx = pd.date_range(
        "2026-10-12 09:15", "2026-10-12 15:25",
        freq="5min", tz="Asia/Kolkata",
    )
    close = [100.0] * len(idx)
    return pd.DataFrame({
        "Open": close,
        "High": [101.0] * len(idx),
        "Low": [99.0] * len(idx),
        "Close": close,
        "Volume": [1000] * len(idx),
    }, index=idx)


def test_audit_counts_non_finite_ohlc_as_error(tmp_path):
    frame = _frame()
    frame.iloc[3, frame.columns.get_loc("High")] = float("inf")
    path = tmp_path / "candles.csv"
    frame.rename_axis("Datetime").reset_index().to_csv(path, index=False)

    summary, _ = audit_symbol("TEST", path, "15:10", "15:25")

    assert summary["rows_with_nonfinite_ohlc"] == 1
    assert summary["assessment"] == "ERRORS_FOUND"


def test_audit_rejects_missing_volume_column(tmp_path):
    frame = _frame().drop(columns=["Volume"])
    path = tmp_path / "candles.csv"
    frame.rename_axis("Datetime").reset_index().to_csv(path, index=False)

    with pytest.raises(ValueError, match="Missing required OHLCV"):
        load_candles(path)


def test_audit_counts_invalid_volume(tmp_path):
    frame = _frame()
    frame.iloc[3, frame.columns.get_loc("Volume")] = -1
    path = tmp_path / "candles.csv"
    frame.rename_axis("Datetime").reset_index().to_csv(path, index=False)

    summary, _ = audit_symbol("TEST", path, "15:10", "15:25")

    assert summary["rows_with_invalid_volume"] == 1
    assert summary["assessment"] == "ERRORS_FOUND"
