"""Tripwire: source and operational scripts must remain advisory-only.

Only the read-only Kite margin-estimation endpoints are allowlisted. This test
scans source text rather than relying on a runtime mock, so a newly introduced
broker mutation reference fails CI before it can be used.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (PROJECT_ROOT / "src", PROJECT_ROOT / "scripts")

# Cover Kite Connect method names and common plural/reversed naming variants.
# order_margins is intentionally excluded: it estimates hypothetical margin
# and does not submit or mutate an order.
FORBIDDEN_BROKER_MUTATIONS = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?:place|modify|cancel|delete|submit|trigger)_(?:orders?|gtts?)"
    r"|(?:orders?|gtts?)_(?:place|modify|cancel|delete|submit|trigger)"
    r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(files)


def test_source_and_scripts_do_not_reference_broker_mutation_apis() -> None:
    violations: list[str] = []
    for path in _source_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary assets are not executable source; skip them.
            continue
        for line_number, line in enumerate(source.splitlines(), start=1):
            match = FORBIDDEN_BROKER_MUTATIONS.search(line)
            if match:
                relative = path.relative_to(PROJECT_ROOT)
                violations.append(f"{relative}:{line_number}: {match.group(0)}")

    assert not violations, (
        "Advisory-only boundary violated. Broker order/GTT mutation references "
        "are forbidden under src/ and scripts/. Only read-only margin estimation "
        "is permitted. Found:\n" + "\n".join(violations)
    )
