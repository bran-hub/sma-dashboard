from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from sma_dashboard.db import init_db
from sma_dashboard.performance import (
    BENCHMARK_TICKER,
    FX_PAIR,
    PerformanceDataError,
    audit_price_coverage,
    calculate_daily_portfolio_returns,
    calculate_daily_portfolio_returns_with_quality,
    calculate_benchmark_twr_series,
    calculate_qtd_holding_price_returns,
    calculate_risk_metrics,
    calculate_twr_series,
    calculate_twr_series_with_quality,
    get_benchmark_returns,
    get_benchmark_returns_with_quality,
    get_benchmark_period_return,
    get_period_return,
    get_period_returns,
    get_since_inception_date,
    maximum_drawdown,
    refresh_benchmark_prices_with_summary,
)


TEST_DB = Path("data/db/test_performance.sqlite")


class PerformanceTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)

    def test_cad_listed_holding_return_calculation(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 100.0)
        _insert_price("2025-01-31", "RY.TO", 100.0)
        _insert_price("2025-02-03", "RY.TO", 110.0)

        returns = calculate_daily_portfolio_returns(TEST_DB)

        self.assertEqual(returns.iloc[0]["date"], "2025-02-03")
        self.assertAlmostEqual(returns.iloc[0]["daily_return"], 0.10)

    def test_usd_listed_holding_is_converted_to_cad_using_fx_rates(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "AAPL", 100.0)
        _insert_price("2025-01-31", "AAPL", 100.0)
        _insert_price("2025-02-03", "AAPL", 110.0)
        _insert_fx("2025-01-31", 0.50)
        _insert_fx("2025-02-03", 0.55)

        returns = calculate_daily_portfolio_returns(TEST_DB)

        self.assertAlmostEqual(returns.iloc[0]["daily_return"], 0.0)

    def test_flat_usd_stock_price_reflects_cadusd_fx_move(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "AAPL", 100.0)
        _insert_price("2025-01-31", "AAPL", 100.0)
        _insert_price("2025-02-03", "AAPL", 100.0)
        _insert_fx("2025-01-31", 0.50)
        _insert_fx("2025-02-03", 0.40)

        returns = calculate_daily_portfolio_returns(TEST_DB)

        # CADUSD=X is USD per 1 CAD. A lower rate means CAD weakened, so a
        # flat USD price is worth more CAD: 100 / 0.40 vs 100 / 0.50.
        self.assertAlmostEqual(returns.iloc[0]["daily_return"], 0.25)

    def test_portfolio_daily_return_uses_weighted_holdings_snapshot(self) -> None:
        _load_standard_fixture()

        returns = calculate_daily_portfolio_returns(TEST_DB)

        self.assertAlmostEqual(returns.iloc[0]["daily_return"], 0.10)
        self.assertAlmostEqual(returns.iloc[1]["daily_return"], -0.0963636364)
        self.assertAlmostEqual(returns.iloc[2]["daily_return"], 0.10)

    def test_weights_drift_between_model_snapshots(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "AAA.TO", 50.0)
        _insert_holding("2025-02-01", "BBB.TO", 50.0)
        for row in [
            ("2025-01-31", "AAA.TO", 100.0),
            ("2025-02-03", "AAA.TO", 200.0),
            ("2025-02-04", "AAA.TO", 200.0),
            ("2025-01-31", "BBB.TO", 100.0),
            ("2025-02-03", "BBB.TO", 100.0),
            ("2025-02-04", "BBB.TO", 130.0),
        ]:
            _insert_price(*row)

        returns = calculate_daily_portfolio_returns(TEST_DB)

        self.assertAlmostEqual(returns.iloc[0]["daily_return"], 0.50)
        # AAA drifts to 2/3 of invested value after day one, leaving BBB at 1/3.
        self.assertAlmostEqual(returns.iloc[1]["daily_return"], 0.10)

    def test_new_snapshot_becomes_effective_next_trading_day(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "AAA.TO", 100.0)
        _insert_holding("2025-02-01", "BBB.TO", 0.0)
        _insert_holding("2025-02-03", "AAA.TO", 0.0)
        _insert_holding("2025-02-03", "BBB.TO", 100.0)
        for row in [
            ("2025-01-31", "AAA.TO", 100.0),
            ("2025-02-03", "AAA.TO", 110.0),
            ("2025-02-04", "AAA.TO", 110.0),
            ("2025-01-31", "BBB.TO", 100.0),
            ("2025-02-03", "BBB.TO", 100.0),
            ("2025-02-04", "BBB.TO", 120.0),
        ]:
            _insert_price(*row)

        returns = calculate_daily_portfolio_returns(TEST_DB)

        self.assertAlmostEqual(returns.iloc[0]["daily_return"], 0.10)
        self.assertAlmostEqual(returns.iloc[1]["daily_return"], 0.20)

    def test_twr_cumulative_chains_seeded_and_calculated_phases(self) -> None:
        _load_standard_fixture()

        series = calculate_twr_series(TEST_DB)

        self.assertEqual(series.iloc[0]["date"], "2025-01-31")
        self.assertEqual(series.iloc[0]["phase"], "seeded")
        self.assertEqual(series.iloc[1]["phase"], "calculated")
        self.assertAlmostEqual(series.iloc[0]["cumulative_return"], 0.02)
        self.assertAlmostEqual(series.iloc[1]["cumulative_return"], 1.02 * 1.10 - 1.0)

    def test_seeded_benchmark_cumulative_chaining_works(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2024-12-31", 1.0, benchmark_return_pct=0.5)
        _insert_seeded_return("2025-01-31", 2.0, benchmark_return_pct=1.0)

        series = calculate_benchmark_twr_series(TEST_DB)

        self.assertEqual(series["date"].tolist(), ["2024-12-31", "2025-01-31"])
        self.assertEqual(series["phase"].tolist(), ["seeded", "seeded"])
        self.assertAlmostEqual(series.iloc[-1]["cumulative_return"], (1.005 * 1.01) - 1.0)

    def test_calculated_benchmark_returns_chain_after_seeded_benchmark_phase(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 2.0, benchmark_return_pct=1.0)
        _insert_price("2025-01-31", BENCHMARK_TICKER, 100.0)
        _insert_price("2025-02-03", BENCHMARK_TICKER, 110.0)

        series = calculate_benchmark_twr_series(TEST_DB, end_date="2025-02-03")

        self.assertEqual(series["phase"].tolist(), ["seeded", "calculated"])
        self.assertAlmostEqual(series.iloc[-1]["cumulative_return"], (1.01 * 1.10) - 1.0)
        self.assertAlmostEqual(
            get_benchmark_period_return("since_inception", TEST_DB, end_date="2025-02-03"),
            (1.01 * 1.10) - 1.0,
        )

    def test_missing_benchmark_prices_fail_clearly(self) -> None:
        init_db(TEST_DB)

        with self.assertRaisesRegex(PerformanceDataError, "Missing benchmark prices"):
            get_benchmark_returns(TEST_DB)

        result = get_benchmark_returns_with_quality(TEST_DB, strict=False)
        self.assertTrue(result.data.empty)
        self.assertEqual(result.issues[0].issue_type, "missing_benchmark")

    def test_benchmark_returns_are_not_fx_converted(self) -> None:
        init_db(TEST_DB)
        _insert_price("2025-01-31", BENCHMARK_TICKER, 100.0)
        _insert_price("2025-02-03", BENCHMARK_TICKER, 110.0)
        _insert_fx("2025-01-31", 0.50)
        _insert_fx("2025-02-03", 0.25)

        returns = get_benchmark_returns(TEST_DB, start_date="2025-02-01", end_date="2025-02-03")

        self.assertAlmostEqual(returns.iloc[0]["benchmark_return"], 0.10)

    def test_refresh_benchmark_prices_stores_price_rows_without_fx(self) -> None:
        init_db(TEST_DB)
        frame = pd.DataFrame(
            {
                "Close": [100.0, 110.0],
                "Adj Close": [100.0, 110.0],
            },
            index=pd.to_datetime(["2025-02-03", "2025-02-04"]),
        )

        with patch("sma_dashboard.performance._download_price_frame", return_value=frame) as download:
            result = refresh_benchmark_prices_with_summary(
                TEST_DB,
                benchmark_ticker=BENCHMARK_TICKER,
                start_date="2025-02-01",
                end_date="2025-02-05",
            )

        download.assert_called_once_with(BENCHMARK_TICKER, "2025-02-01", "2025-02-06")
        self.assertEqual(result.benchmark_ticker, BENCHMARK_TICKER)
        self.assertEqual(result.rows_written, 2)
        self.assertEqual(result.min_date, "2025-02-03")
        self.assertEqual(result.max_date, "2025-02-04")
        conn = sqlite3.connect(TEST_DB)
        try:
            price_count = conn.execute(
                "SELECT COUNT(*) FROM prices WHERE ticker = ?",
                (BENCHMARK_TICKER,),
            ).fetchone()[0]
            fx_count = conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(price_count, 2)
        self.assertEqual(fx_count, 0)

    def test_missing_benchmark_return_does_not_break_sma_seeded_returns(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 2.0)

        series = calculate_twr_series(TEST_DB)

        self.assertEqual(series.iloc[0]["date"], "2025-01-31")
        self.assertAlmostEqual(series.iloc[0]["cumulative_return"], 0.02)

    def test_missing_seeded_benchmark_return_fails_for_seeded_benchmark_comparison(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 2.0)

        with self.assertRaisesRegex(PerformanceDataError, "benchmark_return_pct"):
            calculate_benchmark_twr_series(TEST_DB)

    def test_since_inception_date_is_read_from_seeded_returns(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2024-12-31", 1.0)
        _insert_seeded_return("2025-01-31", 2.0)

        self.assertEqual(get_since_inception_date(TEST_DB), "2024-12-31")

    def test_period_returns(self) -> None:
        _load_standard_fixture()

        daily = [0.10, -0.0963636364, 0.10]
        feb_return = _chain(daily)
        since_inception = _chain([0.02, *daily])

        self.assertAlmostEqual(get_period_return("1D", TEST_DB, end_date="2025-02-05"), 0.10)
        self.assertAlmostEqual(get_period_return("MTD", TEST_DB, end_date="2025-02-05"), feb_return)
        self.assertAlmostEqual(get_period_return("QTD", TEST_DB, end_date="2025-02-05"), since_inception)
        self.assertAlmostEqual(get_period_return("YTD", TEST_DB, end_date="2025-02-05"), since_inception)
        self.assertAlmostEqual(
            get_period_return("since_inception", TEST_DB, end_date="2025-02-05"),
            since_inception,
        )
        self.assertAlmostEqual(
            get_period_return("custom", TEST_DB, start_date="2025-02-03", end_date="2025-02-04"),
            _chain(daily[:2]),
        )

        all_periods = get_period_returns(TEST_DB, end_date="2025-02-05")
        self.assertEqual(set(all_periods), {"1D", "MTD", "QTD", "YTD", "since_inception"})

    def test_maximum_drawdown(self) -> None:
        returns = pd.Series([0.10, -0.0963636364, 0.10])

        self.assertAlmostEqual(maximum_drawdown(returns), -0.0963636364)

    def test_benchmark_relative_metrics(self) -> None:
        _load_standard_fixture(include_benchmark=True)
        portfolio = calculate_daily_portfolio_returns(TEST_DB)
        benchmark = get_benchmark_returns(TEST_DB, end_date="2025-02-05")

        metrics = calculate_risk_metrics(portfolio, benchmark)

        self.assertGreater(metrics.annualized_volatility, 0.0)
        self.assertAlmostEqual(metrics.maximum_drawdown, -0.0963636364)
        self.assertIsNotNone(metrics.sharpe_ratio)
        self.assertIsNotNone(metrics.beta)
        self.assertIsNotNone(metrics.alpha)
        self.assertIsNotNone(metrics.tracking_error)
        self.assertIsNotNone(metrics.information_ratio)

    def test_missing_fx_data_for_usd_holding_fails_clearly(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "AAPL", 100.0)
        _insert_price("2025-01-31", "AAPL", 100.0)
        _insert_price("2025-02-03", "AAPL", 110.0)

        with self.assertRaisesRegex(PerformanceDataError, "CADUSD=X"):
            calculate_daily_portfolio_returns(TEST_DB)

    def test_price_coverage_audit_detects_active_holding_without_prices(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 50.0)
        _insert_holding("2025-02-01", "SHOP.TO", 50.0)
        _insert_price("2025-01-31", "RY.TO", 100.0)
        _insert_price("2025-02-03", "RY.TO", 110.0)

        report = audit_price_coverage(TEST_DB)

        self.assertFalse(report.is_complete)
        self.assertTrue(any(issue.issue_type == "missing_price" and issue.ticker == "SHOP.TO" for issue in report.issues))

    def test_price_coverage_audit_detects_missing_prior_price(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 100.0)
        _insert_price("2025-02-03", "RY.TO", 110.0)

        report = audit_price_coverage(TEST_DB)

        self.assertFalse(report.is_complete)
        self.assertTrue(any("prior price" in issue.message for issue in report.issues))

    def test_price_coverage_audit_detects_usd_holding_missing_fx(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "AAPL", 100.0)
        _insert_price("2025-01-31", "AAPL", 100.0)
        _insert_price("2025-02-03", "AAPL", 110.0)

        report = audit_price_coverage(TEST_DB)

        self.assertFalse(report.is_complete)
        self.assertTrue(any(issue.issue_type == "missing_fx" and issue.ticker == "AAPL" for issue in report.issues))

    def test_five_day_chart_range_uses_observations_not_calendar_days(self) -> None:
        from sma_dashboard.dashboard_support import filter_performance_window

        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2025-02-03", "2025-02-04", "2025-02-05", "2025-02-06", "2025-02-07", "2025-02-10"]
                ),
                "daily_return": [0.01, 0.01, 0.01, 0.01, 0.01, 0.01],
            }
        )

        filtered = filter_performance_window(frame, "5D")

        self.assertEqual(
            filtered["date"].dt.strftime("%Y-%m-%d").tolist(),
            ["2025-02-04", "2025-02-05", "2025-02-06", "2025-02-07", "2025-02-10"],
        )

    def test_weekends_do_not_create_artificial_portfolio_observations(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 100.0)
        _insert_price("2025-01-31", "RY.TO", 99.0)
        _insert_price("2025-02-07", "RY.TO", 100.0)
        _insert_price("2025-02-10", "RY.TO", 101.0)

        returns = calculate_daily_portfolio_returns(TEST_DB)

        self.assertEqual(returns["date"].tolist(), ["2025-02-07", "2025-02-10"])

    def test_missing_benchmark_date_does_not_create_fake_benchmark_return(self) -> None:
        init_db(TEST_DB)
        _insert_price("2025-02-03", BENCHMARK_TICKER, 100.0)
        _insert_price("2025-02-05", BENCHMARK_TICKER, 105.0)

        returns = get_benchmark_returns(TEST_DB, start_date="2025-02-01", end_date="2025-02-05")

        self.assertEqual(returns["date"].tolist(), ["2025-02-05"])
        self.assertAlmostEqual(returns.iloc[0]["benchmark_return"], 0.05)

    def test_missing_price_for_active_cad_holding_is_detected_in_strict_mode(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 50.0)
        _insert_holding("2025-02-01", "SHOP.TO", 50.0)
        _insert_price("2025-01-31", "RY.TO", 100.0)
        _insert_price("2025-02-03", "RY.TO", 110.0)
        _insert_price("2025-01-31", "SHOP.TO", 100.0)

        with self.assertRaisesRegex(PerformanceDataError, "SHOP.TO"):
            calculate_daily_portfolio_returns(TEST_DB)

    def test_dashboard_mode_blocks_missing_price_dates_and_reports_issues(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 50.0)
        _insert_holding("2025-02-01", "SHOP.TO", 50.0)
        for row in [
            ("2025-01-31", "RY.TO", 100.0),
            ("2025-02-03", "RY.TO", 110.0),
            ("2025-02-04", "RY.TO", 121.0),
            ("2025-01-31", "SHOP.TO", 100.0),
            ("2025-02-04", "SHOP.TO", 110.0),
        ]:
            _insert_price(*row)

        result = calculate_daily_portfolio_returns_with_quality(TEST_DB, strict=False)

        self.assertTrue(result.data.empty)
        self.assertTrue(any(issue.issue_type == "missing_price" for issue in result.issues))
        self.assertTrue(any(issue.issue_type == "skipped_return_date" for issue in result.issues))

    def test_dashboard_twr_mode_returns_seeded_only_when_calculated_coverage_is_incomplete(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 1.0)
        _insert_holding("2025-02-01", "RY.TO", 50.0)
        _insert_holding("2025-02-01", "SHOP.TO", 50.0)
        for row in [
            ("2025-01-31", "RY.TO", 100.0),
            ("2025-02-03", "RY.TO", 110.0),
            ("2025-02-04", "RY.TO", 121.0),
            ("2025-01-31", "SHOP.TO", 100.0),
            ("2025-02-04", "SHOP.TO", 110.0),
        ]:
            _insert_price(*row)

        result = calculate_twr_series_with_quality(TEST_DB, strict=False)

        self.assertEqual(result.data["date"].tolist(), ["2025-01-31"])
        self.assertEqual(result.data["phase"].tolist(), ["seeded"])
        self.assertTrue(result.issues)

    def test_missing_fx_for_active_usd_holding_is_reported_in_dashboard_mode(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "AAPL", 100.0)
        _insert_price("2025-01-31", "AAPL", 100.0)
        _insert_price("2025-02-03", "AAPL", 110.0)

        result = calculate_daily_portfolio_returns_with_quality(TEST_DB, strict=False)

        self.assertTrue(result.data.empty)
        self.assertTrue(any(issue.issue_type == "missing_fx" for issue in result.issues))
    def test_missing_holding_data_is_not_silently_ignored_or_renormalized(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 50.0)
        _insert_holding("2025-02-01", "SHOP.TO", 50.0)
        _insert_price("2025-01-31", "RY.TO", 100.0)
        _insert_price("2025-02-03", "RY.TO", 110.0)
        _insert_price("2025-01-31", "SHOP.TO", 100.0)

        with self.assertRaisesRegex(PerformanceDataError, "SHOP.TO"):
            calculate_daily_portfolio_returns(TEST_DB)

    def test_partial_cash_weight_is_not_renormalized(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-01", "RY.TO", 80.0)
        _insert_price("2025-01-31", "RY.TO", 100.0)
        _insert_price("2025-02-03", "RY.TO", 110.0)

        returns = calculate_daily_portfolio_returns(TEST_DB)

        self.assertAlmostEqual(returns.iloc[0]["daily_return"], 0.08)

    def test_benchmark_relative_metrics_align_on_common_valid_dates(self) -> None:
        portfolio = pd.DataFrame(
            {
                "date": ["2025-02-03", "2025-02-04", "2025-02-05"],
                "daily_return": [0.01, 0.02, 0.03],
            }
        )
        benchmark = pd.DataFrame(
            {
                "date": ["2025-02-04", "2025-02-05"],
                "benchmark_return": [0.01, 0.02],
            }
        )

        metrics = calculate_risk_metrics(portfolio, benchmark)

        self.assertIsNotNone(metrics.beta)

    def test_qtd_holding_price_return_for_cad_listed_holding(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-06-30", "AAA.TO", 100.0)
        _insert_price("2025-03-31", "AAA.TO", 100.0)
        _insert_price("2025-06-30", "AAA.TO", 125.0)

        result = calculate_qtd_holding_price_returns(TEST_DB, end_date="2025-06-30")

        self.assertEqual(result.top.iloc[0]["ticker"], "AAA.TO")
        self.assertAlmostEqual(result.top.iloc[0]["qtd_return"], 0.25)
        self.assertEqual(result.top.iloc[0]["start_price_date"], "2025-03-31")
        self.assertEqual(result.top.iloc[0]["end_price_date"], "2025-06-30")

    def test_qtd_holding_price_return_for_usd_holding_uses_cadusd_fx(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-06-30", "AAPL", 100.0)
        _insert_price("2025-03-31", "AAPL", 100.0)
        _insert_price("2025-06-30", "AAPL", 110.0)
        _insert_fx("2025-03-31", 0.50)
        _insert_fx("2025-06-30", 0.55)

        result = calculate_qtd_holding_price_returns(TEST_DB, end_date="2025-06-30")

        self.assertAlmostEqual(result.top.iloc[0]["qtd_return"], 0.0)

    def test_qtd_holding_price_return_excludes_missing_price_or_fx(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-06-30", "AAA.TO", 50.0)
        _insert_holding("2025-06-30", "AAPL", 50.0)
        _insert_price("2025-03-31", "AAA.TO", 100.0)
        _insert_price("2025-06-30", "AAA.TO", 110.0)
        _insert_price("2025-03-31", "AAPL", 100.0)
        _insert_price("2025-06-30", "AAPL", 110.0)

        result = calculate_qtd_holding_price_returns(TEST_DB, end_date="2025-06-30")

        self.assertEqual(result.returns["ticker"].tolist(), ["AAA.TO"])
        self.assertTrue(any(issue.issue_type == "missing_fx" and issue.ticker == "AAPL" for issue in result.issues))

    def test_qtd_holding_price_return_ranks_top_and_bottom_five_current_holdings(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        for index, finish_price in enumerate([130, 120, 110, 105, 101, 99, 95], start=1):
            ticker = f"AAA{index}.TO"
            _insert_holding("2025-03-31", ticker, 0.0)
            _insert_holding("2025-06-30", ticker, 10.0)
            _insert_price("2025-03-31", ticker, 100.0)
            _insert_price("2025-06-30", ticker, float(finish_price))
        _insert_holding("2025-03-31", "OLD.TO", 10.0)
        _insert_price("2025-03-31", "OLD.TO", 100.0)
        _insert_price("2025-06-30", "OLD.TO", 200.0)

        result = calculate_qtd_holding_price_returns(TEST_DB, end_date="2025-06-30")

        self.assertEqual(len(result.top), 5)
        self.assertEqual(len(result.bottom), 5)
        self.assertEqual(result.top.iloc[0]["ticker"], "AAA1.TO")
        self.assertEqual(result.bottom.iloc[0]["ticker"], "AAA7.TO")
        self.assertNotIn("OLD.TO", result.returns["ticker"].tolist())


def _load_standard_fixture(include_benchmark: bool = False) -> None:
    init_db(TEST_DB)
    _insert_seeded_return("2025-01-31", 2.0)
    _insert_holding("2025-02-01", "RY.TO", 60.0)
    _insert_holding("2025-02-01", "AAPL", 40.0)
    for row in [
        ("2025-01-31", "RY.TO", 100.0),
        ("2025-02-03", "RY.TO", 110.0),
        ("2025-02-04", "RY.TO", 99.0),
        ("2025-02-05", "RY.TO", 108.9),
        ("2025-01-31", "AAPL", 100.0),
        ("2025-02-03", "AAPL", 110.0),
        ("2025-02-04", "AAPL", 110.0),
        ("2025-02-05", "AAPL", 121.0),
    ]:
        _insert_price(*row)
    for row in [
        ("2025-01-31", 0.50),
        ("2025-02-03", 0.50),
        ("2025-02-04", 0.55),
        ("2025-02-05", 0.55),
    ]:
        _insert_fx(*row)
    if include_benchmark:
        for row in [
            ("2025-01-31", BENCHMARK_TICKER, 100.0),
            ("2025-02-03", BENCHMARK_TICKER, 105.0),
            ("2025-02-04", BENCHMARK_TICKER, 102.9),
            ("2025-02-05", BENCHMARK_TICKER, 108.045),
        ]:
            _insert_price(*row)


def _insert_seeded_return(seed_date: str, return_pct: float, benchmark_return_pct: float | None = None) -> None:
    _execute(
        """
        INSERT INTO seeded_returns (date, return_pct, benchmark_return_pct, source, notes)
        VALUES (?, ?, ?, 'manager reported', NULL)
        """,
        (seed_date, return_pct, benchmark_return_pct),
    )


def _insert_holding(snapshot_date: str, ticker: str, weight: float) -> None:
    currency = "CAD" if ticker.endswith((".TO", ".V", ".NE", ".CN")) else "USD"
    _execute(
        """
        INSERT INTO holdings (date, ticker, weight, currency, shares, cost_basis)
        VALUES (?, ?, ?, ?, NULL, NULL)
        """,
        (snapshot_date, ticker, weight, currency),
    )


def _insert_price(price_date: str, ticker: str, adj_close: float) -> None:
    _execute(
        """
        INSERT INTO prices (date, ticker, close, adj_close)
        VALUES (?, ?, ?, ?)
        """,
        (price_date, ticker, adj_close, adj_close),
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


def _chain(returns: list[float]) -> float:
    value = 1.0
    for daily_return in returns:
        value *= 1.0 + daily_return
    return value - 1.0


if __name__ == "__main__":
    unittest.main()
