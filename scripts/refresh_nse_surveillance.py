"""Build a validated local NSE surveillance snapshot from official source files.

The importer deliberately does not scrape or infer classifications. A snapshot is
marked complete only when the operator supplies all five same-date NSE source
files and explicitly confirms they represent the complete latest lists.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import zipfile
from zoneinfo import ZoneInfo

from src.market_safety import load_surveillance_snapshot

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_OUTPUT = Path(".local/nse_surveillance.json")
NSE_SOURCE_URL = "https://www.nseindia.com/all-reports"

SOURCE_URLS = {
    "asm": "https://www.nseindia.com/static/regulations/additional-surveillance-measure",
    "gsm": "https://www.nseindia.com/static/regulations/graded-surveillance-measure",
    "esm": "https://www.nseindia.com/static/regulations/enhanced-surveillance-measure-esm",
    "t2t": "https://www.nseindia.com/all-reports",
    "pca": "https://www.nseindia.com/market-data/stocks-in-call-auction",
}

SYMBOL_FIELDS = ("SYMBOL", "TCKRSYMB", "SC_SYMBOL", "SCRIP", "SCRIPT", "SECURITY")
STAGE_FIELDS = ("STAGE", "ASM_STAGE", "GSM_STAGE", "ESM_STAGE", "SURVEILLANCE_STAGE")
T2T_SERIES = {"BE", "BZ", "ST", "SZ"}


class SurveillanceImportError(ValueError):
    """Raised when an official source file cannot be safely normalized."""


def _normalize_header(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")


def _clean_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or symbol in {"NA", "N/A", "NAN", "NONE", "-"}:
        return ""
    if not re.fullmatch(r"[A-Z0-9&._-]{1,32}", symbol):
        raise SurveillanceImportError(f"Invalid NSE symbol value: {value!r}")
    return symbol


def _open_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.is_file():
        raise SurveillanceImportError(f"Required source file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise SurveillanceImportError(f"Source file has no CSV header: {path}")
            headers = [_normalize_header(name) for name in reader.fieldnames]
            rows = []
            for raw in reader:
                row = {
                    _normalize_header(key): str(value or "").strip()
                    for key, value in raw.items()
                    if key is not None
                }
                if any(value for value in row.values()):
                    rows.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise SurveillanceImportError(f"Could not read CSV source {path}: {type(exc).__name__}") from exc
    return rows, headers


def _symbol_field(headers: list[str], *, source: str) -> str:
    field = next((name for name in SYMBOL_FIELDS if name in headers), None)
    if field is None:
        raise SurveillanceImportError(
            f"{source} source is missing a supported symbol column. "
            f"Found headers: {headers}"
        )
    return field


def _stage_field(headers: list[str]) -> str | None:
    return next((name for name in STAGE_FIELDS if name in headers), None)


def _add_entry(
    symbols: dict[str, list[dict[str, str]]],
    symbol: str,
    measure: str,
    stage: str,
) -> None:
    if not symbol:
        return
    item = {"measure": measure, "stage": stage.strip() or "Listed"}
    entries = symbols.setdefault(symbol, [])
    if item not in entries:
        entries.append(item)


def _parse_measure_csv(path: Path, measure: str) -> tuple[list[dict[str, str]], list[str]]:
    rows, headers = _open_csv(path)
    symbol_col = _symbol_field(headers, source=measure)
    stage_col = _stage_field(headers)
    normalized = []
    for row in rows:
        symbol = _clean_symbol(row.get(symbol_col))
        if not symbol:
            continue
        stage = row.get(stage_col, "") if stage_col else "Listed"
        normalized.append({"symbol": symbol, "stage": stage.strip() or "Listed"})
    return normalized, headers


def _parse_t2t_bhavcopy(path: Path) -> tuple[list[str], int]:
    """Read current NSE UDiFF ZIP or legacy CSV and identify Trade-for-Trade series."""
    if not path.is_file():
        raise SurveillanceImportError(f"Required T2T bhavcopy does not exist: {path}")
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                members = [
                    name for name in archive.namelist()
                    if name.lower().endswith(".csv") and not name.startswith("__MACOSX/")
                ]
                if len(members) != 1:
                    raise SurveillanceImportError(
                        f"T2T ZIP must contain exactly one CSV file; found {len(members)}."
                    )
                data = archive.read(members[0])
        except (OSError, zipfile.BadZipFile) as exc:
            raise SurveillanceImportError(f"Could not read T2T ZIP {path}: {type(exc).__name__}") from exc
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SurveillanceImportError("UDiFF bhavcopy CSV is not UTF-8/UTF-8-BOM.") from exc
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise SurveillanceImportError("UDiFF bhavcopy CSV has no header.")
        headers = [_normalize_header(name) for name in reader.fieldnames]
        rows = [
            {_normalize_header(key): str(value or "").strip() for key, value in row.items() if key}
            for row in reader
        ]
    else:
        rows, headers = _open_csv(path)

    symbol_col = _symbol_field(headers, source="T2T bhavcopy")
    series_col = next((name for name in ("SCTY_SRS", "SERIES", "SCTYSRS") if name in headers), None)
    if series_col is None:
        raise SurveillanceImportError(
            f"T2T bhavcopy is missing UDiFF SctySrs or legacy Series column. Headers: {headers}"
        )
    found = set()
    for row in rows:
        symbol = _clean_symbol(row.get(symbol_col))
        series = str(row.get(series_col) or "").strip().upper()
        if symbol and series in T2T_SERIES:
            found.add(symbol)
    if not rows:
        raise SurveillanceImportError("T2T bhavcopy contains no records.")
    return sorted(found), len(rows)


def _parse_pca_csv(path: Path) -> tuple[list[str], int]:
    rows, headers = _open_csv(path)
    symbol_col = _symbol_field(headers, source="Periodic Call Auction")
    symbols = sorted({
        symbol for row in rows if (symbol := _clean_symbol(row.get(symbol_col)))
    })
    return symbols, len(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_snapshot(
    *,
    as_of: date,
    asm_csv: Path,
    gsm_csv: Path,
    esm_csv: Path,
    t2t_bhavcopy: Path,
    pca_csv: Path,
    confirm_complete: bool,
) -> dict[str, Any]:
    """Normalize the five official NSE inputs; never silently mark partial data complete."""
    if not confirm_complete:
        raise SurveillanceImportError(
            "Refusing to mark the snapshot complete without --confirm-complete. "
            "Verify that all five source files are the latest complete NSE files for --as-of."
        )
    sources = {
        "asm": Path(asm_csv),
        "gsm": Path(gsm_csv),
        "esm": Path(esm_csv),
        "t2t": Path(t2t_bhavcopy),
        "pca": Path(pca_csv),
    }
    symbols: dict[str, list[dict[str, str]]] = {}
    metadata: dict[str, dict[str, Any]] = {}

    for measure in ("asm", "gsm", "esm"):
        records, headers = _parse_measure_csv(sources[measure], measure.upper())
        for record in records:
            _add_entry(symbols, record["symbol"], measure.upper(), record["stage"])
        metadata[measure] = {
            "filename": sources[measure].name,
            "sha256": _sha256(sources[measure]),
            "rows": len(records),
            "headers": headers,
            "source_url": SOURCE_URLS[measure],
        }

    t2t_symbols, t2t_rows = _parse_t2t_bhavcopy(sources["t2t"])
    for symbol in t2t_symbols:
        _add_entry(symbols, symbol, "TRADE_TO_TRADE", "NSE equity series BE/BZ/ST/SZ")
    metadata["t2t"] = {
        "filename": sources["t2t"].name,
        "sha256": _sha256(sources["t2t"]),
        "rows": t2t_rows,
        "restricted_symbols": len(t2t_symbols),
        "source_url": SOURCE_URLS["t2t"],
    }

    pca_symbols, pca_rows = _parse_pca_csv(sources["pca"])
    for symbol in pca_symbols:
        _add_entry(symbols, symbol, "PERIODIC_CALL_AUCTION", "NSE Periodic Call Auction list")
    metadata["pca"] = {
        "filename": sources["pca"].name,
        "sha256": _sha256(sources["pca"]),
        "rows": pca_rows,
        "restricted_symbols": len(pca_symbols),
        "source_url": SOURCE_URLS["pca"],
    }

    for entries in symbols.values():
        entries.sort(key=lambda item: (item["measure"], item["stage"]))

    counts = {
        measure: sum(
            1 for entries in symbols.values()
            if any(entry["measure"] == measure for entry in entries)
        )
        for measure in ("ASM", "GSM", "ESM", "TRADE_TO_TRADE", "PERIODIC_CALL_AUCTION")
    }
    return {
        "schema_version": 1,
        "source": "NSE India official surveillance lists and daily equity bhavcopy",
        "source_url": NSE_SOURCE_URL,
        "as_of": as_of.isoformat(),
        "complete": True,
        "generated_at": datetime.now(IST).isoformat(),
        "symbols": symbols,
        "summary": {
            "unique_symbols": len(symbols),
            "measure_symbol_counts": counts,
            "input_sources": metadata,
        },
    }


def write_snapshot(snapshot: dict[str, Any], output: Path) -> None:
    """Validate and atomically replace the local snapshot only after full validation."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=output.parent,
        prefix=f".{output.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(raw)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        validation = load_surveillance_snapshot(temporary, as_of=date.fromisoformat(snapshot["as_of"]))
        if validation["status"] != "VALID" or validation.get("complete") is not True:
            raise SurveillanceImportError(
                f"Generated snapshot did not validate: {validation.get('status')} — {validation.get('reason')}"
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build .local/nse_surveillance.json from official NSE CSV/ZIP source files."
    )
    parser.add_argument("--as-of", required=True, help="Same trade-date for all source files, YYYY-MM-DD.")
    parser.add_argument("--asm-csv", required=True, type=Path, help="NSE ASM shortlisted-securities CSV.")
    parser.add_argument("--gsm-csv", required=True, type=Path, help="NSE GSM shortlisted-securities CSV.")
    parser.add_argument("--esm-csv", required=True, type=Path, help="NSE ESM shortlisted-securities CSV.")
    parser.add_argument("--t2t-bhavcopy", required=True, type=Path, help="NSE UDiFF Common Bhavcopy Final ZIP or legacy CSV.")
    parser.add_argument("--pca-csv", required=True, type=Path, help="NSE Periodic Call Auction securities CSV.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--confirm-complete",
        action="store_true",
        help="Confirm all five inputs are latest official complete lists for --as-of.",
    )
    args = parser.parse_args(argv)
    try:
        as_of = date.fromisoformat(args.as_of)
        if as_of > datetime.now(IST).date():
            raise SurveillanceImportError("--as-of cannot be in the future.")
        snapshot = build_snapshot(
            as_of=as_of,
            asm_csv=args.asm_csv,
            gsm_csv=args.gsm_csv,
            esm_csv=args.esm_csv,
            t2t_bhavcopy=args.t2t_bhavcopy,
            pca_csv=args.pca_csv,
            confirm_complete=args.confirm_complete,
        )
        write_snapshot(snapshot, args.output)
    except (ValueError, OSError, SurveillanceImportError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "output": str(args.output),
        "as_of": snapshot["as_of"],
        "complete": snapshot["complete"],
        **snapshot["summary"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
