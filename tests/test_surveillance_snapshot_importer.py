import csv
import json
from datetime import date
from pathlib import Path
import zipfile

import pytest

from scripts.refresh_nse_surveillance import (
    SurveillanceImportError,
    build_snapshot,
    write_snapshot,
)
from src.market_safety import load_surveillance_snapshot, surveillance_status_for_symbol


def _write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["SYMBOL"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def _source_files(tmp_path: Path) -> dict[str, Path]:
    asm = _write_csv(tmp_path / "asm.csv", [
        {"SYMBOL": "AAA", "COMPANY NAME": "Alpha", "ASM STAGE": "Long Term - Stage I"},
    ])
    gsm = _write_csv(tmp_path / "gsm.csv", [
        {"SYMBOL": "BBB", "COMPANY NAME": "Beta", "GSM STAGE": "Stage III"},
    ])
    esm = _write_csv(tmp_path / "esm.csv", [
        {"SYMBOL": "CCC", "ESM STAGE": "Stage II"},
        {"SYMBOL": "AAA", "ESM STAGE": "Stage I"},
    ])
    pca = _write_csv(tmp_path / "pca.csv", [
        {"Symbol": "DDD", "Price": "12.50"},
    ])
    # Current NSE UDiFF field names; the importer also accepts legacy SYMBOL/SERIES.
    t2t_csv = tmp_path / "bhavcopy.csv"
    _write_csv(t2t_csv, [
        {"TckrSymb": "EEE", "SctySrs": "BE"},
        {"TckrSymb": "FFF", "SctySrs": "EQ"},
        {"TckrSymb": "GGG", "SctySrs": "ST"},
        {"TckrSymb": "HHH", "SctySrs": "BZ"},
    ])
    t2t_zip = tmp_path / "bhavcopy.zip"
    with zipfile.ZipFile(t2t_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(t2t_csv, arcname="BhavCopy_NSE_CM_test.csv")
    return {"asm": asm, "gsm": gsm, "esm": esm, "t2t": t2t_zip, "pca": pca}


def _build(tmp_path: Path, *, confirm_complete: bool = True):
    files = _source_files(tmp_path)
    return build_snapshot(
        as_of=date.today(),
        asm_csv=files["asm"],
        gsm_csv=files["gsm"],
        esm_csv=files["esm"],
        t2t_bhavcopy=files["t2t"],
        pca_csv=files["pca"],
        confirm_complete=confirm_complete,
    )


def test_build_snapshot_merges_official_measures_and_preserves_stages(tmp_path):
    snapshot = _build(tmp_path)

    assert snapshot["schema_version"] == 1
    assert snapshot["complete"] is True
    assert snapshot["as_of"] == date.today().isoformat()
    assert snapshot["summary"]["unique_symbols"] == 7
    assert snapshot["symbols"]["AAA"] == [
        {"measure": "ASM", "stage": "Long Term - Stage I"},
        {"measure": "ESM", "stage": "Stage I"},
    ]
    assert snapshot["symbols"]["EEE"] == [
        {"measure": "TRADE_TO_TRADE", "stage": "NSE equity series BE/BZ/ST/SZ"},
    ]
    assert "FFF" not in snapshot["symbols"]
    assert snapshot["symbols"]["GGG"][0]["measure"] == "TRADE_TO_TRADE"
    assert snapshot["symbols"]["HHH"][0]["measure"] == "TRADE_TO_TRADE"
    assert snapshot["symbols"]["DDD"][0]["measure"] == "PERIODIC_CALL_AUCTION"


def test_write_snapshot_validates_generated_file_and_symbol_statuses(tmp_path):
    snapshot = _build(tmp_path)
    output = tmp_path / ".local" / "nse_surveillance.json"

    write_snapshot(snapshot, output)
    loaded = load_surveillance_snapshot(output, as_of=date.today())

    assert loaded["status"] == "VALID"
    assert loaded["complete"] is True
    assert surveillance_status_for_symbol("AAA", loaded)["status"] == "RESTRICTED"
    assert surveillance_status_for_symbol("EEE", loaded)["status"] == "RESTRICTED"
    assert (
        surveillance_status_for_symbol("ZZZ", loaded)["status"]
        == "NO_RESTRICTION_LISTED_IN_COMPLETE_SNAPSHOT"
    )
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["summary"]["input_sources"]["t2t"]["sha256"]


def test_snapshot_refuses_partial_inputs_without_explicit_confirmation(tmp_path):
    files = _source_files(tmp_path)
    with pytest.raises(SurveillanceImportError, match="--confirm-complete"):
        build_snapshot(
            as_of=date.today(),
            asm_csv=files["asm"],
            gsm_csv=files["gsm"],
            esm_csv=files["esm"],
            t2t_bhavcopy=files["t2t"],
            pca_csv=files["pca"],
            confirm_complete=False,
        )


def test_missing_source_file_fails_without_creating_snapshot(tmp_path):
    files = _source_files(tmp_path)
    missing = tmp_path / "not-downloaded.csv"
    with pytest.raises(SurveillanceImportError, match="does not exist"):
        build_snapshot(
            as_of=date.today(),
            asm_csv=files["asm"],
            gsm_csv=files["gsm"],
            esm_csv=files["esm"],
            t2t_bhavcopy=files["t2t"],
            pca_csv=missing,
            confirm_complete=True,
        )
    output = tmp_path / "nse_surveillance.json"
    assert not output.exists()


def test_bad_source_header_is_rejected(tmp_path):
    files = _source_files(tmp_path)
    _write_csv(files["asm"], [{"NAME": "AAA", "ASM STAGE": "Stage I"}])
    with pytest.raises(SurveillanceImportError, match="symbol column"):
        build_snapshot(
            as_of=date.today(),
            asm_csv=files["asm"],
            gsm_csv=files["gsm"],
            esm_csv=files["esm"],
            t2t_bhavcopy=files["t2t"],
            pca_csv=files["pca"],
            confirm_complete=True,
        )


def test_legacy_t2t_bhavcopy_series_columns_are_supported(tmp_path):
    files = _source_files(tmp_path)
    legacy = _write_csv(tmp_path / "legacy.csv", [
        {"SYMBOL": "LEGACY", "SERIES": "BE"},
        {"SYMBOL": "ORDINARY", "SERIES": "EQ"},
    ])
    files["t2t"] = legacy
    snapshot = build_snapshot(
        as_of=date.today(),
        asm_csv=files["asm"],
        gsm_csv=files["gsm"],
        esm_csv=files["esm"],
        t2t_bhavcopy=files["t2t"],
        pca_csv=files["pca"],
        confirm_complete=True,
    )
    assert snapshot["symbols"]["LEGACY"][0]["measure"] == "TRADE_TO_TRADE"
    assert "ORDINARY" not in snapshot["symbols"]
