from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
import math
import os
from pathlib import Path
import sqlite3

import pandas as pd

from sma_dashboard.db import PROJECT_ROOT, connect
from sma_dashboard.batch_ingestion import DEFAULT_MANIFEST_PATH, load_manifest
from sma_dashboard.performance import BENCHMARK_TICKER, CALCULATED_START_DATE


MISSING_VALUE = "—"
DEFAULT_STARTING_CAPITAL = 100_000.0
PERFORMANCE_RANGE_OPTIONS = (
    "5D",
    "1M",
    "6M",
    "YTD",
    "QTD",
    "1Y",
    "5Y",
    "10Y",
    "Since Inception",
)
COMPRESSED_TRADING_AXIS_RANGES = {"5D", "1M", "6M", "YTD", "QTD", "1Y"}
CALENDAR_AXIS_RANGES = {"5Y", "10Y", "Since Inception"}
DASHBOARD_DEFAULT_DB_PATH = Path(
    os.environ.get("SMA_DASHBOARD_DB", PROJECT_ROOT / "data" / "db" / "sma_dashboard.db")
)
BENCHMARK_REFRESH_COMMAND = (
    "python refresh_benchmark.py --db data/db/sma_dashboard.db "
    f"--benchmark {BENCHMARK_TICKER} --start-date {CALCULATED_START_DATE}"
)
REQUIRED_DASHBOARD_TABLES = {
    "seeded_returns",
    "holdings",
    "trades",
    "prices",
    "fx_rates",
    "rejected_rows",
}
DISPLAY_LABELS = {
    "date": "Date",
    "ticker": "Ticker",
    "currency": "Currency",
    "weight": "Weight",
    "weight_decimal": "Weight",
    "action": "Action",
    "weight_change": "Weight Change",
    "notes": "Notes",
    "pe_trailing": "Trailing P/E",
    "pe_forward": "Forward P/E",
    "pb": "Price / Book",
    "ev_ebitda": "EV / EBITDA",
    "dividend_yield": "Dividend Yield",
    "market_cap": "Market Cap (CAD)",
    "market_cap_cad": "Market Cap (CAD)",
    "publisher": "Source",
    "published_at": "Published",
    "title": "Headline",
    "link": "Link",
    "annualized_volatility": "Annualized Volatility",
    "maximum_drawdown": "Maximum Drawdown",
    "sharpe_ratio": "Sharpe Ratio",
    "beta": "Beta",
    "alpha": "Alpha",
    "tracking_error": "Tracking Error",
    "information_ratio": "Information Ratio",
    "period": "Period",
    "portfolio": "Portfolio",
    "benchmark": "Benchmark",
    "excess": "Excess Return",
    "status": "Status",
    "qtd_return": "QTD CAD Return",
    "start_price_date": "Start Price Date",
    "end_price_date": "End Price Date",
}


@dataclass(frozen=True)
class DatabaseValidation:
    path: Path
    is_valid: bool
    message: str
    missing_tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class BenchmarkPriceCoverage:
    benchmark_ticker: str
    row_count: int
    min_date: str | None
    max_date: str | None
    start_date: str

    @property
    def has_calculated_prices(self) -> bool:
        return self.row_count > 0


@dataclass(frozen=True)
class MarketDataRefreshSummary:
    holding_tickers: tuple[str, ...]
    benchmark_ticker: str
    holding_price_rows_written: int
    fx_rows_written: int
    benchmark_rows_written: int
    latest_holding_price_before: str | None
    latest_holding_price_after: str | None
    latest_benchmark_price_before: str | None
    latest_benchmark_price_after: str | None
    missing_holding_price_tickers_after: tuple[str, ...] = ()
    failed_tickers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def rows_written(self) -> int:
        return self.holding_price_rows_written + self.fx_rows_written + self.benchmark_rows_written

    @property
    def refreshed_anything(self) -> bool:
        return self.rows_written > 0


@dataclass(frozen=True)
class DashboardFreshness:
    latest_model_update_date: str | None
    latest_current_holding_price_date: str | None
    latest_benchmark_price_date: str | None
    latest_calculated_return_date: str | None
    current_holding_price_status: str = "unknown"
    missing_current_holding_price_tickers: tuple[str, ...] = ()


SETUP_COMMANDS = [
    "python init_db.py --db data/db/sma_dashboard.db",
    "python load_seeded_returns.py --file data/raw/seeded_returns.csv --db data/db/sma_dashboard.db --cutoff-date 2025-01-31",
    "python ingestion.py --file data/raw/<model_update.xlsx> --db data/db/sma_dashboard.db --config config/column_mapping.json --ticker-overrides config/ticker_overrides.local.json",
    BENCHMARK_REFRESH_COMMAND,
]


def resolve_dashboard_db_path(path_value: Path | str | None = None) -> Path:
    if path_value is None or str(path_value).strip() == "":
        return DASHBOARD_DEFAULT_DB_PATH
    path = Path(str(path_value).strip())
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def validate_dashboard_database(path_value: Path | str | None = None) -> DatabaseValidation:
    path = resolve_dashboard_db_path(path_value)
    if not path.exists():
        return DatabaseValidation(path, False, "Database file does not exist.")
    try:
        with closing(sqlite3.connect(path)) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
    except sqlite3.Error as exc:
        return DatabaseValidation(path, False, f"Could not open database: {exc}")
    missing = tuple(sorted(REQUIRED_DASHBOARD_TABLES - tables))
    if missing:
        return DatabaseValidation(path, False, "Database is not initialized for the dashboard.", missing)
    return DatabaseValidation(path, True, "Database is ready.")


