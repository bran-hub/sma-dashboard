from __future__ import annotations

import argparse
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import pandas as pd

from sma_dashboard.db import DEFAULT_DB_PATH, connect, init_db
from sma_dashboard.ingestion import FX_PAIR, _download_price_frame, _price_rows_from_frame


BENCHMARK_TICKER = "^GSPTSE"
CALCULATED_START_DATE = "2025-02-01"
SEEDED_END_DATE = "2025-01-31"
TRADING_DAYS_PER_YEAR = 252
Period = Literal["1D", "MTD", "QTD", "YTD", "since_inception", "custom"]


class PerformanceDataError(ValueError):
    """Raised when required M2 performance inputs are missing or inconsistent."""


@dataclass(frozen=True)
class RiskMetrics:
    annualized_volatility: float
    maximum_drawdown: float
    sharpe_ratio: float | None
    beta: float | None
    alpha: float | None
    tracking_error: float | None
    information_ratio: float | None


@dataclass(frozen=True)
class BenchmarkRefreshResult:
    benchmark_ticker: str
    rows_written: int
    min_date: str | None
    max_date: str | None


@dataclass(frozen=True)
class PerformanceDataIssue:
    issue_type: str
    date: str
    ticker: str | None
    message: str


@dataclass(frozen=True)
class PriceCoverageReport:
    issues: list[PerformanceDataIssue]

    @property
    def is_complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class PerformanceCalculationResult:
    data: pd.DataFrame
    issues: list[PerformanceDataIssue]


@dataclass(frozen=True)
class HoldingReturnRankingResult:
    returns: pd.DataFrame
    top: pd.DataFrame
    bottom: pd.DataFrame
    issues: list[PerformanceDataIssue]


def get_since_inception_date(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """Return the locked since-inception anchor from seeded_returns, never hardcoded."""
    with closing(connect(db_path)) as conn:
        value = conn.execute("SELECT MIN(date) FROM seeded_returns").fetchone()[0]
    if value is None:
        raise PerformanceDataError("No seeded_returns rows found; since-inception date is unavailable.")
    return str(value)


def calculate_daily_portfolio_returns(
    db_path: Path | str = DEFAULT_DB_PATH,
    start_date: str = CALCULATED_START_DATE,
    end_date: str | None = None,
    strict: bool = True,
) -> pd.DataFrame:
    """Calculate daily CAD TWR inputs from holdings snapshots and adjusted prices.

    A holdings snapshot becomes effective on the next observed trading day.
    Snapshot weights then drift with security returns until the next snapshot;
    any residual allocation is treated as zero-return cash.
    TWR is used because this project evaluates manager decisions independent of
    contribution or withdrawal timing; MWR/IRR is intentionally out of scope.
    """
    return calculate_daily_portfolio_returns_with_quality(
        db_path=db_path,
        start_date=start_date,
        end_date=end_date,
        strict=strict,
    ).data


def calculate_daily_portfolio_returns_with_quality(
    db_path: Path | str = DEFAULT_DB_PATH,
    start_date: str = CALCULATED_START_DATE,
    end_date: str | None = None,
    strict: bool = True,
) -> PerformanceCalculationResult:
    """Calculate daily portfolio returns and optionally collect data-quality issues.

    Strict mode raises as soon as required price or FX data is missing. Dashboard
    mode returns blocking issues instead of partial calculated returns, and never
    renormalizes remaining holdings or treats missing equity data as cash.
    """
    holdings = _load_holdings(db_path)
    if holdings.empty:
        raise PerformanceDataError("No holdings rows found; cannot calculate portfolio returns.")

    coverage = audit_price_coverage(db_path, start_date=start_date, end_date=end_date)
    if not coverage.is_complete:
        if strict:
            raise PerformanceDataError(_coverage_error_message(coverage.issues))
        return PerformanceCalculationResult(pd.DataFrame(columns=["date", "daily_return", "phase"]), coverage.issues)

    issues: list[PerformanceDataIssue] = []
    tickers = [ticker for ticker, _ in _active_holdings_in_period(holdings, pd.Timestamp(start_date), pd.Timestamp(end_date) if end_date else None)]
    currencies = {ticker: _holding_currency(holdings, ticker) for ticker in tickers}
    prices = _load_cad_adjusted_prices(
        db_path,
        tickers,
        currencies=currencies,
        strict=strict,
        issues=issues,
    )
    returns = _calculate_price_returns(prices)
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date) if end_date else returns.index.max()
    if pd.isna(end) and not strict and issues:
        issue_date = next((issue.date for issue in issues if issue.date), start.date().isoformat())
        issues.append(
            PerformanceDataIssue(
                issue_type="skipped_return_date",
                date=issue_date,
                ticker=None,
                message="Skipped calculated return dates because required data was missing.",
            )
        )
        return PerformanceCalculationResult(pd.DataFrame(columns=["date", "daily_return", "phase"]), issues)
    if pd.isna(end):
        raise PerformanceDataError("No price returns available for held tickers.")

    rows: list[dict[str, object]] = []
    current_weights: dict[str, float] = {}
    applied_snapshot_date: pd.Timestamp | None = None
    for day in returns.loc[(returns.index >= start) & (returns.index <= end)].index:
        snapshot = _latest_holdings_snapshot(holdings, day, effective_next_day=True)
        if snapshot.empty:
            continue
        snapshot_date = pd.Timestamp(snapshot["date"].iloc[0])
        if applied_snapshot_date is None or snapshot_date != applied_snapshot_date:
            current_weights = _snapshot_weights(snapshot)
            applied_snapshot_date = snapshot_date
        if not current_weights:
            continue
        active_tickers = list(current_weights)
        day_returns = returns.loc[day, active_tickers]
        missing = day_returns[day_returns.isna()].index.tolist()
        if missing:
            if day_returns.isna().all():
                continue
            message = f"Missing price return for {missing} on {day.date().isoformat()}."
            if strict:
                raise PerformanceDataError(message)
            for ticker in missing:
                issues.append(
                    PerformanceDataIssue(
                        issue_type="missing_price",
                        date=day.date().isoformat(),
                        ticker=str(ticker),
                        message=f"Missing price return for {ticker} on {day.date().isoformat()}.",
                    )
                )
            issues.append(
                PerformanceDataIssue(
                    issue_type="skipped_return_date",
                    date=day.date().isoformat(),
                    ticker=None,
                    message=f"Skipped portfolio return date {day.date().isoformat()} because required data was missing.",
                )
            )
            if not strict:
                return PerformanceCalculationResult(pd.DataFrame(columns=["date", "daily_return", "phase"]), issues)
            continue
        portfolio_return = float(
            sum(current_weights[ticker] * float(day_returns[ticker]) for ticker in active_tickers)
        )
        rows.append(
            {
                "date": day.date().isoformat(),
                "daily_return": portfolio_return,
                "phase": "calculated",
            }
        )
        gross_portfolio = 1.0 + portfolio_return
        if gross_portfolio <= 0:
            raise PerformanceDataError(
                f"Portfolio value became non-positive on {day.date().isoformat()}."
            )
        current_weights = {
            ticker: current_weights[ticker] * (1.0 + float(day_returns[ticker])) / gross_portfolio
            for ticker in active_tickers
        }

    if not rows and not strict and issues:
        return PerformanceCalculationResult(pd.DataFrame(columns=["date", "daily_return", "phase"]), issues)
    if not rows:
        raise PerformanceDataError("No calculated daily returns available for the requested date range.")
    return PerformanceCalculationResult(pd.DataFrame(rows), issues)


