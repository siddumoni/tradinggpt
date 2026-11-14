import csv
import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.paper_archive_replay import run_archive_replay

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 13)


def _manifest(archive_root: Path, day_dir: Path, inputs: list[Path]) -> None:
    files = []
    for path in inputs:
        raw = path.read_bytes()
        files.append({
            "path": path.relative_to(archive_root).as_posix(),
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "required": True,
        })
    (day_dir / "manifest.json").write_text(
        json.dumps({
            "report_date": DAY.isoformat(),
            "research_only": True,
            "orders_sent_to_broker": False,
            "files": files,
        }),
        encoding="utf-8",
    )


def _archive(tmp_path: Path, *, missing_bar: str | None = None) -> Path:
    archive_root = tmp_path / "archive"
    day_dir = archive_root / "daily" / DAY.isoformat()
    scan_dir = day_dir / "scans"
    candle_dir = day_dir / "candles"
    scan_dir.mkdir(parents=True)
    candle_dir.mkdir(parents=True)

    scan_time = datetime(2026, 11, 13, 10, 2, 5, tzinfo=IST)
    scan = {
        "status": "RESEARCH_SCAN_ONLY",
        "as_of": scan_time.isoformat(),
        "data_provider_mode": "kite",
        "orders_placed_or_modified": False,
        "symbols_requested": ["INFY"],
        "candidates": [{
            "symbol": "INFY",
            "action": "CONDITIONAL_BUY",
            "side": "LONG",
            "strategy": "vwap_trend",
            "confidence": 80,
            "market_regime": "BULLISH",
            "signal_time": "2026-11-13T09:55:00+05:30",
            "reference_entry": 100.0,
            "stop_loss": 98.0,
            "target_1": 104.0,
            "target_2": 108.0,
            "quantity": 99,
            "data_verified_live": True,
            "margin_verified": True,
            "company_specific_risk": "LOW",
        }],
        "market_snapshots": {
            "INFY": {
                "last_price": 100.0,
                "bid": 99.99,
                "ask": 100.01,
                "timestamp": "2026-11-13T10:02:03+05:30",
                "verified_live": True,
            },
        },
    }
    scan_path = scan_dir / "phase6_scan_20261113_100205.json"
    scan_path.write_text(json.dumps(scan), encoding="utf-8")

    candle_path = candle_dir / "INFY.csv"
    with candle_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "high", "low", "close", "volume", "provider_mode"])
        stamp = datetime.combine(DAY, time(9, 15), tzinfo=IST)
        while stamp.time() <= time(15, 10):
            label = stamp.strftime("%H:%M")
            if label != missing_bar:
                values = (100.0, 100.5, 99.5, 100.0)
                if label == "10:00":
                    # Target touched in the part of the candle before/around entry;
                    # close stays below target so the partial OHLC must not exit.
                    values = (100.0, 109.0, 99.0, 107.0)
                elif label == "10:05":
                    # This is the first full candle after the 10:02:05 entry.
                    values = (107.0, 109.0, 106.0, 108.5)
                writer.writerow([stamp.isoformat(), *values, 1000, "kite"])
            stamp += timedelta(minutes=5)

    report_dir = day_dir / "reports"
    report_dir.mkdir()
    daily_report_path = report_dir / f"paper_daily_{DAY.isoformat()}.md"
    daily_report_path.write_text(
        "## Champion account — nse_champion_v2\n",
        encoding="utf-8",
    )
    _manifest(archive_root, day_dir, [scan_path, candle_path, daily_report_path])
    return archive_root


def test_archive_replay_uses_shared_account_and_only_post_entry_target(tmp_path):
    archive_root = _archive(tmp_path)
    summary = run_archive_replay(archive_root, tmp_path / "output", from_date=DAY, to_date=DAY)

    assert summary["status"] == "COMPLETE"
    assert summary["days_processed"] == 1
    assert summary["closed_trades"] == 1
    assert summary["net_pnl"] > 0
    assert summary["scan_reports_processed"] == 1
    assert Path(summary["summary_path"]).is_file()
    assert Path(summary["trades_path"]).is_file()

    with Path(summary["trades_path"]).open(encoding="utf-8", newline="") as handle:
        trade = next(csv.DictReader(handle))
    assert trade["exit_reason"] == "TARGET_2"
    assert trade["exit_time"].startswith("2026-11-13T10:10:00")


def test_archive_replay_rejects_mixed_champion_versions(tmp_path):
    archive_root = _archive(tmp_path)
    day_dir = archive_root / "daily" / DAY.isoformat()
    daily_report_path = day_dir / "reports" / f"paper_daily_{DAY.isoformat()}.md"
    daily_report_path.write_text(
        "## Champion account — champion_v1\n",
        encoding="utf-8",
    )
    inputs = [
        *sorted((day_dir / "scans").glob("phase6_scan_*.json")),
        *sorted((day_dir / "candles").glob("*.csv")),
        daily_report_path,
    ]
    _manifest(archive_root, day_dir, inputs)

    with pytest.raises(ValueError, match="uses champion_v1"):
        run_archive_replay(
            archive_root,
            tmp_path / "output",
            from_date=DAY,
            to_date=DAY,
        )


def test_replay_output_identity_includes_assumptions(tmp_path):
    archive_root = _archive(tmp_path)
    output_dir = tmp_path / "output"

    baseline = run_archive_replay(
        archive_root,
        output_dir,
        from_date=DAY,
        to_date=DAY,
    )
    higher_slippage = run_archive_replay(
        archive_root,
        output_dir,
        slippage_bps=10.0,
        from_date=DAY,
        to_date=DAY,
    )

    assert baseline["input_sha256"] == higher_slippage["input_sha256"]
    assert baseline["run_id"] != higher_slippage["run_id"]
    assert baseline["summary_path"] != higher_slippage["summary_path"]
    assert baseline["trades_path"] != higher_slippage["trades_path"]


def test_archive_replay_fails_closed_on_missing_candle(tmp_path):
    archive_root = _archive(tmp_path, missing_bar="10:05")

    with pytest.raises(ValueError, match="Incomplete 5-minute session"):
        run_archive_replay(archive_root, tmp_path / "output")
