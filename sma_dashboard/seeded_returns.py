from __future__ import annotations

import argparse
import csv
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from sma_dashboard.db import DEFAULT_DB_PATH, connect, init_db


SEEDED_CUTOFF_DATE = "2025-01-31"
REQUIRED_COLUMNS = ("Date", "Return", "S&P/TSX Composite")


@dataclass(frozen=True)
class SeededReturnsLoadResult:
    rows_loaded: int
    rows_skipped_after_cutoff: int
    malformed_rows_rejected: int


def load_seeded_returns_csv(
    file_path: Path | str,
    db_path: Path | str = DEFAULT_DB_PATH,
    cutoff_date: str = SEEDED_CUTOFF_DATE,
    source: str = "manager reported",
    notes: str | None = None,
) -> SeededReturnsLoadResult:
    """Load exported seeded return CSV reports into seeded_returns.

    The exported file may contain metadata rows before the real table and
    footer rows after it. The real header is detected by required column names.
    Return percentages are stored as percentage values, not decimal returns.
    """
    rows = _read_csv_rows(file_path)
    header_index = _find_header_row(rows)
    header = [_clean_cell(cell) for cell in rows[header_index]]
    indexes = {name: header.index(name) for name in REQUIRED_COLUMNS}
    cutoff = pd.Timestamp(cutoff_date)
    loaded: list[tuple[str, float, float | None, str, str | None]] = []
    skipped = 0
    rejected = 0

    for raw_row in rows[header_index + 1 :]:
        if _is_blank_row(raw_row):
            continue
        try:
            row_date = _parse_month_end(_cell(raw_row, indexes["Date"]))
            return_pct = _parse_percent(_cell(raw_row, indexes["Return"]))
            benchmark_pct = _parse_optional_percent(_cell(raw_row, indexes["S&P/TSX Composite"]))
        except ValueError:
            rejected += 1
            continue
        if row_date > cutoff:
            skipped += 1
            continue
        loaded.append((row_date.date().isoformat(), return_pct, benchmark_pct, source, notes))

    loaded.sort(key=lambda row: row[0])
    db_file = init_db(db_path)
    with closing(connect(db_file)) as conn:
        with conn:
            conn.executemany(
                """
                INSERT INTO seeded_returns (date, return_pct, benchmark_return_pct, source, notes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    return_pct = excluded.return_pct,
                    benchmark_return_pct = excluded.benchmark_return_pct,
                    source = excluded.source,
                    notes = excluded.notes
                """,
                loaded,
            )
    return SeededReturnsLoadResult(len(loaded), skipped, rejected)


def _read_csv_rows(file_path: Path | str) -> list[list[str]]:
    with Path(file_path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def _find_header_row(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows):
        cleaned = {_clean_cell(cell) for cell in row}
        if all(column in cleaned for column in REQUIRED_COLUMNS):
            return index
    raise ValueError(f"Could not find header row containing {REQUIRED_COLUMNS}.")


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return _clean_cell(row[index])


def _clean_cell(value: Any) -> str:
    return str(value).strip()


def _is_blank_row(row: list[str]) -> bool:
    return all(not _clean_cell(cell) for cell in row)


def _parse_month_end(value: str) -> pd.Timestamp:
    if not value:
        raise ValueError("Missing date.")
    for fmt in ("%b %y", "%B %y"):
        try:
            parsed = datetime.strptime(value, fmt)
            return pd.Timestamp(parsed.year, parsed.month, 1) + pd.offsets.MonthEnd(0)
        except ValueError:
            continue
    parsed = pd.to_datetime(value, errors="raise")
    return pd.Timestamp(parsed.year, parsed.month, 1) + pd.offsets.MonthEnd(0)


def _parse_percent(value: str) -> float:
    if not value:
        raise ValueError("Missing percent value.")
    cleaned = value.replace("%", "").replace(",", "").strip()
    return float(cleaned)


def _parse_optional_percent(value: str) -> float | None:
    if not value:
        return None
    return _parse_percent(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load seeded SMA and benchmark returns from exported CSV.")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--cutoff-date", default=SEEDED_CUTOFF_DATE)
    parser.add_argument("--source", default="manager reported")
    parser.add_argument("--notes")
    args = parser.parse_args()
    result = load_seeded_returns_csv(args.file, args.db, args.cutoff_date, args.source, args.notes)
    print(
        "Seeded returns load complete: "
        f"rows_loaded={result.rows_loaded}, "
        f"rows_skipped_after_cutoff={result.rows_skipped_after_cutoff}, "
        f"malformed_rows_rejected={result.malformed_rows_rejected}"
    )


if __name__ == "__main__":
    main()
