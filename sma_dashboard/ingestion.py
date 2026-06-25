from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from sma_dashboard.db import DEFAULT_DB_PATH, PROJECT_ROOT, connect, init_db


DEFAULT_MAPPING_PATH = PROJECT_ROOT / "config" / "column_mapping.json"
FX_PAIR = "CADUSD=X"
CAD_SUFFIXES = (".TO", ".V")
TRADE_ACTIONS = {"buy", "sell", "trim", "add"}
POSITIVE_ACTIONS = {"buy", "add"}
NEGATIVE_ACTIONS = {"sell", "trim"}


@dataclass(frozen=True)
class IngestionResult:
    holdings_written: int
    trades_written: int
    rejected_rows: int
    skipped_cash_rows: int
    prices_written: int
    fx_rates_written: int


def load_column_mapping(path: Path | str = DEFAULT_MAPPING_PATH) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        mapping = json.load(handle)
    if "columns" not in mapping or not isinstance(mapping["columns"], dict):
        raise ValueError("Column mapping must contain a 'columns' object.")
    for required in ("weight",):
        if required not in mapping["columns"]:
            raise ValueError(f"Column mapping missing required field: {required}")
    if "ticker" not in mapping["columns"] and "raw_ticker" not in mapping["columns"]:
        raise ValueError("Column mapping missing required field: ticker or raw_ticker")
    return mapping


def load_ticker_overrides(path: Path | str | None = None) -> dict[str, str]:
    if path is None:
        return {}
    override_path = Path(path)
    if not override_path.exists():
        return {}
    with override_path.open("r", encoding="utf-8") as handle:
        return {str(key).strip().upper(): str(value).strip().upper() for key, value in json.load(handle).items()}


def ingest_model_update(
    excel_path: Path | str,
    model_date: str | date | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
    mapping_path: Path | str = DEFAULT_MAPPING_PATH,
    ticker_overrides_path: Path | str | None = None,
    skip_market_data: bool = False,
) -> IngestionResult:
    """Parse one manager model update and persist M1 data.

    Prices are stored as returned by yfinance. USD-listed tickers also store
    CADUSD=X rates so performance.py can convert returns to CAD reproducibly.
    """
    db_file = init_db(db_path)
    mapping = load_column_mapping(mapping_path)
    ticker_overrides = load_ticker_overrides(ticker_overrides_path)
    frame = _read_model_update_excel(excel_path, mapping)
    resolved_model_date = _resolve_model_date(excel_path, model_date, mapping, frame)

    holdings: list[tuple[str, str, float, float | None, float | None]] = []
    trades: list[tuple[str, str, str, float, str | None]] = []
    rejected: list[tuple[str, str, str, str]] = []
    skipped_cash_rows = 0

    for _, row in frame.iterrows():
        raw = _row_to_jsonable_dict(row)
        if _is_ignorable_row(row, mapping):
            continue
        if _is_cash_row(row, mapping):
            skipped_cash_rows += 1
            continue
        try:
            holding, trade = _parse_row(row, mapping, resolved_model_date, ticker_overrides)
            holdings.append(holding)
            if trade is not None:
                trades.append(trade)
        except ValueError as exc:
            rejected.append(
                (
                    datetime.now().isoformat(timespec="seconds"),
                    str(excel_path),
                    json.dumps(raw, sort_keys=True, default=str),
                    str(exc),
                )
            )

    with closing(connect(db_file)) as conn:
        with conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO holdings (date, ticker, weight, shares, cost_basis)
                VALUES (?, ?, ?, ?, ?)
                """,
                holdings,
            )
            conn.executemany(
                """
                INSERT INTO trades (date, ticker, action, weight_change, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                trades,
            )
            conn.executemany(
                """
                INSERT INTO rejected_rows (date_attempted, source_file, raw_row_data, reason)
                VALUES (?, ?, ?, ?)
                """,
                rejected,
            )

    prices_written = 0
    fx_rates_written = 0
    if holdings and not skip_market_data:
        tickers = sorted({holding[1] for holding in holdings})
        update_date = min(holding[0] for holding in holdings)
        prices_written, fx_rates_written = refresh_market_data(tickers, db_file, update_date)

    return IngestionResult(
        holdings_written=len(holdings),
        trades_written=len(trades),
        rejected_rows=len(rejected),
        skipped_cash_rows=skipped_cash_rows,
        prices_written=prices_written,
        fx_rates_written=fx_rates_written,
    )


