from __future__ import annotations

import argparse
import math
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from sma_dashboard.db import DEFAULT_DB_PATH, connect
from sma_dashboard.holdings import get_latest_holdings_snapshot
from sma_dashboard.ingestion import FX_PAIR, is_usd_listed


INFO_FIELD_MAP = {
    "pe_trailing": "trailingPE",
    "pe_forward": "forwardPE",
    "pb": "priceToBook",
    "ev_ebitda": "enterpriseToEbitda",
    "dividend_yield": "dividendYield",
    "market_cap": "marketCap",
}
RATIO_METRICS = ("pe_trailing", "pe_forward", "pb", "ev_ebitda", "dividend_yield")
WEIGHTED_METRICS = (*RATIO_METRICS, "market_cap")


class ValuationDataError(ValueError):
    """Raised when M3 valuation inputs cannot be represented safely."""


@dataclass(frozen=True)
class PortfolioValuation:
    holdings: pd.DataFrame
    weighted_averages: dict[str, float | None]


def get_holding_valuations(
    db_path: Path | str = DEFAULT_DB_PATH,
    as_of_date: str | None = None,
) -> pd.DataFrame:
    """Fetch per-holding yfinance valuation data for the latest holdings snapshot.

    Ratio metrics are returned as reported by yfinance. Market cap is returned
    as CAD in the `market_cap` field; USD-listed tickers are converted using
    the latest stored CADUSD=X rate, where CADUSD=X is USD per 1 CAD.
    """
    holdings = get_latest_holdings_snapshot(db_path, as_of_date)
    fx_rate = _latest_cadusd_rate(db_path)
    rows: list[dict[str, Any]] = []
    for holding in holdings.itertuples(index=False):
        info = _fetch_ticker_info(holding.ticker) or {}
        row: dict[str, Any] = {
            "date": holding.date,
            "ticker": holding.ticker,
            "weight": float(holding.weight),
            "weight_decimal": float(holding.weight_decimal),
        }
        for metric, source_field in INFO_FIELD_MAP.items():
            if metric == "dividend_yield":
                value = normalize_dividend_yield(info)
            else:
                value = _clean_number(info.get(source_field))
            if metric == "market_cap":
                value = _market_cap_to_cad(holding.ticker, value, fx_rate)
            row[metric] = value
        rows.append(row)
    return pd.DataFrame(rows)


def get_portfolio_valuation(
    db_path: Path | str = DEFAULT_DB_PATH,
    as_of_date: str | None = None,
) -> PortfolioValuation:
    holdings = get_holding_valuations(db_path, as_of_date)
    return PortfolioValuation(
        holdings=holdings,
        weighted_averages=calculate_weighted_average_valuation(holdings),
    )


def calculate_weighted_average_valuation(valuations: pd.DataFrame) -> dict[str, float | None]:
    """Calculate portfolio-weighted averages, excluding missing values per metric.

    Ratios are weighted by current portfolio weight. If a holding is missing a
    metric, its weight is excluded from that metric's denominator rather than
    treating the missing value as zero.
    """
    averages: dict[str, float | None] = {}
    for metric in WEIGHTED_METRICS:
        valid = valuations[["weight_decimal", metric]].dropna()
        if valid.empty or valid["weight_decimal"].sum() == 0:
            averages[metric] = None
            continue
        averages[metric] = float(
            (valid["weight_decimal"] * valid[metric]).sum() / valid["weight_decimal"].sum()
        )
    return averages


def _fetch_ticker_info(ticker: str) -> dict[str, Any]:
    import yfinance as yf

    return yf.Ticker(ticker).info or {}


def normalize_dividend_yield(info: dict[str, Any]) -> float | None:
    """Return dividend yield as a decimal yield.

    yfinance usually reports dividendYield as a decimal, but some tickers can
    appear as percentage-style values. When dividendRate and price are
    available, compare both raw dividendYield and dividendYield / 100 against
    the implied dividendRate / price. Without that cross-check, keep plausible
    decimal yields and divide values that look percentage-style.
    """
    raw_yield = _clean_number(info.get("dividendYield"))
    if raw_yield is None or raw_yield < 0:
        return None

    dividend_rate = _clean_number(info.get("dividendRate"))
    price = _clean_number(info.get("currentPrice"))
    if price is None:
        price = _clean_number(info.get("regularMarketPrice"))
    if dividend_rate is not None and dividend_rate >= 0 and price is not None and price > 0:
        implied_yield = dividend_rate / price
        if _close_yield(raw_yield, implied_yield):
            return raw_yield
        scaled_yield = raw_yield / 100.0
        if _close_yield(scaled_yield, implied_yield):
            return scaled_yield

    return raw_yield / 100.0 if raw_yield > 0.25 else raw_yield


def _close_yield(candidate: float, expected: float) -> bool:
    return math.isclose(candidate, expected, rel_tol=0.25, abs_tol=0.002)


def _latest_cadusd_rate(db_path: Path | str) -> float | None:
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT rate
            FROM fx_rates
            WHERE pair = ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (FX_PAIR,),
        ).fetchone()
    return None if row is None else float(row[0])


def _market_cap_to_cad(ticker: str, market_cap: float | None, cadusd_rate: float | None) -> float | None:
    if market_cap is None:
        return None
    if not is_usd_listed(ticker):
        return market_cap
    if cadusd_rate is None:
        raise ValuationDataError(
            f"Missing {FX_PAIR} FX rate required to convert USD market cap for {ticker} to CAD."
        )
    return market_cap / cadusd_rate


def _clean_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch SMA holding valuation metrics.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--as-of-date")
    parser.add_argument("--weighted-average", action="store_true")
    args = parser.parse_args()
    valuation = get_portfolio_valuation(args.db_path, args.as_of_date)
    if args.weighted_average:
        print(valuation.weighted_averages)
    else:
        print(valuation.holdings.to_string(index=False))


if __name__ == "__main__":
    main()