def load_trade_log(
    db_path: Path | str = DASHBOARD_DEFAULT_DB_PATH,
    ticker: str | None = None,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    query = ["SELECT date, ticker, action, weight_change, notes FROM trades WHERE 1 = 1"]
    params: list[str] = []
    if ticker:
        query.append("AND ticker = ?")
        params.append(ticker)
    if action:
        query.append("AND action = ?")
        params.append(action)
    if start_date:
        query.append("AND date >= ?")
        params.append(start_date)
    if end_date:
        query.append("AND date <= ?")
        params.append(end_date)
    query.append("ORDER BY date DESC, id DESC")
    with closing(connect(db_path)) as conn:
        return pd.read_sql_query(" ".join(query), conn, params=params)


def list_trade_filter_values(db_path: Path | str = DASHBOARD_DEFAULT_DB_PATH) -> tuple[list[str], list[str]]:
    with closing(connect(db_path)) as conn:
        tickers = [row[0] for row in conn.execute("SELECT DISTINCT ticker FROM trades ORDER BY ticker")]
        actions = [row[0] for row in conn.execute("SELECT DISTINCT action FROM trades ORDER BY action")]
    return tickers, actions


def get_table_count(db_path: Path | str, table_name: str) -> int:
    if table_name not in REQUIRED_DASHBOARD_TABLES:
        raise ValueError(f"Unsupported table for dashboard availability check: {table_name}")
    with closing(connect(db_path)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def database_overview(db_path: Path | str = DASHBOARD_DEFAULT_DB_PATH) -> dict[str, int]:
    return {
        table: get_table_count(db_path, table)
        for table in ("seeded_returns", "holdings", "trades", "prices", "fx_rates")
    }


def get_dashboard_freshness(
    db_path: Path | str = DASHBOARD_DEFAULT_DB_PATH,
    benchmark_ticker: str = BENCHMARK_TICKER,
) -> DashboardFreshness:
    price_dates = _current_holding_price_dates(db_path)
    missing_tickers = tuple(ticker for ticker, latest_date in price_dates.items() if latest_date is None)
    return DashboardFreshness(
        latest_model_update_date=_latest_holding_snapshot_date(db_path),
        latest_current_holding_price_date=_latest_complete_current_holding_price_date(price_dates),
        latest_benchmark_price_date=_latest_price_date(db_path, benchmark_ticker),
        latest_calculated_return_date=_latest_calculated_return_date(db_path),
        current_holding_price_status=_current_holding_price_status(price_dates),
        missing_current_holding_price_tickers=missing_tickers,
    )


def refresh_dashboard_market_data(
    db_path: Path | str = DASHBOARD_DEFAULT_DB_PATH,
    benchmark_ticker: str = BENCHMARK_TICKER,
) -> MarketDataRefreshSummary:
    """Refresh current holdings, required FX, and benchmark prices for dashboard use."""
    from sma_dashboard.ingestion import refresh_market_data
    from sma_dashboard.performance import refresh_benchmark_prices_with_summary

    tickers = tuple(_current_active_holding_tickers(db_path))
    latest_holding_before = _latest_current_holding_price_date(db_path)
    latest_benchmark_before = _latest_price_date(db_path, benchmark_ticker)
    holding_rows = 0
    fx_rows = 0
    benchmark_rows = 0
    failed_tickers: list[str] = []
    warnings: list[str] = []

    if not tickers:
        warnings.append("No current holdings found; holding price refresh skipped.")

    for ticker in tickers:
        try:
            ticker_price_rows, ticker_fx_rows = refresh_market_data([ticker], db_path)
            holding_rows += ticker_price_rows
            fx_rows += ticker_fx_rows
        except Exception as exc:
            failed_tickers.append(ticker)
            warnings.append(f"{ticker}: market-data refresh failed: {exc}")

    benchmark_start = _next_refresh_start(latest_benchmark_before, CALCULATED_START_DATE)
    if date.fromisoformat(benchmark_start) <= date.today():
        try:
            benchmark_result = refresh_benchmark_prices_with_summary(
                db_path=db_path,
                benchmark_ticker=benchmark_ticker,
                start_date=benchmark_start,
            )
            benchmark_rows = benchmark_result.rows_written
        except Exception as exc:
            warnings.append(f"{benchmark_ticker}: benchmark refresh failed: {exc}")

    return MarketDataRefreshSummary(
        holding_tickers=tickers,
        benchmark_ticker=benchmark_ticker,
        holding_price_rows_written=holding_rows,
        fx_rows_written=fx_rows,
        benchmark_rows_written=benchmark_rows,
        latest_holding_price_before=latest_holding_before,
        latest_holding_price_after=_latest_current_holding_price_date(db_path),
        latest_benchmark_price_before=latest_benchmark_before,
        latest_benchmark_price_after=_latest_price_date(db_path, benchmark_ticker),
        missing_holding_price_tickers_after=_missing_current_holding_price_tickers(db_path),
        failed_tickers=tuple(failed_tickers),
        warnings=tuple(warnings),
    )


def pending_model_update_notice(
    db_path: Path | str = DASHBOARD_DEFAULT_DB_PATH,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
) -> str | None:
    path = Path(manifest_path)
    if not path.exists():
        return None
    latest_holding = _latest_holding_snapshot_date(db_path)
    if latest_holding is None:
        return None
    try:
        entries = load_manifest(path)
    except Exception as exc:
        return f"Could not inspect model update manifest: {exc}"
    if not entries:
        return None
    latest_manifest = max(entry.model_date for entry in entries)
    if latest_manifest <= latest_holding:
        return None
    return (
        f"Model update manifest includes updates through {latest_manifest}, "
        f"but the latest ingested holdings snapshot is {latest_holding}. "
        "Ingest model updates deliberately before relying on refreshed market data."
    )


def get_calculated_benchmark_price_coverage(
    db_path: Path | str = DASHBOARD_DEFAULT_DB_PATH,
    benchmark_ticker: str = BENCHMARK_TICKER,
    start_date: str = CALCULATED_START_DATE,
) -> BenchmarkPriceCoverage:
    """Inspect calculated-phase benchmark price availability without raising raw SQL errors."""
    try:
        with closing(connect(db_path)) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), MIN(date), MAX(date)
                FROM prices
                WHERE ticker = ? AND date >= ?
                """,
                (benchmark_ticker, start_date),
            ).fetchone()
    except sqlite3.Error:
        return BenchmarkPriceCoverage(benchmark_ticker, 0, None, None, start_date)
    return BenchmarkPriceCoverage(
        benchmark_ticker=benchmark_ticker,
        row_count=int(row[0] or 0),
        min_date=row[1],
        max_date=row[2],
        start_date=start_date,
    )


def _latest_holding_snapshot_date(db_path: Path | str) -> str | None:
    try:
        with closing(connect(db_path)) as conn:
            return conn.execute("SELECT MAX(date) FROM holdings").fetchone()[0]
    except sqlite3.Error:
        return None


def _current_active_holding_tickers(db_path: Path | str) -> list[str]:
    latest = _latest_holding_snapshot_date(db_path)
    if latest is None:
        return []
    try:
        with closing(connect(db_path)) as conn:
            frame = pd.read_sql_query(
                """
                SELECT ticker, weight
                FROM holdings
                WHERE date = ?
                ORDER BY ticker
                """,
                conn,
                params=(latest,),
            )
    except sqlite3.Error:
        return []
    if frame.empty:
        return []
    weights = frame["weight"].astype(float)
    if weights.abs().max() > 1.0 or weights.abs().sum() > 1.5:
        weights = weights / 100.0
    return frame.loc[weights != 0, "ticker"].astype(str).tolist()


def _latest_current_holding_price_date(db_path: Path | str) -> str | None:
    return _latest_complete_current_holding_price_date(_current_holding_price_dates(db_path))


def _current_holding_price_dates(db_path: Path | str) -> dict[str, str | None]:
    tickers = _current_active_holding_tickers(db_path)
    if not tickers:
        return {}
    placeholders = ", ".join("?" for _ in tickers)
    try:
        with closing(connect(db_path)) as conn:
            rows = conn.execute(
                f"""
                SELECT ticker, MAX(date) AS latest_date
                FROM prices
                WHERE ticker IN ({placeholders})
                GROUP BY ticker
                """,
                tickers,
            ).fetchall()
    except sqlite3.Error:
        return {}
    latest_by_ticker = {ticker: None for ticker in tickers}
    latest_by_ticker.update({str(ticker): latest_date for ticker, latest_date in rows})
    return latest_by_ticker


def _latest_complete_current_holding_price_date(price_dates: dict[str, str | None]) -> str | None:
    if not price_dates or any(latest_date is None for latest_date in price_dates.values()):
        return None
    return min(str(latest_date) for latest_date in price_dates.values() if latest_date is not None)


def _current_holding_price_status(price_dates: dict[str, str | None]) -> str:
    if not price_dates:
        return "no_current_holdings"
    if any(latest_date is None for latest_date in price_dates.values()):
        return "missing_prices"
    return "complete"


def _missing_current_holding_price_tickers(db_path: Path | str) -> tuple[str, ...]:
    price_dates = _current_holding_price_dates(db_path)
    return tuple(ticker for ticker, latest_date in price_dates.items() if latest_date is None)


def _latest_price_date(db_path: Path | str, ticker: str) -> str | None:
    try:
        with closing(connect(db_path)) as conn:
            return conn.execute("SELECT MAX(date) FROM prices WHERE ticker = ?", (ticker,)).fetchone()[0]
    except sqlite3.Error:
        return None


def _latest_calculated_return_date(db_path: Path | str) -> str | None:
    try:
        from sma_dashboard.performance import calculate_twr_series_with_quality

        result = calculate_twr_series_with_quality(db_path, strict=False)
    except Exception:
        return None
    if result.data.empty:
        return None
    calculated = result.data[result.data["phase"] == "calculated"]
    if calculated.empty:
        return None
    return str(calculated["date"].max())


def _next_refresh_start(latest_date: str | None, fallback: str) -> str:
    if latest_date is None:
        return fallback
    return (date.fromisoformat(latest_date) + timedelta(days=1)).isoformat()


def format_percent(value: float | None, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return MISSING_VALUE
    return f"{value * 100:.{decimals}f}%"


def format_percent_points(value: float | None, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return MISSING_VALUE
    return f"{value:.{decimals}f}%"


def format_number(value: float | None, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return MISSING_VALUE
    return f"{value:,.{decimals}f}"


def format_ratio(value: float | None, decimals: int = 2) -> str:
    return format_number(value, decimals)


def format_currency_cad(value: float | None) -> str:
    if value is None or pd.isna(value):
        return MISSING_VALUE
    number = float(value)
    abs_value = abs(number)
    sign = "-" if number < 0 else ""
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs_value >= threshold:
            scaled = abs_value / threshold
            compact = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{sign}C${compact}{suffix}"
    return f"{sign}C${abs_value:,.0f}"


def format_currency_cad_full(value: float | None) -> str:
    if value is None or pd.isna(value):
        return MISSING_VALUE
    number = float(value)
    sign = "-" if number < 0 else ""
    return f"{sign}C${abs(number):,.0f}"


def format_date(value: object) -> str:
    if value is None or pd.isna(value):
        return MISSING_VALUE
    try:
        return pd.to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return str(value)


def format_action(value: object) -> str:
    if value is None or pd.isna(value):
        return MISSING_VALUE
    text = str(value).strip()
    return text.capitalize() if text else MISSING_VALUE


def label_for(field_name: str) -> str:
    return DISPLAY_LABELS.get(field_name, field_name)


def apply_display_labels(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(columns={column: label_for(str(column)) for column in frame.columns})


def format_holdings_table(holdings: pd.DataFrame) -> pd.DataFrame:
    display = holdings[
        [column for column in ("date", "ticker", "currency", "weight") if column in holdings.columns]
    ].copy()
    if "date" in display:
        display["date"] = display["date"].map(format_date)
    if "weight" in display:
        display["weight"] = display["weight"].map(format_percent_points)
    return apply_display_labels(display)


def format_valuation_table(valuations: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "ticker",
        "weight",
        "pe_trailing",
        "pe_forward",
        "pb",
        "ev_ebitda",
        "dividend_yield",
        "market_cap",
    ]
    display = valuations[[column for column in columns if column in valuations.columns]].copy()
    if "weight" in display:
        display["weight"] = display["weight"].map(format_percent_points)
    for column in ("pe_trailing", "pe_forward", "pb", "ev_ebitda"):
        if column in display:
            display[column] = display[column].map(format_ratio)
    if "dividend_yield" in display:
        display["dividend_yield"] = display["dividend_yield"].map(format_percent)
    if "market_cap" in display:
        display["market_cap"] = display["market_cap"].map(format_currency_cad)
    return apply_display_labels(display)


def prepare_valuation_table_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Rename columns and format market cap for display.

    - Renames columns for readability
    - Formats market cap from scientific notation to readable currency (C$X,XXX,XXX)
    - Keeps all other numeric columns in numeric form for Streamlit formatting
    """
    df = df.copy()

    # Rename columns for display
    df = df.rename(columns={
        "ticker": "Ticker",
        "weight": "Weight",
        "pe_trailing": "Trailing P/E",
        "pe_forward": "Forward P/E",
        "pb": "Price / Book",
        "ev_ebitda": "EV / EBITDA",
        "dividend_yield": "Dividend Yield",
        "market_cap": "Market Cap (CAD)",
    })

    # Preserve numeric dtypes for columns formatted by Streamlit, including
    # columns whose current values are all missing.
    for column in (
        "Weight",
        "Trailing P/E",
        "Forward P/E",
        "Price / Book",
        "EV / EBITDA",
        "Dividend Yield",
    ):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    # Select only display columns (in order)
    display_cols = [
        "Ticker", "Weight", "Trailing P/E", "Forward P/E",
        "Price / Book", "EV / EBITDA", "Dividend Yield", "Market Cap (CAD)"
    ]
    df = df[[col for col in display_cols if col in df.columns]]

    # Format market cap: convert from scientific notation to readable currency string
    # (e.g., 8.303890e+10 → C$83,038,900,000)
    if "Market Cap (CAD)" in df.columns:
        df["Market Cap (CAD)"] = df["Market Cap (CAD)"].apply(
            lambda x: f"C${x:,.0f}" if pd.notna(x) else None
        )

    return df


def format_weighted_valuation_averages(averages: dict[str, float | None]) -> pd.DataFrame:
    rows = [
        ("pe_trailing", format_ratio(averages.get("pe_trailing"))),
        ("pe_forward", format_ratio(averages.get("pe_forward"))),
        ("pb", format_ratio(averages.get("pb"))),
        ("ev_ebitda", format_ratio(averages.get("ev_ebitda"))),
        ("dividend_yield", format_percent(averages.get("dividend_yield"))),
        ("market_cap_cad", format_currency_cad(averages.get("market_cap"))),
    ]
    return pd.DataFrame([(label_for(metric), value) for metric, value in rows], columns=["Metric", "Value"])


def format_trade_log_table(trades: pd.DataFrame) -> pd.DataFrame:
    display = trades[[column for column in ("date", "ticker", "action", "weight_change", "notes") if column in trades.columns]].copy()
    if "date" in display:
        display["date"] = display["date"].map(format_date)
    if "action" in display:
        display["action"] = display["action"].map(format_action)
    if "weight_change" in display:
        display["weight_change"] = display["weight_change"].map(format_percent_points)
    return apply_display_labels(display.fillna(MISSING_VALUE))


def format_news_table(news: pd.DataFrame) -> pd.DataFrame:
    display = news[[column for column in ("published_at", "ticker", "weight", "title", "publisher", "link") if column in news.columns]].copy()
    if "published_at" in display:
        display["published_at"] = display["published_at"].map(format_date)
    if "weight" in display:
        display["weight"] = display["weight"].map(format_percent_points)
    return apply_display_labels(display.fillna(MISSING_VALUE))


def format_risk_metrics_table(metrics: object) -> pd.DataFrame:
    rows = {
        "annualized_volatility": format_percent(getattr(metrics, "annualized_volatility", None)),
        "maximum_drawdown": format_percent(getattr(metrics, "maximum_drawdown", None)),
        "sharpe_ratio": format_ratio(getattr(metrics, "sharpe_ratio", None)),
        "beta": format_ratio(getattr(metrics, "beta", None)),
        "alpha": format_percent(getattr(metrics, "alpha", None)),
        "tracking_error": format_percent(getattr(metrics, "tracking_error", None)),
        "information_ratio": format_ratio(getattr(metrics, "information_ratio", None)),
    }
    return pd.DataFrame([(label_for(metric), value) for metric, value in rows.items()], columns=["Metric", "Value"])


def prepare_rolling_metrics_chart_data(rolling: pd.DataFrame) -> pd.DataFrame:
    """Prepare display-only rolling metric fields without changing calculations.

    ``rolling_tracking_error`` is stored/calculated as a decimal annualized value.
    The ``tracking_error_pct`` column is only for chart display on a percent axis.
    Missing values are preserved so the chart never forward-fills or interpolates.
    """
    columns = [
        "date",
        "rolling_sharpe",
        "rolling_tracking_error",
        "tracking_error_pct",
        "rolling_sharpe_display",
        "tracking_error_display",
    ]
    if rolling.empty:
        return pd.DataFrame(columns=columns)
    display = rolling.copy()
    if "date" not in display:
        return pd.DataFrame(columns=columns)
    display["date"] = pd.to_datetime(display["date"], errors="coerce")
    display["rolling_sharpe"] = pd.to_numeric(display.get("rolling_sharpe"), errors="coerce")
    display["rolling_tracking_error"] = pd.to_numeric(display.get("rolling_tracking_error"), errors="coerce")
    display = display.dropna(subset=["date"])
    display = display.dropna(subset=["rolling_sharpe", "rolling_tracking_error"], how="all")
    if display.empty:
        return pd.DataFrame(columns=columns)
    display = display.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    display["tracking_error_pct"] = display["rolling_tracking_error"] * 100.0
    display["rolling_sharpe_display"] = display["rolling_sharpe"].map(format_ratio)
    display["tracking_error_display"] = display["rolling_tracking_error"].map(format_percent)
    return display[columns]


def latest_rolling_metrics(rolling: pd.DataFrame) -> dict[str, object] | None:
    chart_data = prepare_rolling_metrics_chart_data(rolling)
    if chart_data.empty:
        return None
    latest = chart_data.iloc[-1]
    return {
        "date": latest["date"],
        "rolling_sharpe": latest["rolling_sharpe"],
        "rolling_tracking_error": latest["rolling_tracking_error"],
    }


def select_rolling_metrics_axis_tick_dates(chart_data: pd.DataFrame) -> list[pd.Timestamp]:
    """Select regular calendar ticks for the rolling metrics chart.

    The rolling metric rows remain limited to actual observations. These ticks
    are display-only calendar guides so the axis has natural month/quarter/year
    spacing rather than labels placed on arbitrary trading days.
    """
    observed = get_observed_chart_dates(chart_data)
    if not observed:
        return []
    first = observed[0]
    last = observed[-1]
    month_span = (last.year - first.year) * 12 + last.month - first.month + 1
    if month_span <= 18:
        start = pd.Timestamp(first.year, first.month, 1)
        ticks = pd.date_range(start=start, end=last, freq="MS")
    elif month_span <= 60:
        start = pd.Timestamp(first.year, ((first.month - 1) // 3) * 3 + 1, 1)
        ticks = pd.date_range(start=start, end=last, freq="QS")
    else:
        start = pd.Timestamp(first.year, 1, 1)
        ticks = pd.date_range(start=start, end=last, freq="YS")
    return _dedupe_timestamps(list(ticks))


def normalize_starting_capital(value: object, default: float = DEFAULT_STARTING_CAPITAL) -> float:
    try:
        capital = float(value)
    except (TypeError, ValueError):
        return default
    if pd.isna(capital) or capital <= 0:
        return default
    return capital


def get_range_start_date(latest_date: object, selected_range: str) -> pd.Timestamp | None:
    latest = pd.Timestamp(latest_date).normalize()
    if selected_range in {"5D", "Since Inception"}:
        return None
    if selected_range == "YTD":
        return pd.Timestamp(latest.year, 1, 1)
    if selected_range == "QTD":
        quarter_month = ((latest.month - 1) // 3) * 3 + 1
        return pd.Timestamp(latest.year, quarter_month, 1)
    offsets = {
        "1M": pd.DateOffset(months=1),
        "6M": pd.DateOffset(months=6),
        "1Y": pd.DateOffset(years=1),
        "5Y": pd.DateOffset(years=5),
        "10Y": pd.DateOffset(years=10),
    }
    if selected_range not in offsets:
        raise ValueError(f"Unsupported performance range: {selected_range}")
    return latest - offsets[selected_range]


def filter_performance_window(frame: pd.DataFrame, selected_range: str) -> pd.DataFrame:
    if frame.empty or "date" not in frame:
        return frame.copy()
    dated = frame.copy()
    dated["date"] = pd.to_datetime(dated["date"])
    dated = dated.sort_values("date").reset_index(drop=True)
    if selected_range == "Since Inception":
        return dated
    if selected_range == "5D":
        dates = dated["date"].drop_duplicates().tail(5)
        return dated[dated["date"].isin(dates)].reset_index(drop=True)
    start = get_range_start_date(dated["date"].max(), selected_range)
    if start is None:
        return dated
    return dated[dated["date"] >= start].reset_index(drop=True)


def rebase_growth_series(
    frame: pd.DataFrame,
    starting_capital: float,
    value_column: str = "cumulative_return",
) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float)
    base_return = float(frame[value_column].iloc[0])
    return starting_capital * (1.0 + frame[value_column].astype(float)) / (1.0 + base_return)


def build_growth_chart_data(
    portfolio: pd.DataFrame,
    benchmark: pd.DataFrame | None = None,
    starting_capital: float = DEFAULT_STARTING_CAPITAL,
    selected_range: str = "Since Inception",
) -> pd.DataFrame:
    portfolio_window = filter_performance_window(portfolio, selected_range)
    if portfolio_window.empty:
        return pd.DataFrame(columns=["date", "series", "growth", "phase", "growth_display"])
    start_date = portfolio_window["date"].min()
    end_date = portfolio_window["date"].max()
    parts = [_growth_part(portfolio_window, "Portfolio", starting_capital)]
    if benchmark is not None and not benchmark.empty:
        benchmark_dated = benchmark.copy()
        benchmark_dated["date"] = pd.to_datetime(benchmark_dated["date"])
        benchmark_window = benchmark_dated[
            (benchmark_dated["date"] >= start_date) & (benchmark_dated["date"] <= end_date)
        ].sort_values("date")
        if not benchmark_window.empty:
            parts.append(_growth_part(benchmark_window, "Benchmark", starting_capital))
    return pd.concat(parts, ignore_index=True).sort_values(["date", "series"]).reset_index(drop=True)


def calculate_trailing_annualized_returns(
    portfolio: pd.DataFrame,
    benchmark: pd.DataFrame | None,
    periods: tuple[int, ...] = (1, 3, 5, 7, 10),
    max_start_lag_days: int = 31,
) -> pd.DataFrame:
    columns = ["period", "portfolio", "benchmark", "excess", "status"]
    if portfolio.empty:
        return pd.DataFrame(columns=columns)
    if benchmark is None or benchmark.empty:
        return pd.DataFrame(
            [
                {
                    "period": f"{years}Y",
                    "portfolio": None,
                    "benchmark": None,
                    "excess": None,
                    "status": "Benchmark unavailable",
                }
                for years in periods
            ],
            columns=columns,
        )

    portfolio_series = _cumulative_series(portfolio)
    benchmark_series = _cumulative_series(benchmark)
    common_dates = portfolio_series.index.intersection(benchmark_series.index).sort_values()
    if common_dates.empty:
        return pd.DataFrame(
            [
                {
                    "period": f"{years}Y",
                    "portfolio": None,
                    "benchmark": None,
                    "excess": None,
                    "status": "Benchmark unavailable",
                }
                for years in periods
            ],
            columns=columns,
        )

    end_date = common_dates.max()
    rows = []
    for years in periods:
        target_start = end_date - pd.DateOffset(years=years)
        eligible_starts = common_dates[common_dates >= target_start]
        if common_dates.min() > target_start or eligible_starts.empty:
            rows.append(_annualized_row(years, None, None, "Insufficient history"))
            continue
        start_date = eligible_starts.min()
        if (start_date - target_start).days > max_start_lag_days:
            rows.append(_annualized_row(years, None, None, "Insufficient history"))
            continue
        portfolio_return = _annualized_return(portfolio_series.loc[start_date], portfolio_series.loc[end_date], start_date, end_date)
        benchmark_return = _annualized_return(benchmark_series.loc[start_date], benchmark_series.loc[end_date], start_date, end_date)
        rows.append(_annualized_row(years, portfolio_return, benchmark_return, "Full"))
    return pd.DataFrame(rows, columns=columns)


def format_annualized_returns_table(frame: pd.DataFrame) -> pd.DataFrame:
    display = frame.copy()
    for column in ("portfolio", "benchmark", "excess"):
        if column in display:
            display[column] = display[column].map(format_percent)
    return apply_display_labels(display.fillna(MISSING_VALUE))


def format_qtd_holding_returns_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["ticker", "weight", "qtd_return", "start_price_date", "end_price_date"]
    display = frame[[column for column in columns if column in frame.columns]].copy()
    if "weight" in display:
        display["weight"] = display["weight"].map(format_percent_points)
    if "qtd_return" in display:
        display["qtd_return"] = display["qtd_return"].map(format_percent)
    for column in ("start_price_date", "end_price_date"):
        if column in display:
            display[column] = display[column].map(format_date)
    return apply_display_labels(display.fillna(MISSING_VALUE))


def _cumulative_series(frame: pd.DataFrame) -> pd.Series:
    dated = frame.copy()
    dated["date"] = pd.to_datetime(dated["date"])
    return dated.set_index("date")["cumulative_return"].astype(float).sort_index()


def _annualized_row(
    years: int,
    portfolio_return: float | None,
    benchmark_return: float | None,
    status: str,
) -> dict[str, object]:
    excess = None if portfolio_return is None or benchmark_return is None else portfolio_return - benchmark_return
    return {
        "period": f"{years}Y",
        "portfolio": portfolio_return,
        "benchmark": benchmark_return,
        "excess": excess,
        "status": status,
    }


def _annualized_return(start_value: float, end_value: float, start_date: pd.Timestamp, end_date: pd.Timestamp) -> float:
    years_elapsed = (end_date - start_date).days / 365.25
    if years_elapsed <= 0:
        return float("nan")
    gross_return = (1.0 + float(end_value)) / (1.0 + float(start_value))
    return float(gross_return ** (1.0 / years_elapsed) - 1.0)


def benchmark_lag_warning(portfolio: pd.DataFrame, benchmark: pd.DataFrame | None, benchmark_ticker: str) -> str | None:
    if portfolio.empty or benchmark is None or benchmark.empty or "date" not in portfolio or "date" not in benchmark:
        return None
    latest_portfolio = pd.to_datetime(portfolio["date"]).max()
    latest_benchmark = pd.to_datetime(benchmark["date"]).max()
    if pd.isna(latest_portfolio) or pd.isna(latest_benchmark) or latest_benchmark >= latest_portfolio:
        return None
    return (
        f"Benchmark data for {benchmark_ticker} ends on {latest_benchmark.date().isoformat()}, "
        f"before the latest portfolio observation on {latest_portfolio.date().isoformat()}. "
        "Refresh benchmark prices to update the comparison."
    )


def get_observed_chart_dates(chart_data: pd.DataFrame) -> list[pd.Timestamp]:
    if chart_data.empty or "date" not in chart_data:
        return []
    dates = pd.to_datetime(chart_data["date"], errors="coerce").dropna().drop_duplicates().sort_values()
    return [pd.Timestamp(value).normalize() for value in dates]


def should_use_compressed_trading_axis(selected_range: str) -> bool:
    return selected_range in COMPRESSED_TRADING_AXIS_RANGES


def add_observation_axis_fields(chart_data: pd.DataFrame) -> pd.DataFrame:
    if chart_data.empty or "date" not in chart_data:
        return chart_data.copy()
    observed = get_observed_chart_dates(chart_data)
    date_to_index = {date: index for index, date in enumerate(observed)}
    enriched = chart_data.copy()
    normalized_dates = pd.to_datetime(enriched["date"]).dt.normalize()
    enriched["observation_index"] = normalized_dates.map(date_to_index).astype(int)
    enriched["observation_date_label"] = normalized_dates.dt.strftime("%b %d")
    return enriched.sort_values(["observation_index", "series"]).reset_index(drop=True)


def select_observation_axis_labels(chart_data: pd.DataFrame, selected_range: str) -> list[dict[str, object]]:
    observed = get_observed_chart_dates(chart_data)
    if not observed:
        return []
    max_labels_by_range = {
        "5D": 5,
        "1M": 6,
        "6M": 7,
        "YTD": 7,
        "QTD": 7,
        "1Y": 7,
    }
    max_labels = max_labels_by_range.get(selected_range, 7)
    selected_indexes = _evenly_spaced_indexes(len(observed), max_labels)
    return [
        {"index": index, "label": observed[index].strftime("%b %d")}
        for index in selected_indexes
    ]


def select_calendar_axis_tick_dates(chart_data: pd.DataFrame, selected_range: str) -> list[pd.Timestamp]:
    observed = get_observed_chart_dates(chart_data)
    if not observed:
        return []
    first = observed[0]
    last = observed[-1]
    year_span = last.year - first.year
    if selected_range == "5Y":
        step_years = 1
    elif selected_range == "10Y":
        step_years = 1 if year_span <= 10 else 2
    elif selected_range == "Since Inception":
        step_years = 1 if year_span <= 15 else 2
    else:
        return select_x_axis_tick_dates(chart_data, selected_range)

    ticks = [first]
    for year in range(first.year + 1, last.year + 1, step_years):
        candidates = [date for date in observed if date.year == year]
        if candidates:
            ticks.append(candidates[0])
    if ticks[-1] != last:
        ticks.append(last)
    return _dedupe_timestamps(ticks)


def select_x_axis_tick_dates(chart_data: pd.DataFrame, selected_range: str, max_ticks: int = 8) -> list[pd.Timestamp]:
    """Return x-axis ticks from actual observed chart dates only.

    Altair's continuous time axis may choose weekend calendar ticks for short
    windows. These tick values keep labels tied to real portfolio/benchmark
    observations and deduplicate long-form series rows.
    """
    observed = get_observed_chart_dates(chart_data)
    if not observed:
        return []
    if selected_range == "5D":
        return observed
    if len(observed) <= max_ticks:
        return observed

    step = max(1, math.ceil((len(observed) - 1) / (max_ticks - 1)))
    ticks = observed[::step]
    if ticks[-1] != observed[-1]:
        ticks.append(observed[-1])
    return ticks


def _evenly_spaced_indexes(length: int, max_labels: int) -> list[int]:
    if length <= 0:
        return []
    if length <= max_labels:
        return list(range(length))
    step = (length - 1) / (max_labels - 1)
    indexes = [round(step * position) for position in range(max_labels)]
    indexes[0] = 0
    indexes[-1] = length - 1
    return sorted(set(indexes))


def _dedupe_timestamps(values: list[pd.Timestamp]) -> list[pd.Timestamp]:
    seen = set()
    deduped = []
    for value in values:
        normalized = pd.Timestamp(value).normalize()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def calculate_growth_y_axis_domain(
    chart_data: pd.DataFrame,
    starting_capital: float,
    padding_ratio: float = 0.10,
    minimum_span_ratio: float = 0.015,
) -> tuple[float, float] | None:
    """Return a readable y-axis domain for visible growth-of-capital data.

    The dashboard rebases returns for display only. This helper keeps short
    ranges readable without forcing the y-axis to zero, while enforcing a
    minimum span so tiny daily moves are not over-amplified.
    """
    if chart_data.empty or "growth" not in chart_data:
        return None

    values = pd.to_numeric(chart_data["growth"], errors="coerce").dropna()
    if values.empty:
        return None

    visible_min = float(values.min())
    visible_max = float(values.max())
    visible_span = visible_max - visible_min
    padding = visible_span * padding_ratio
    lower = visible_min - padding
    upper = visible_max + padding

    normalized_capital = normalize_starting_capital(starting_capital)
    minimum_span = normalized_capital * minimum_span_ratio
    if upper - lower < minimum_span:
        center = (visible_min + visible_max) / 2.0
        half_span = minimum_span / 2.0
        lower = center - half_span
        upper = center + half_span

    return (lower, upper)


def performance_range_note(original: pd.DataFrame, filtered: pd.DataFrame, selected_range: str) -> str | None:
    if original.empty or filtered.empty or selected_range == "Since Inception":
        return None
    original_dates = pd.to_datetime(original["date"]).drop_duplicates()
    filtered_dates = pd.to_datetime(filtered["date"]).drop_duplicates()
    if selected_range == "5D" and len(filtered_dates) < min(5, len(original_dates)):
        return "Showing the available observations for this range."
    requested_start = get_range_start_date(original_dates.max(), selected_range)
    if requested_start is not None and filtered_dates.min() > requested_start:
        return "Selected range starts before available data, so the chart begins at the earliest available point."
    return None


def _growth_part(frame: pd.DataFrame, series_name: str, starting_capital: float) -> pd.DataFrame:
    part = frame.copy()
    part["growth"] = rebase_growth_series(part, starting_capital)
    part["series"] = series_name
    part["phase"] = part.get("phase", "unknown").astype(str).str.capitalize()
    part["growth_display"] = part["growth"].map(format_currency_cad_full)
    return part[["date", "series", "growth", "phase", "growth_display"]]


def apply_date_filter(frame: pd.DataFrame, start_date: str | None, end_date: str | None) -> pd.DataFrame:
    if frame.empty or "date" not in frame:
        return frame
    dated = frame.copy()
    dated["_date"] = pd.to_datetime(dated["date"])
    if start_date:
        dated = dated[dated["_date"] >= pd.Timestamp(start_date)]
    if end_date:
        dated = dated[dated["_date"] <= pd.Timestamp(end_date)]
    return dated.drop(columns=["_date"])


def build_performance_chart_frame(portfolio: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> pd.DataFrame:
    """Create phase-separated cumulative return columns for Streamlit line charts."""
    parts = []
    portfolio_part = _phase_columns(
        portfolio,
        value_column="cumulative_return",
        prefix="Portfolio",
    )
    parts.append(portfolio_part)
    if benchmark is not None and not benchmark.empty:
        parts.append(_phase_columns(benchmark, value_column="cumulative_return", prefix="Benchmark"))
    chart = pd.concat(parts, axis=1).sort_index()
    return chart


def _phase_columns(frame: pd.DataFrame, value_column: str, prefix: str) -> pd.DataFrame:
    dated = frame.copy()
    dated["date"] = pd.to_datetime(dated["date"])
    dated = dated.set_index("date").sort_index()
    result = pd.DataFrame(index=dated.index)
    for phase in ("seeded", "calculated"):
        label = f"{prefix} ({phase})"
        result[label] = dated[value_column].where(dated["phase"] == phase)
    return result