def audit_price_coverage(
    db_path: Path | str = DEFAULT_DB_PATH,
    start_date: str = CALCULATED_START_DATE,
    end_date: str | None = None,
) -> PriceCoverageReport:
    """Check that active holdings have enough price/FX data before returns are calculated."""
    holdings = _load_holdings(db_path)
    if holdings.empty:
        return PriceCoverageReport(
            [
                PerformanceDataIssue(
                    issue_type="missing_holdings",
                    date="",
                    ticker=None,
                    message="No holdings rows found; cannot audit price coverage.",
                )
            ]
        )

    audit_start = pd.Timestamp(start_date)
    audit_end = pd.Timestamp(end_date) if end_date else None
    active = _active_holdings_in_period(holdings, audit_start, audit_end)
    issues: list[PerformanceDataIssue] = []

    for ticker, first_active_date in active:
        prices = _load_price_series(db_path, ticker)
        if prices.empty:
            issues.append(
                PerformanceDataIssue(
                    issue_type="missing_price",
                    date=pd.Timestamp(first_active_date).date().isoformat(),
                    ticker=ticker,
                    message=(
                        f"Missing stored prices for active holding {ticker}. "
                        "Refresh market data or add a local ticker override."
                    ),
                )
            )
            continue

        usable = prices.dropna(subset=["adj_close"])
        if usable.empty:
            issues.append(
                PerformanceDataIssue(
                    issue_type="missing_price",
                    date=pd.Timestamp(first_active_date).date().isoformat(),
                    ticker=ticker,
                    message=f"No usable adjusted prices for active holding {ticker}.",
                )
            )
            continue

        coverage_start = max(pd.Timestamp(first_active_date), audit_start)
        if usable[usable.index < coverage_start].empty:
            issues.append(
                PerformanceDataIssue(
                    issue_type="missing_price",
                    date=coverage_start.date().isoformat(),
                    ticker=ticker,
                    message=f"Missing prior price for {ticker} before first calculated return date {coverage_start.date().isoformat()}.",
                )
            )
        if usable[usable.index >= coverage_start].empty:
            issues.append(
                PerformanceDataIssue(
                    issue_type="missing_price",
                    date=coverage_start.date().isoformat(),
                    ticker=ticker,
                    message=f"Missing price observations for {ticker} on or after {coverage_start.date().isoformat()}.",
                )
            )
        if audit_end is not None and usable[usable.index <= audit_end].empty:
            issues.append(
                PerformanceDataIssue(
                    issue_type="missing_price",
                    date=audit_end.date().isoformat(),
                    ticker=ticker,
                    message=f"Missing price observations for {ticker} through requested end date {audit_end.date().isoformat()}.",
                )
            )

        if _holding_currency(holdings, ticker) == "USD":
            start = usable.index.min().date().isoformat()
            end = (audit_end if audit_end is not None else usable.index.max()).date().isoformat()
            try:
                fx = _load_fx_series(db_path, start, end)
            except PerformanceDataError as exc:
                issues.append(
                    PerformanceDataIssue(
                        issue_type="missing_fx",
                        date=start,
                        ticker=ticker,
                        message=str(exc),
                    )
                )
                continue
            missing_fx_dates = usable.loc[(usable.index >= pd.Timestamp(start)) & (usable.index <= pd.Timestamp(end))].index.difference(fx.index)
            if not missing_fx_dates.empty:
                sample = missing_fx_dates[0].date().isoformat()
                issues.append(
                    PerformanceDataIssue(
                        issue_type="missing_fx",
                        date=sample,
                        ticker=ticker,
                        message=f"Missing {FX_PAIR} FX rate for USD-listed ticker {ticker} on {sample}.",
                    )
                )

    return PriceCoverageReport(issues)


