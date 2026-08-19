from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sma_dashboard.db import DEFAULT_DB_PATH, PROJECT_ROOT, init_db
from sma_dashboard.holdings import ensure_model_update_not_applied
from sma_dashboard.ingestion import DEFAULT_MAPPING_PATH, IngestionResult, ingest_model_update


DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "data" / "raw" / "model_updates_manifest.csv"
REQUIRED_MANIFEST_COLUMNS = {"file", "model_date"}


@dataclass(frozen=True)
class ManifestEntry:
    file: Path
    model_date: str
    notes: str | None = None


@dataclass(frozen=True)
class BatchIngestionResult:
    files_processed: int
    files_skipped: int
    holdings_written: int
    trades_written: int
    rejected_rows: int
    skipped_cash_rows: int
    prices_written: int
    fx_rates_written: int


def load_manifest(manifest_path: Path | str = DEFAULT_MANIFEST_PATH) -> list[ManifestEntry]:
    """Load and validate a private manifest of model updates.

    The manifest's model_date is authoritative for batch ingestion. Relative
    file paths are resolved against the project root so entries like
    data/raw/model_update.xlsx work regardless of the shell's current directory.
    """
    path = _resolve_path(manifest_path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("Manifest is empty.")
        missing = REQUIRED_MANIFEST_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"Manifest missing required columns: {', '.join(sorted(missing))}.")
        entries = [_entry_from_row(row) for row in reader if _has_content(row)]

    _validate_unique_entries(entries)
    return sorted(entries, key=lambda entry: entry.model_date)


def ingest_manifest(
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    db_path: Path | str = DEFAULT_DB_PATH,
    mapping_path: Path | str = DEFAULT_MAPPING_PATH,
    ticker_overrides_path: Path | str | None = None,
    ticker_currency_overrides_path: Path | str | None = None,
    skip_market_data: bool = False,
    replace_date: bool = False,
) -> BatchIngestionResult:
    entries = load_manifest(manifest_path)
    init_db(db_path)

    totals = BatchIngestionResult(0, 0, 0, 0, 0, 0, 0, 0)
    for entry in entries:
        if not replace_date:
            ensure_model_update_not_applied(db_path, entry.model_date)
        result = ingest_model_update(
            entry.file,
            model_date=entry.model_date,
            db_path=db_path,
            mapping_path=mapping_path,
            ticker_overrides_path=ticker_overrides_path,
            ticker_currency_overrides_path=ticker_currency_overrides_path,
            skip_market_data=skip_market_data,
            replace_date=replace_date,
            source="real data",
            notes=entry.notes,
        )
        totals = _add_result(totals, result)
    return totals


def _entry_from_row(row: dict[str, str | None]) -> ManifestEntry:
    raw_file = (row.get("file") or "").strip()
    raw_date = (row.get("model_date") or "").strip()
    if not raw_file:
        raise ValueError("Manifest row is missing file.")
    if not raw_date:
        raise ValueError(f"Manifest row for {raw_file} is missing model_date.")

    model_date = _parse_iso_date(raw_date, raw_file)
    file_path = _resolve_path(raw_file)
    if not file_path.exists():
        raise ValueError(f"Manifest file does not exist: {raw_file}")

    notes = (row.get("notes") or "").strip() or None
    return ManifestEntry(file=file_path, model_date=model_date, notes=notes)


def _has_content(row: dict[str, str | None]) -> bool:
    return any((value or "").strip() for value in row.values())


def _resolve_path(path_value: Path | str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _parse_iso_date(value: str, raw_file: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid model_date for {raw_file}: {value!r}. Use YYYY-MM-DD.") from exc
    return parsed.isoformat()


def _validate_unique_entries(entries: list[ManifestEntry]) -> None:
    files_seen: set[Path] = set()
    dates_seen: set[str] = set()
    for entry in entries:
        file_key = entry.file.resolve()
        if file_key in files_seen:
            raise ValueError(f"Duplicate manifest file row: {entry.file}")
        files_seen.add(file_key)
        if entry.model_date in dates_seen:
            raise ValueError(f"Duplicate manifest model_date row: {entry.model_date}")
        dates_seen.add(entry.model_date)


def _add_result(total: BatchIngestionResult, result: IngestionResult) -> BatchIngestionResult:
    return BatchIngestionResult(
        files_processed=total.files_processed + 1,
        files_skipped=total.files_skipped,
        holdings_written=total.holdings_written + result.holdings_written,
        trades_written=total.trades_written + result.trades_written,
        rejected_rows=total.rejected_rows + result.rejected_rows,
        skipped_cash_rows=total.skipped_cash_rows + result.skipped_cash_rows,
        prices_written=total.prices_written + result.prices_written,
        fx_rates_written=total.fx_rates_written + result.fx_rates_written,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch ingest SMA manager model updates from a private manifest.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--db", "--db-path", dest="db_path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--config", "--mapping-path", dest="mapping_path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--ticker-overrides", type=Path)
    parser.add_argument("--ticker-currency-overrides", type=Path)
    parser.add_argument("--skip-market-data", action="store_true")
    parser.add_argument(
        "--replace-date",
        action="store_true",
        help=(
            "Before each manifest row, delete existing holdings, trades, "
            "and audit status for that model_date."
        ),
    )
    args = parser.parse_args()

    result = ingest_manifest(
        manifest_path=args.manifest,
        db_path=args.db_path,
        mapping_path=args.mapping_path,
        ticker_overrides_path=args.ticker_overrides,
        ticker_currency_overrides_path=args.ticker_currency_overrides,
        skip_market_data=args.skip_market_data,
        replace_date=args.replace_date,
    )
    print(f"files_processed={result.files_processed}")
    print(f"files_skipped={result.files_skipped}")
    print(f"holdings_written={result.holdings_written}")
    print(f"trades_written={result.trades_written}")
    print(f"rejected_rows={result.rejected_rows}")
    print(f"skipped_cash_rows={result.skipped_cash_rows}")
    print(f"prices_written={result.prices_written}")
    print(f"fx_rates_written={result.fx_rates_written}")


if __name__ == "__main__":
    main()