def _read_model_update_excel(excel_path: Path | str, mapping: dict[str, Any]) -> pd.DataFrame:
    if mapping.get("required_headers"):
        raw = pd.read_excel(excel_path, sheet_name=mapping.get("sheet_name", 0), header=None)
        header_index = _find_header_row(raw, mapping["required_headers"])
        header = [_clean_header(value) for value in raw.iloc[header_index].tolist()]
        frame = raw.iloc[header_index + 1 :].copy()
        frame.columns = header
        frame = frame.dropna(axis=1, how="all")
        return frame.reset_index(drop=True)
    return pd.read_excel(
        excel_path,
        sheet_name=mapping.get("sheet_name", 0),
        header=mapping.get("header", 0),
    )


def _find_header_row(frame: pd.DataFrame, required_headers: list[str]) -> int:
    required = {_clean_header(value) for value in required_headers}
    for index, row in frame.iterrows():
        present = {_clean_header(value) for value in row.tolist() if _clean_header(value)}
        if required.issubset(present):
            return int(index)
    raise ValueError(f"Could not find model update header row with required headers: {sorted(required)}")


def _clean_header(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _resolve_model_date(
    excel_path: Path | str,
    model_date: str | date | None,
    mapping: dict[str, Any],
    frame: pd.DataFrame,
) -> str | None:
    if model_date is not None:
        return _to_iso_date(model_date)
    filename_date = _date_from_filename(excel_path)
    if filename_date is not None:
        return filename_date
    date_column = mapping["columns"].get("date")
    if date_column and date_column in frame.columns:
        return None
    raise ValueError(
        "Missing model update date. Provide --model-date YYYY-MM-DD or include YYYY-MM-DD in the filename."
    )


def _date_from_filename(excel_path: Path | str) -> str | None:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", Path(excel_path).name)
    if match is None:
        return None
    return _to_iso_date(match.group(1))


def refresh_market_data(
    tickers: list[str],
    db_path: Path | str = DEFAULT_DB_PATH,
    fallback_start_date: str | date | None = None,
) -> tuple[int, int]:
    """Fetch prices for current tickers and FX rates required for USD listings."""
    init_db(db_path)
    fallback = _to_iso_date(fallback_start_date) if fallback_start_date else date.today().isoformat()
    since_inception = _get_since_inception_date(db_path) or fallback
    price_rows: list[tuple[str, str, float, float]] = []
    fx_rows: list[tuple[str, str, float]] = []

    with closing(connect(db_path)) as conn:
        with conn:
            existing = _existing_tickers(conn)
            for ticker in tickers:
                start = since_inception if ticker not in existing else _next_price_start(conn, ticker, fallback)
                end = _tomorrow_iso()
                if start >= end:
                    continue
                prices = _download_price_frame(ticker, start, end)
                if prices.empty:
                    continue
                if is_usd_listed(ticker):
                    fx = _download_fx_frame(start, end)
                    if fx.empty:
                        raise RuntimeError(f"Missing FX data for {FX_PAIR} from {start} to {end}.")
                    fx_rows.extend(_fx_rows_from_frame(fx))
                price_rows.extend(_price_rows_from_frame(ticker, prices))

            conn.executemany(
                """
                INSERT OR REPLACE INTO prices (date, ticker, close, adj_close)
                VALUES (?, ?, ?, ?)
                """,
                price_rows,
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO fx_rates (date, pair, rate)
                VALUES (?, ?, ?)
                """,
                sorted(set(fx_rows)),
            )

    return len(price_rows), len(set(fx_rows))


def is_usd_listed(ticker: str) -> bool:
    return not ticker.upper().endswith(CAD_SUFFIXES)


def _parse_row(
    row: pd.Series,
    mapping: dict[str, Any],
    model_date: str | date | None,
    ticker_overrides: dict[str, str] | None = None,
) -> tuple[tuple[str, str, float, float | None, float | None], tuple[str, str, str, float, str | None] | None]:
    columns = mapping["columns"]
    row_date = _optional_cell(row, columns.get("date"))
    parsed_date = _to_iso_date(row_date if row_date is not None else model_date)
    ticker_source = columns.get("raw_ticker") or columns.get("ticker")
    ticker = normalize_ticker(_required_cell(row, ticker_source, "ticker"), ticker_overrides or {})
    weight = _parse_number(_required_cell(row, columns["weight"], "weight"), "weight")
    weight = _normalize_percent(weight, mapping.get("weight_input", "percent"), "weight")
    shares = _parse_optional_number(_optional_cell(row, columns.get("shares")), "shares")
    cost_basis = _parse_optional_number(_optional_cell(row, columns.get("cost_basis")), "cost_basis")

    holding = (parsed_date, ticker, weight, shares, cost_basis)
    action_cell = _optional_cell(row, columns.get("action"))
    change_cell = _optional_cell(row, columns.get("weight_change"))
    notes = _parse_optional_text(_optional_cell(row, columns.get("notes")))

    action = _parse_action(action_cell)
    if action is None:
        return holding, None

    if change_cell is None:
        raise ValueError("Recognized trade action requires weight_change.")
    weight_change = _parse_number(change_cell, "weight_change")
    weight_change = _normalize_percent(
        weight_change,
        mapping.get("weight_change_input", "percent"),
        "weight_change",
    )
    _validate_action_sign(action, weight_change)
    return holding, (parsed_date, ticker, action, weight_change, notes)


def normalize_ticker(raw_ticker: Any, ticker_overrides: dict[str, str] | None = None) -> str:
    raw = str(raw_ticker).strip().upper()
    overrides = ticker_overrides or {}
    if raw in overrides:
        return _parse_ticker(overrides[raw])
    if raw.endswith(" CN"):
        base = raw[:-3].strip().replace("/", "-").replace(".", "-").replace(" ", "")
        return _parse_ticker(f"{base}.TO")
    return _parse_ticker(raw)


def _parse_action(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in TRADE_ACTIONS:
        return text
    return None


def _is_cash_row(row: pd.Series, mapping: dict[str, Any]) -> bool:
    columns = mapping["columns"]
    description = str(_optional_cell(row, columns.get("security_description")) or "").strip().upper()
    raw_ticker = str(_optional_cell(row, columns.get("raw_ticker") or columns.get("ticker")) or "").strip().upper()
    cusip = str(_optional_cell(row, columns.get("cusip")) or "").strip().upper()
    if description in {"CANADIAN DOLLAR", "CASH", "CASH AND EQUIVALENTS"}:
        return True
    return raw_ticker.isdigit() and (not cusip or cusip.isdigit())


def _is_ignorable_row(row: pd.Series, mapping: dict[str, Any]) -> bool:
    if all(_optional_cell(row, str(column)) is None for column in row.index):
        return True
    columns = mapping["columns"]
    description = str(_optional_cell(row, columns.get("security_description")) or "").strip().upper()
    ticker = _optional_cell(row, columns.get("raw_ticker") or columns.get("ticker"))
    weight = _optional_cell(row, columns.get("weight"))
    if description in {"TOTAL", "TOTALS", "GRAND TOTAL"}:
        return True
    if ticker is None and weight is not None:
        return _looks_like_total_weight(weight)
    return ticker is None and weight is None


def _looks_like_total_weight(value: Any) -> bool:
    try:
        return abs(_parse_number(value, "weight") - 100.0) < 0.000001
    except ValueError:
        return False


def _required_cell(row: pd.Series, column: str, field: str) -> Any:
    value = _optional_cell(row, column)
    if value is None:
        raise ValueError(f"Missing required {field}.")
    return value


def _optional_cell(row: pd.Series, column: str | None) -> Any | None:
    if not column or column not in row:
        return None
    value = row[column]
    if pd.isna(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _parse_ticker(value: Any) -> str:
    ticker = str(value).strip().upper()
    if not ticker:
        raise ValueError("Missing required ticker.")
    if any(char.isspace() for char in ticker):
        raise ValueError(f"Invalid ticker '{ticker}': whitespace is not allowed.")
    return ticker


def _parse_number(value: Any, field: str) -> float:
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1]
        value = cleaned
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric value for {field}: {value!r}.") from exc
    if pd.isna(number):
        raise ValueError(f"Invalid numeric value for {field}: {value!r}.")
    return number


def _parse_optional_number(value: Any | None, field: str) -> float | None:
    if value is None:
        return None
    return _parse_number(value, field)


def _normalize_percent(value: float, input_kind: str, field: str) -> float:
    if input_kind == "decimal":
        return value * 100
    if input_kind == "percent":
        return value
    raise ValueError(f"Invalid {field} input mode '{input_kind}'. Use 'percent' or 'decimal'.")


def _parse_optional_text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_iso_date(value: Any) -> str:
    if value is None:
        raise ValueError("Missing model update date. Provide a date column or --model-date.")
    try:
        parsed = pd.to_datetime(value, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid date value: {value!r}.") from exc
    return parsed.date().isoformat()


def _validate_action_sign(action: str, weight_change: float) -> None:
    if action in POSITIVE_ACTIONS and weight_change <= 0:
        raise ValueError(f"Action '{action}' requires a positive weight_change.")
    if action in NEGATIVE_ACTIONS and weight_change >= 0:
        raise ValueError(f"Action '{action}' requires a negative weight_change.")


def _row_to_jsonable_dict(row: pd.Series) -> dict[str, Any]:
    return {str(key): None if pd.isna(value) else value for key, value in row.to_dict().items()}


def _get_since_inception_date(db_path: Path | str) -> str | None:
    with closing(connect(db_path)) as conn:
        value = conn.execute("SELECT MIN(date) FROM seeded_returns").fetchone()[0]
    return value


def _existing_tickers(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT DISTINCT ticker FROM prices")}


def _next_price_start(conn: sqlite3.Connection, ticker: str, fallback: str) -> str:
    latest = conn.execute("SELECT MAX(date) FROM prices WHERE ticker = ?", (ticker,)).fetchone()[0]
    if latest is None:
        return fallback
    return (date.fromisoformat(latest) + timedelta(days=1)).isoformat()


def _tomorrow_iso() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def _download_price_frame(ticker: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    frame = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
    return _flatten_yfinance_frame(frame)


def _download_fx_frame(start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    frame = yf.download(FX_PAIR, start=start, end=end, progress=False, auto_adjust=False)
    return _flatten_yfinance_frame(frame)


def _flatten_yfinance_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    return frame


def _convert_usd_prices_to_cad(prices: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    merged = prices.join(fx[["Close"]].rename(columns={"Close": "FxRate"}), how="left")
    merged["FxRate"] = merged["FxRate"].ffill().bfill()
    if merged["FxRate"].isna().any():
        raise RuntimeError("FX conversion failed because one or more daily rates are missing.")
    converted = prices.copy()
    converted["Close"] = merged["Close"] / merged["FxRate"]
    adj_source = "Adj Close" if "Adj Close" in merged else "Close"
    converted["Adj Close"] = merged[adj_source] / merged["FxRate"]
    return converted


def _price_rows_from_frame(ticker: str, frame: pd.DataFrame) -> list[tuple[str, str, float, float]]:
    rows = []
    for index, row in frame.iterrows():
        close = row.get("Close")
        adj_close = row.get("Adj Close", close)
        if pd.isna(close) or pd.isna(adj_close):
            continue
        rows.append((index.date().isoformat(), ticker, float(close), float(adj_close)))
    return rows


def _fx_rows_from_frame(frame: pd.DataFrame) -> list[tuple[str, str, float]]:
    rows = []
    for index, row in frame.iterrows():
        rate = row.get("Close")
        if pd.isna(rate):
            continue
        rows.append((index.date().isoformat(), FX_PAIR, float(rate)))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest one SMA manager model update.")
    parser.add_argument("excel_path", type=Path, nargs="?")
    parser.add_argument("--file", dest="file_path", type=Path)
    parser.add_argument("--model-date", help="Model update date if the Excel file has no date column.")
    parser.add_argument("--db", "--db-path", dest="db_path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--config", "--mapping-path", dest="mapping_path", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--ticker-overrides", type=Path)
    parser.add_argument(
        "--skip-market-data",
        action="store_true",
        help="Only parse/write holdings and trades; useful for offline validation.",
    )
    args = parser.parse_args()
    excel_path = args.file_path or args.excel_path
    if excel_path is None:
        parser.error("Provide a model update workbook with --file or positional excel_path.")
    result = ingest_model_update(
        excel_path,
        model_date=args.model_date,
        db_path=args.db_path,
        mapping_path=args.mapping_path,
        ticker_overrides_path=args.ticker_overrides,
        skip_market_data=args.skip_market_data,
    )
    print(result)


if __name__ == "__main__":
    main()