def calculate_twr_series(
    db_path: Path | str = DEFAULT_DB_PATH,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Chain seeded and calculated returns into one cumulative CAD TWR series."""
    return calculate_twr_series_with_quality(db_path=db_path, end_date=end_date, strict=True).data


def calculate_twr_series_with_quality(
    db_path: Path | str = DEFAULT_DB_PATH,
    end_date: str | None = None,
    strict: bool = True,
) -> PerformanceCalculationResult:
    """Chain seeded and calculated returns, returning skipped-date issues when requested."""
    seeded = _load_seeded_returns(db_path)
    calculated_result = _try_calculated_returns(db_path, end_date, strict=strict)
    calculated = calculated_result.data
    combined = pd.concat([seeded, calculated], ignore_index=True)
    combined = combined.sort_values("date").reset_index(drop=True)
    combined["cumulative_return"] = (1.0 + combined["daily_return"]).cumprod() - 1.0
    return PerformanceCalculationResult(
        combined[["date", "daily_return", "cumulative_return", "phase"]],
        calculated_result.issues,
    )


def calculate_benchmark_twr_series(
    db_path: Path | str = DEFAULT_DB_PATH,
    end_date: str | None = None,
    benchmark_ticker: str = BENCHMARK_TICKER,
) -> pd.DataFrame:
    """Chain seeded benchmark returns into calculated ^GSPTSE benchmark returns.

    Seeded benchmark returns are monthly points from seeded_returns. Calculated
    benchmark returns remain daily returns from the prices table beginning on
    2025-02-01. Missing seeded benchmark rows fail clearly so benchmark charts
    do not appear complete when the seeded phase is incomplete.
    """
    seeded = _load_seeded_benchmark_returns(db_path)
    calculated = _try_calculated_benchmark_returns(db_path, end_date, benchmark_ticker)
    combined = pd.concat([seeded, calculated], ignore_index=True)
    combined = combined.sort_values("date").reset_index(drop=True)
    combined["cumulative_return"] = (1.0 + combined["benchmark_return"]).cumprod() - 1.0
    return combined[["date", "benchmark_return", "cumulative_return", "phase"]]


def get_period_return(
    period: Period = "YTD",
    db_path: Path | str = DEFAULT_DB_PATH,
    start_date: str | None = None,
    end_date: str | None = None,
) -> float:
    """Return geometrically chained TWR for a named or custom period."""
    series = calculate_twr_series(db_path, end_date=end_date)
    latest = pd.Timestamp(end_date) if end_date else pd.to_datetime(series["date"]).max()
    start = _period_start(period, latest, start_date, db_path)
    dated = series.assign(_date=pd.to_datetime(series["date"]))
    window = dated[(dated["_date"] >= start) & (dated["_date"] <= latest)]
    if window.empty:
        raise PerformanceDataError(
            f"No performance returns available from {start.date().isoformat()} to {latest.date().isoformat()}."
        )
    if period == "1D":
        return float(window.iloc[-1]["daily_return"])
    return float((1.0 + window["daily_return"]).prod() - 1.0)


def get_period_returns(
    db_path: Path | str = DEFAULT_DB_PATH,
    end_date: str | None = None,
) -> dict[str, float]:
    return {
        period: get_period_return(period, db_path=db_path, end_date=end_date)
        for period in ("1D", "MTD", "QTD", "YTD", "since_inception")
    }


def get_benchmark_returns(
    db_path: Path | str = DEFAULT_DB_PATH,
    benchmark_ticker: str = BENCHMARK_TICKER,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Read benchmark daily returns from prices; ^GSPTSE is treated as CAD-denominated."""
    prices = _load_price_series(db_path, benchmark_ticker)
    if prices.empty:
        raise PerformanceDataError(
            f"Missing benchmark prices for {benchmark_ticker}. Run refresh_benchmark_prices first."
        )
    returns = prices["adj_close"].pct_change(fill_method=None).dropna()
    if start_date:
        returns = returns[returns.index >= pd.Timestamp(start_date)]
    if end_date:
        returns = returns[returns.index <= pd.Timestamp(end_date)]
    if returns.empty:
        raise PerformanceDataError(f"No benchmark returns available for {benchmark_ticker}.")
    return pd.DataFrame({"date": returns.index.date.astype(str), "benchmark_return": returns.to_numpy()})


def get_benchmark_returns_with_quality(
    db_path: Path | str = DEFAULT_DB_PATH,
    benchmark_ticker: str = BENCHMARK_TICKER,
    start_date: str | None = None,
    end_date: str | None = None,
    strict: bool = True,
) -> PerformanceCalculationResult:
    try:
        data = get_benchmark_returns(db_path, benchmark_ticker, start_date, end_date)
    except PerformanceDataError as exc:
        if strict:
            raise
        issue = PerformanceDataIssue(
            issue_type="missing_benchmark",
            date=start_date or "",
            ticker=benchmark_ticker,
            message=str(exc),
        )
        return PerformanceCalculationResult(pd.DataFrame(columns=["date", "benchmark_return"]), [issue])
    return PerformanceCalculationResult(data, [])


def get_benchmark_period_return(
    period: Period = "YTD",
    db_path: Path | str = DEFAULT_DB_PATH,
    start_date: str | None = None,
    end_date: str | None = None,
    benchmark_ticker: str = BENCHMARK_TICKER,
) -> float:
    series = calculate_benchmark_twr_series(db_path, end_date=end_date, benchmark_ticker=benchmark_ticker)
    latest = pd.Timestamp(end_date) if end_date else pd.to_datetime(series["date"]).max()
    start = _period_start(period, latest, start_date, db_path)
    dated = series.assign(_date=pd.to_datetime(series["date"]))
    window = dated[(dated["_date"] >= start) & (dated["_date"] <= latest)]
    if window.empty:
        raise PerformanceDataError(
            f"No benchmark returns available from {start.date().isoformat()} to {latest.date().isoformat()}."
        )
    if period == "1D":
        return float(window.iloc[-1]["benchmark_return"])
    return float((1.0 + window["benchmark_return"]).prod() - 1.0)


def calculate_risk_metrics(
    portfolio_returns: pd.DataFrame | None = None,
    benchmark_returns: pd.DataFrame | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
    risk_free_rate: float = 0.0,
) -> RiskMetrics:
    """Calculate daily-return risk metrics using 252 trading days per year.

    The default risk-free rate is 0, which is pragmatic for an MVP and can be
    overridden when a specific cash or T-bill assumption is desired.
    """
    portfolio = portfolio_returns if portfolio_returns is not None else calculate_daily_portfolio_returns(db_path)
    p = _series_from_returns_frame(portfolio, "daily_return")
    annualized_vol = float(p.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(p) > 1 else 0.0
    max_dd = maximum_drawdown(p)
    daily_rf = (1.0 + risk_free_rate) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0
    excess = p - daily_rf
    sharpe = _safe_ratio(excess.mean() * TRADING_DAYS_PER_YEAR, annualized_vol)

    if benchmark_returns is None:
        benchmark_returns = get_benchmark_returns(
            db_path,
            start_date=p.index.min().date().isoformat(),
            end_date=p.index.max().date().isoformat(),
        )
    b = _series_from_returns_frame(benchmark_returns, "benchmark_return")
    aligned = pd.concat([p.rename("portfolio"), b.rename("benchmark")], axis=1, join="inner").dropna()
    if len(aligned) < 2:
        beta = alpha = tracking_error = information_ratio = None
    else:
        bench_var = float(aligned["benchmark"].var(ddof=1))
        beta = None if bench_var == 0 else float(aligned["portfolio"].cov(aligned["benchmark"]) / bench_var)
        alpha = None if beta is None else float(
            (aligned["portfolio"].mean() - daily_rf - beta * (aligned["benchmark"].mean() - daily_rf))
            * TRADING_DAYS_PER_YEAR
        )
        active = aligned["portfolio"] - aligned["benchmark"]
        tracking_error = float(active.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
        information_ratio = _safe_ratio(active.mean() * TRADING_DAYS_PER_YEAR, tracking_error)

    return RiskMetrics(
        annualized_volatility=annualized_vol,
        maximum_drawdown=max_dd,
        sharpe_ratio=sharpe,
        beta=beta,
        alpha=alpha,
        tracking_error=tracking_error,
        information_ratio=information_ratio,
    )


def calculate_rolling_metrics(
    portfolio_returns: pd.DataFrame | None = None,
    benchmark_returns: pd.DataFrame | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
    window: int = TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> pd.DataFrame:
    """Calculate rolling Sharpe and tracking error from daily calculated returns.

    A 252-trading-day window approximates 12 months. If there are not enough
    observations, an empty frame is returned so UI callers can show a clean
    empty state instead of failing.
    """
    portfolio = portfolio_returns if portfolio_returns is not None else calculate_daily_portfolio_returns(db_path)
    p = _series_from_returns_frame(portfolio, "daily_return")
    if len(p) < window:
        return pd.DataFrame(columns=["date", "rolling_sharpe", "rolling_tracking_error"])
    daily_rf = (1.0 + risk_free_rate) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0
    excess = p - daily_rf
    rolling_vol = excess.rolling(window).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
    rolling_return = excess.rolling(window).mean() * TRADING_DAYS_PER_YEAR
    rolling_sharpe = rolling_return / rolling_vol

    result = pd.DataFrame({"rolling_sharpe": rolling_sharpe})
    if benchmark_returns is not None:
        b = _series_from_returns_frame(benchmark_returns, "benchmark_return")
    else:
        try:
            b = _series_from_returns_frame(
                get_benchmark_returns(
                    db_path,
                    start_date=p.index.min().date().isoformat(),
                    end_date=p.index.max().date().isoformat(),
                ),
                "benchmark_return",
            )
        except PerformanceDataError:
            b = pd.Series(dtype=float)
    if not b.empty:
        aligned = pd.concat([p.rename("portfolio"), b.rename("benchmark")], axis=1, join="inner").dropna()
        active = aligned["portfolio"] - aligned["benchmark"]
        tracking_error = active.rolling(window).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
        result = result.join(tracking_error.rename("rolling_tracking_error"), how="left")
    else:
        result["rolling_tracking_error"] = pd.NA
    result = result.dropna(how="all").dropna(subset=["rolling_sharpe"])
    if result.empty:
        return pd.DataFrame(columns=["date", "rolling_sharpe", "rolling_tracking_error"])
    return result.reset_index().rename(columns={"index": "date"})


def calculate_qtd_holding_price_returns(
    db_path: Path | str = DEFAULT_DB_PATH,
    end_date: str | None = None,
    limit: int = 5,
) -> HoldingReturnRankingResult:
    """Rank current holdings by their own QTD CAD adjusted price return.

    This is a security price-return screen only, not contribution or attribution.
    Missing price or FX data excludes that ticker and is returned as a concise
    data-quality issue rather than being filled or treated as zero.
    """
    holdings = _load_current_active_holdings(db_path)
    latest_end = pd.Timestamp(end_date) if end_date else _latest_performance_observation_date(db_path)
    quarter_start = _quarter_start(latest_end)
    rows: list[dict[str, object]] = []
    issues: list[PerformanceDataIssue] = []

    for holding in holdings.itertuples(index=False):
        ticker = str(holding.ticker)
        prices = _load_price_series(db_path, ticker)
        if prices.empty:
            issues.append(_holding_issue("missing_price", latest_end, ticker, "Missing stored prices."))
            continue
        start_row = _price_row_on_or_before(prices, quarter_start - pd.Timedelta(days=1))
        end_row = _price_row_on_or_before(prices, latest_end)
        if start_row is None:
            issues.append(_holding_issue("missing_price", quarter_start, ticker, "Missing start price before quarter start."))
            continue
        if end_row is None:
            issues.append(_holding_issue("missing_price", latest_end, ticker, "Missing end price through latest observation."))
            continue
        start_price_date, start_price = start_row
        end_price_date, end_price = end_row
        try:
            currency = str(holding.currency)
            start_cad = _price_to_cad(db_path, ticker, start_price_date, start_price, currency)
            end_cad = _price_to_cad(db_path, ticker, end_price_date, end_price, currency)
        except PerformanceDataError as exc:
            issues.append(_holding_issue("missing_fx", end_price_date, ticker, str(exc)))
            continue
        rows.append(
            {
                "ticker": ticker,
                "weight": float(holding.weight),
                "qtd_return": float(end_cad / start_cad - 1.0),
                "start_price_date": start_price_date.date().isoformat(),
                "end_price_date": end_price_date.date().isoformat(),
            }
        )

    returns = (
        pd.DataFrame(rows).sort_values("qtd_return", ascending=False).reset_index(drop=True)
        if rows
        else pd.DataFrame(columns=["ticker", "weight", "qtd_return", "start_price_date", "end_price_date"])
    )
    return HoldingReturnRankingResult(
        returns=returns,
        top=returns.head(limit).reset_index(drop=True),
        bottom=returns.tail(limit).sort_values("qtd_return", ascending=True).reset_index(drop=True),
        issues=issues,
    )


def maximum_drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns).cumprod()
    drawdowns = wealth / wealth.cummax() - 1.0
    return float(drawdowns.min()) if not drawdowns.empty else 0.0


def refresh_benchmark_prices(
    db_path: Path | str = DEFAULT_DB_PATH,
    benchmark_ticker: str = BENCHMARK_TICKER,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    """Fetch and store benchmark prices using the existing yfinance download pattern.

    Tests patch the yfinance-facing helper; production use can call this when
    ^GSPTSE rows are missing from prices.
    """
    return refresh_benchmark_prices_with_summary(
        db_path=db_path,
        benchmark_ticker=benchmark_ticker,
        start_date=start_date,
        end_date=end_date,
    ).rows_written


def refresh_benchmark_prices_with_summary(
    db_path: Path | str = DEFAULT_DB_PATH,
    benchmark_ticker: str = BENCHMARK_TICKER,
    start_date: str | None = None,
    end_date: str | None = None,
) -> BenchmarkRefreshResult:
    """Fetch/store CAD benchmark prices and return a concise load summary."""
    init_db(db_path)
    start = start_date or get_since_inception_date(db_path)
    end = end_date or date.today().isoformat()
    frame = _download_price_frame(benchmark_ticker, start, _yfinance_inclusive_end(end))
    if frame.empty:
        raise PerformanceDataError(f"No benchmark data returned for {benchmark_ticker}.")
    rows = _price_rows_from_frame(benchmark_ticker, frame)
    if not rows:
        raise PerformanceDataError(f"No usable benchmark price rows returned for {benchmark_ticker}.")
    with closing(connect(db_path)) as conn:
        with conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO prices (date, ticker, close, adj_close)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
    dates = [row[0] for row in rows]
    return BenchmarkRefreshResult(
        benchmark_ticker=benchmark_ticker,
        rows_written=len(rows),
        min_date=min(dates),
        max_date=max(dates),
    )


def _yfinance_inclusive_end(end_date: str) -> str:
    """Convert an intended inclusive end date to yfinance's exclusive end date."""
    return (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()


def _load_holdings(db_path: Path | str) -> pd.DataFrame:
    with closing(connect(db_path)) as conn:
        frame = pd.read_sql_query(
            "SELECT date, ticker, weight, currency FROM holdings ORDER BY date, ticker",
            conn,
        )
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _active_holdings_in_period(
    holdings: pd.DataFrame,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp | None,
) -> list[tuple[str, pd.Timestamp]]:
    if holdings.empty:
        return []
    eligible = holdings.copy()
    if end_date is not None:
        eligible = eligible[eligible["date"] <= end_date]
    first_active: dict[str, pd.Timestamp] = {}
    for snapshot_date in sorted(eligible["date"].drop_duplicates()):
        snapshot = eligible[eligible["date"] == snapshot_date]
        active = _active_holdings(snapshot)
        for ticker in active["ticker"].astype(str):
            first_active[ticker] = min(first_active.get(ticker, pd.Timestamp(snapshot_date)), pd.Timestamp(snapshot_date))
    return sorted(
        (
            ticker,
            max(first_date, start_date) if first_date < start_date else first_date,
        )
        for ticker, first_date in first_active.items()
    )


def _coverage_error_message(issues: list[PerformanceDataIssue]) -> str:
    if not issues:
        return "Price coverage is incomplete."
    details = "; ".join(issue.message for issue in issues[:5])
    if len(issues) > 5:
        details += f"; and {len(issues) - 5} more issue(s)"
    return "Price coverage is incomplete for active holdings. " + details


def _load_current_active_holdings(db_path: Path | str) -> pd.DataFrame:
    with closing(connect(db_path)) as conn:
        latest = conn.execute("SELECT MAX(date) FROM holdings").fetchone()[0]
        if latest is None:
            raise PerformanceDataError("No holdings rows found; cannot rank holding returns.")
        frame = pd.read_sql_query(
            """
            SELECT date, ticker, weight, currency
            FROM holdings
            WHERE date = ?
            ORDER BY ticker
            """,
            conn,
            params=(latest,),
        )
    if frame.empty:
        raise PerformanceDataError("No current holdings found; cannot rank holding returns.")
    weights = _weights_to_decimal(frame["weight"])
    return frame[weights != 0].reset_index(drop=True)


def _load_seeded_returns(db_path: Path | str) -> pd.DataFrame:
    with closing(connect(db_path)) as conn:
        frame = pd.read_sql_query(
            """
            SELECT date, return_pct
            FROM seeded_returns
            WHERE date <= ?
            ORDER BY date
            """,
            conn,
            params=(SEEDED_END_DATE,),
        )
    if frame.empty:
        raise PerformanceDataError("No seeded_returns rows found for the seeded performance phase.")
    frame["daily_return"] = frame["return_pct"].astype(float) / 100.0
    frame["phase"] = "seeded"
    return frame[["date", "daily_return", "phase"]]


def _load_seeded_benchmark_returns(db_path: Path | str) -> pd.DataFrame:
    with closing(connect(db_path)) as conn:
        frame = pd.read_sql_query(
            """
            SELECT date, benchmark_return_pct
            FROM seeded_returns
            WHERE date <= ?
            ORDER BY date
            """,
            conn,
            params=(SEEDED_END_DATE,),
        )
    if frame.empty:
        raise PerformanceDataError("No seeded_returns rows found for seeded benchmark phase.")
    missing = frame[frame["benchmark_return_pct"].isna()]["date"].tolist()
    if missing:
        raise PerformanceDataError(
            "Missing benchmark_return_pct for seeded benchmark dates: "
            + ", ".join(str(value) for value in missing)
        )
    frame["benchmark_return"] = frame["benchmark_return_pct"].astype(float) / 100.0
    frame["phase"] = "seeded"
    return frame[["date", "benchmark_return", "phase"]]


def _try_calculated_returns(
    db_path: Path | str,
    end_date: str | None,
    strict: bool = True,
) -> PerformanceCalculationResult:
    try:
        return calculate_daily_portfolio_returns_with_quality(db_path, CALCULATED_START_DATE, end_date, strict=strict)
    except PerformanceDataError as exc:
        if "No holdings rows found" in str(exc):
            return PerformanceCalculationResult(pd.DataFrame(columns=["date", "daily_return", "phase"]), [])
        raise


def _try_calculated_benchmark_returns(
    db_path: Path | str,
    end_date: str | None,
    benchmark_ticker: str,
) -> pd.DataFrame:
    try:
        return get_benchmark_returns(
            db_path,
            benchmark_ticker,
            start_date=CALCULATED_START_DATE,
            end_date=end_date,
        ).assign(phase="calculated")
    except PerformanceDataError as exc:
        if "Missing benchmark prices" in str(exc) or "No benchmark returns available" in str(exc):
            return pd.DataFrame(columns=["date", "benchmark_return", "phase"])
        raise


def _load_cad_adjusted_prices(
    db_path: Path | str,
    tickers: list[str],
    currencies: dict[str, str] | None = None,
    strict: bool = True,
    issues: list[PerformanceDataIssue] | None = None,
) -> pd.DataFrame:
    series_by_ticker: dict[str, pd.Series] = {}
    for ticker in tickers:
        prices = _load_price_series(db_path, ticker)
        if prices.empty:
            message = f"Missing prices for held ticker {ticker}."
            if strict:
                raise PerformanceDataError(message)
            if issues is not None:
                issues.append(
                    PerformanceDataIssue(
                        issue_type="missing_price",
                        date="",
                        ticker=ticker,
                        message=message,
                    )
                )
            series_by_ticker[ticker] = pd.Series(dtype=float, name=ticker)
            continue
        adjusted = prices["adj_close"].astype(float)
        if (currencies or {}).get(ticker, "CAD") == "USD":
            start = adjusted.index.min().date().isoformat()
            end = adjusted.index.max().date().isoformat()
            try:
                fx = _load_fx_series(db_path, start, end)
            except PerformanceDataError:
                if strict:
                    raise
                fx = pd.Series(dtype=float)
            fx = fx.reindex(adjusted.index)
            missing_dates = adjusted.index[fx.isna()]
            if not missing_dates.empty:
                sample = missing_dates[0].date().isoformat()
                message = f"Missing {FX_PAIR} FX rate for USD-listed ticker {ticker} on {sample}."
                if strict:
                    raise PerformanceDataError(message)
                if issues is not None:
                    for missing_date in missing_dates:
                        issues.append(
                            PerformanceDataIssue(
                                issue_type="missing_fx",
                                date=missing_date.date().isoformat(),
                                ticker=ticker,
                                message=f"Missing {FX_PAIR} FX rate for USD-listed ticker {ticker} on {missing_date.date().isoformat()}.",
                            )
                        )
            adjusted = adjusted / fx
        series_by_ticker[ticker] = adjusted.rename(ticker)
    return pd.concat(series_by_ticker.values(), axis=1).sort_index()


def _load_price_series(db_path: Path | str, ticker: str) -> pd.DataFrame:
    with closing(connect(db_path)) as conn:
        frame = pd.read_sql_query(
            """
            SELECT date, close, adj_close
            FROM prices
            WHERE ticker = ?
            ORDER BY date
            """,
            conn,
            params=(ticker,),
        )
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date")


def _price_row_on_or_before(prices: pd.DataFrame, target_date: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    eligible = prices[prices.index <= target_date].sort_index()
    if eligible.empty:
        return None
    row = eligible.iloc[-1]
    price = row["adj_close"] if not pd.isna(row["adj_close"]) else row["close"]
    if pd.isna(price):
        return None
    return (pd.Timestamp(eligible.index[-1]), float(price))


def _price_to_cad(
    db_path: Path | str,
    ticker: str,
    price_date: pd.Timestamp,
    price: float,
    currency: str,
) -> float:
    if currency != "USD":
        return float(price)
    fx_rate = _load_fx_rate_on_date(db_path, price_date)
    return float(price) / fx_rate


def _load_fx_rate_on_date(db_path: Path | str, fx_date: pd.Timestamp) -> float:
    with closing(connect(db_path)) as conn:
        value = conn.execute(
            """
            SELECT rate
            FROM fx_rates
            WHERE pair = ? AND date = ?
            """,
            (FX_PAIR, fx_date.date().isoformat()),
        ).fetchone()
    if value is None:
        raise PerformanceDataError(f"Missing {FX_PAIR} FX rate on {fx_date.date().isoformat()}.")
    return float(value[0])


def _latest_performance_observation_date(db_path: Path | str) -> pd.Timestamp:
    result = calculate_twr_series_with_quality(db_path, strict=False)
    if result.data.empty:
        raise PerformanceDataError("No performance observations available.")
    return pd.to_datetime(result.data["date"]).max()


def _quarter_start(day: pd.Timestamp) -> pd.Timestamp:
    quarter_month = ((day.month - 1) // 3) * 3 + 1
    return pd.Timestamp(day.year, quarter_month, 1)


def _holding_issue(issue_type: str, issue_date: pd.Timestamp, ticker: str, message: str) -> PerformanceDataIssue:
    return PerformanceDataIssue(
        issue_type=issue_type,
        date=pd.Timestamp(issue_date).date().isoformat(),
        ticker=ticker,
        message=f"{ticker}: {message}",
    )


def _calculate_price_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return pd.concat(
        [
            prices[column].dropna().pct_change(fill_method=None).rename(column)
            for column in prices.columns
        ],
        axis=1,
    ).sort_index()


def _load_fx_series(db_path: Path | str, start_date: str, end_date: str) -> pd.Series:
    with closing(connect(db_path)) as conn:
        frame = pd.read_sql_query(
            """
            SELECT date, rate
            FROM fx_rates
            WHERE pair = ? AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            conn,
            params=(FX_PAIR, start_date, end_date),
        )
    if frame.empty:
        raise PerformanceDataError(f"Missing {FX_PAIR} FX rates from {start_date} to {end_date}.")
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date")["rate"].astype(float)


def _latest_holdings_snapshot(
    holdings: pd.DataFrame,
    day: pd.Timestamp,
    effective_next_day: bool = False,
) -> pd.DataFrame:
    eligible = holdings[holdings["date"] < day] if effective_next_day else holdings[holdings["date"] <= day]
    if eligible.empty:
        return eligible
    latest_date = eligible["date"].max()
    return eligible[eligible["date"] == latest_date].reset_index(drop=True)


def _active_holdings(snapshot: pd.DataFrame) -> pd.DataFrame:
    if snapshot.empty:
        return snapshot
    weights = _weights_to_decimal(snapshot["weight"])
    return snapshot[weights != 0].reset_index(drop=True)


def _snapshot_weights(snapshot: pd.DataFrame) -> dict[str, float]:
    active = _active_holdings(snapshot).copy()
    if active.empty:
        return {}
    weights = _weights_to_decimal(active["weight"])
    if (weights < 0).any():
        raise PerformanceDataError("Negative holdings weights are not supported by the long-only performance model.")
    if float(weights.sum()) > 1.000001:
        snapshot_date = pd.Timestamp(active["date"].iloc[0]).date().isoformat()
        raise PerformanceDataError(f"Holdings weights exceed 100% for snapshot {snapshot_date}.")
    return {
        str(ticker): float(weight)
        for ticker, weight in zip(active["ticker"], weights)
        if float(weight) != 0.0
    }


def _holding_currency(holdings: pd.DataFrame, ticker: str) -> str:
    values = holdings.loc[holdings["ticker"] == ticker, "currency"].dropna().astype(str).str.upper().unique()
    if len(values) != 1 or values[0] not in {"CAD", "USD"}:
        raise PerformanceDataError(f"Holding currency is missing or inconsistent for {ticker}.")
    return str(values[0])


def _weights_to_decimal(weights: pd.Series) -> pd.Series:
    numeric = weights.astype(float)
    if numeric.abs().max() > 1.0 or numeric.abs().sum() > 1.5:
        return numeric / 100.0
    return numeric


def _period_start(period: Period, latest: pd.Timestamp, start_date: str | None, db_path: Path | str) -> pd.Timestamp:
    if period == "custom":
        if not start_date:
            raise PerformanceDataError("start_date is required when period='custom'.")
        return pd.Timestamp(start_date)
    if period == "1D":
        return latest
    if period == "MTD":
        return pd.Timestamp(latest.year, latest.month, 1)
    if period == "QTD":
        quarter_month = ((latest.month - 1) // 3) * 3 + 1
        return pd.Timestamp(latest.year, quarter_month, 1)
    if period == "YTD":
        return pd.Timestamp(latest.year, 1, 1)
    if period == "since_inception":
        return pd.Timestamp(get_since_inception_date(db_path))
    raise PerformanceDataError(f"Unsupported period: {period}")


def _series_from_returns_frame(frame: pd.DataFrame, column: str) -> pd.Series:
    dated = frame.copy()
    dated["date"] = pd.to_datetime(dated["date"])
    return dated.set_index("date")[column].astype(float).sort_index()


def _safe_ratio(numerator: float, denominator: float | None) -> float | None:
    if denominator is None or denominator == 0 or math.isnan(denominator):
        return None
    return float(numerator / denominator)


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate SMA performance metrics.")
    parser.add_argument("--db", "--db-path", dest="db_path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--period", choices=["1D", "MTD", "QTD", "YTD", "since_inception", "custom"], default="YTD")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args()
    value = get_period_return(args.period, args.db_path, args.start_date, args.end_date)
    print(f"{args.period} TWR (CAD): {value:.6f}")


def benchmark_refresh_main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and store benchmark prices for calculated performance.")
    parser.add_argument("--db", "--db-path", dest="db_path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--benchmark", default=BENCHMARK_TICKER)
    parser.add_argument("--start-date", default=CALCULATED_START_DATE)
    parser.add_argument("--end-date")
    args = parser.parse_args()
    result = refresh_benchmark_prices_with_summary(
        db_path=args.db_path,
        benchmark_ticker=args.benchmark,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print(f"benchmark={result.benchmark_ticker}")
    print(f"rows_written={result.rows_written}")
    print(f"min_date={result.min_date}")
    print(f"max_date={result.max_date}")


if __name__ == "__main__":
    main()
