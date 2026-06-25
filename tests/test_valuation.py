from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from sma_dashboard.db import init_db
from sma_dashboard.ingestion import FX_PAIR
from sma_dashboard.valuation import (
    ValuationDataError,
    calculate_weighted_average_valuation,
    get_holding_valuations,
    get_portfolio_valuation,
    normalize_dividend_yield,
)


TEST_DB = Path("data/db/test_valuation.sqlite")


class ValuationTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)

    def test_latest_holdings_snapshot_selection_and_per_holding_valuation(self) -> None:
        _load_holdings_fixture()
        info = {
            "RY.TO": {
                "trailingPE": 12.0,
                "forwardPE": 11.0,
                "priceToBook": 1.8,
                "enterpriseToEbitda": 9.0,
                "dividendYield": 0.04,
                "marketCap": 100_000_000.0,
            },
            "AAPL": {"trailingPE": 30.0, "marketCap": 2_000_000_000.0},
        }

        with patch("sma_dashboard.valuation._fetch_ticker_info", side_effect=lambda ticker: info[ticker]):
            valuations = get_holding_valuations(TEST_DB)

        self.assertEqual(valuations["ticker"].tolist(), ["AAPL", "RY.TO"])
        self.assertEqual(valuations["weight"].tolist(), [25.0, 75.0])
        self.assertAlmostEqual(
            valuations.loc[valuations["ticker"] == "AAPL", "market_cap"].iloc[0],
            2_000_000_000.0 / 0.80,
        )
        self.assertAlmostEqual(
            valuations.loc[valuations["ticker"] == "RY.TO", "market_cap"].iloc[0],
            100_000_000.0,
        )
        self.assertAlmostEqual(
            valuations.loc[valuations["ticker"] == "RY.TO", "dividend_yield"].iloc[0],
            0.04,
        )

    def test_dividend_yield_percentage_style_normalizes_with_implied_yield(self) -> None:
        normalized = normalize_dividend_yield(
            {
                "dividendYield": 0.92,
                "dividendRate": 0.86,
                "currentPrice": 93.5,
            }
        )

        self.assertAlmostEqual(normalized, 0.0092, places=4)

    def test_dividend_yield_decimal_style_normalizes_with_implied_yield(self) -> None:
        normalized = normalize_dividend_yield(
            {
                "dividendYield": 0.0092,
                "dividendRate": 0.86,
                "currentPrice": 93.5,
            }
        )

        self.assertAlmostEqual(normalized, 0.0092, places=4)

    def test_dividend_yield_decimal_fallback_remains_decimal(self) -> None:
        self.assertAlmostEqual(normalize_dividend_yield({"dividendYield": 0.035}), 0.035)

    def test_dividend_yield_percentage_style_fallback_is_scaled_down(self) -> None:
        self.assertAlmostEqual(normalize_dividend_yield({"dividendYield": 0.92}), 0.0092)

    def test_missing_dividend_fields_return_none(self) -> None:
        self.assertIsNone(normalize_dividend_yield({}))

    def test_missing_yfinance_fields_are_none_not_crashes(self) -> None:
        _load_holdings_fixture()

        with patch("sma_dashboard.valuation._fetch_ticker_info", return_value={}):
            valuations = get_holding_valuations(TEST_DB)

        self.assertTrue(valuations["pe_trailing"].isna().all())
        self.assertTrue(valuations["pe_forward"].isna().all())
        self.assertTrue(valuations["market_cap"].isna().all())

    def test_portfolio_weighted_average_excludes_missing_values(self) -> None:
        _load_holdings_fixture()
        info = {
            "RY.TO": {
                "trailingPE": 10.0,
                "forwardPE": 9.0,
                "dividendYield": 0.92,
                "dividendRate": 0.86,
                "currentPrice": 93.5,
                "marketCap": 100.0,
            },
            "AAPL": {
                "trailingPE": None,
                "forwardPE": 30.0,
                "dividendYield": 0.035,
                "marketCap": 80.0,
            },
        }

        with patch("sma_dashboard.valuation._fetch_ticker_info", side_effect=lambda ticker: info[ticker]):
            portfolio = get_portfolio_valuation(TEST_DB)

        averages = portfolio.weighted_averages
        self.assertAlmostEqual(averages["pe_trailing"], 10.0)
        self.assertAlmostEqual(averages["pe_forward"], 0.75 * 9.0 + 0.25 * 30.0)
        self.assertAlmostEqual(averages["dividend_yield"], 0.75 * (0.92 / 100.0) + 0.25 * 0.035)
        self.assertAlmostEqual(
            averages["market_cap"],
            (0.75 * 100.0 + 0.25 * (80.0 / 0.80)) / (0.75 + 0.25),
        )

    def test_weighted_average_returns_none_when_metric_missing_for_all_holdings(self) -> None:
        _load_holdings_fixture()

        with patch("sma_dashboard.valuation._fetch_ticker_info", return_value={}):
            valuations = get_holding_valuations(TEST_DB)

        self.assertIsNone(calculate_weighted_average_valuation(valuations)["pb"])

    def test_missing_fx_for_usd_market_cap_fails_clearly(self) -> None:
        init_db(TEST_DB)
        _insert_holding("2025-02-01", "AAPL", 100.0)

        with patch(
            "sma_dashboard.valuation._fetch_ticker_info",
            return_value={"marketCap": 2_000_000_000.0},
        ):
            with self.assertRaisesRegex(ValuationDataError, "CADUSD=X"):
                get_holding_valuations(TEST_DB)


def _load_holdings_fixture() -> None:
    init_db(TEST_DB)
    _insert_holding("2025-01-15", "OLD.TO", 100.0)
    _insert_holding("2025-02-01", "RY.TO", 75.0)
    _insert_holding("2025-02-01", "AAPL", 25.0)
    _insert_fx("2025-02-02", 0.80)


def _insert_holding(snapshot_date: str, ticker: str, weight: float) -> None:
    _execute(
        """
        INSERT INTO holdings (date, ticker, weight, shares, cost_basis)
        VALUES (?, ?, ?, NULL, NULL)
        """,
        (snapshot_date, ticker, weight),
    )


def _insert_fx(fx_date: str, rate: float) -> None:
    _execute(
        """
        INSERT INTO fx_rates (date, pair, rate)
        VALUES (?, ?, ?)
        """,
        (fx_date, FX_PAIR, rate),
    )


def _execute(sql: str, params: tuple[object, ...]) -> None:
    conn = sqlite3.connect(TEST_DB)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
