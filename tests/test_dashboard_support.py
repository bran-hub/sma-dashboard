from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sma_dashboard.dashboard_support import (
    BENCHMARK_REFRESH_COMMAND,
    DEFAULT_STARTING_CAPITAL,
    DASHBOARD_DEFAULT_DB_PATH,
    add_observation_axis_fields,
    benchmark_lag_warning,
    build_growth_chart_data,
    build_performance_chart_frame,
    calculate_trailing_annualized_returns,
    calculate_growth_y_axis_domain,
    database_overview,
    filter_performance_window,
    format_currency_cad,
    format_currency_cad_full,
    format_news_table,
    format_number,
    format_percent,
    format_qtd_holding_returns_table,
    format_ratio,
    format_risk_metrics_table,
    format_trade_log_table,
    format_valuation_table,
    format_weighted_valuation_averages,
    get_calculated_benchmark_price_coverage,
    get_dashboard_freshness,
    get_observed_chart_dates,
    get_range_start_date,
    label_for,
    list_trade_filter_values,
    load_trade_log,
    normalize_starting_capital,
    pending_model_update_notice,
    prepare_valuation_table_for_display,
    prepare_rolling_metrics_chart_data,
    refresh_dashboard_market_data,
    resolve_dashboard_db_path,
    select_calendar_axis_tick_dates,
    select_observation_axis_labels,
    select_rolling_metrics_axis_tick_dates,
    select_x_axis_tick_dates,
    should_use_compressed_trading_axis,
    validate_dashboard_database,
    latest_rolling_metrics,
)
from sma_dashboard.db import DEFAULT_DB_PATH, PROJECT_ROOT, init_db
from sma_dashboard.performance import BENCHMARK_TICKER, calculate_rolling_metrics


TEST_DB = Path("data/db/test_dashboard_support.sqlite")
TEST_MANIFEST = Path("data/raw/test_dashboard_support_manifest.csv")
TEST_MODEL_FILE = Path("data/raw/test_dashboard_support_model.xlsx")


class DashboardSupportTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)
        TEST_MANIFEST.unlink(missing_ok=True)
        TEST_MODEL_FILE.unlink(missing_ok=True)

    def test_trade_log_query_filters_and_sorts_most_recent_first(self) -> None:
        init_db(TEST_DB)
        _insert_trade("2025-02-03", "RY.TO", "buy", 1.0)
        _insert_trade("2025-02-05", "RY.TO", "sell", -0.5)
        _insert_trade("2025-02-04", "AAPL", "buy", 2.0)

        trades = load_trade_log(TEST_DB, ticker="RY.TO", start_date="2025-02-04")

        self.assertEqual(trades["date"].tolist(), ["2025-02-05"])
        self.assertEqual(trades.iloc[0]["action"], "sell")

    def test_dashboard_default_db_path_resolves_to_project_db_file(self) -> None:
        self.assertEqual(resolve_dashboard_db_path(), DASHBOARD_DEFAULT_DB_PATH)
        self.assertEqual(DASHBOARD_DEFAULT_DB_PATH, PROJECT_ROOT / "data" / "db" / "sma_dashboard.db")
        self.assertEqual(DEFAULT_DB_PATH, DASHBOARD_DEFAULT_DB_PATH)

    def test_relative_db_path_resolves_against_project_root(self) -> None:
        path = resolve_dashboard_db_path("data/db/custom.sqlite")

        self.assertEqual(path, PROJECT_ROOT / "data" / "db" / "custom.sqlite")

    def test_absolute_db_path_is_preserved(self) -> None:
        absolute = Path("C:/tmp/sma_dashboard_test.db")

        self.assertEqual(resolve_dashboard_db_path(absolute), absolute)

    def test_missing_db_file_is_detected_gracefully(self) -> None:
        missing = TEST_DB.parent / "missing_dashboard.sqlite"
        missing.unlink(missing_ok=True)

        validation = validate_dashboard_database(missing)

        self.assertFalse(validation.is_valid)
        self.assertIn("does not exist", validation.message)

    def test_empty_sqlite_db_is_detected_gracefully(self) -> None:
        TEST_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(TEST_DB)
        conn.close()

        validation = validate_dashboard_database(TEST_DB)

        self.assertFalse(validation.is_valid)
        self.assertIn("not initialized", validation.message)
        self.assertIn("seeded_returns", validation.missing_tables)

    def test_initialized_db_passes_dashboard_validation(self) -> None:
        init_db(TEST_DB)

        validation = validate_dashboard_database(TEST_DB)

        self.assertTrue(validation.is_valid)
        self.assertEqual(validation.missing_tables, ())

    def test_trade_filter_values(self) -> None:
        init_db(TEST_DB)
        _insert_trade("2025-02-03", "RY.TO", "buy", 1.0)
        _insert_trade("2025-02-04", "AAPL", "sell", -1.0)

        tickers, actions = list_trade_filter_values(TEST_DB)

        self.assertEqual(tickers, ["AAPL", "RY.TO"])
        self.assertEqual(actions, ["buy", "sell"])

    def test_database_overview_counts_expected_tables(self) -> None:
        init_db(TEST_DB)
        _insert_trade("2025-02-03", "RY.TO", "buy", 1.0)

        overview = database_overview(TEST_DB)

        self.assertEqual(overview["trades"], 1)
        self.assertEqual(overview["holdings"], 0)

    def test_missing_calculated_benchmark_prices_are_detected_without_raw_sql_errors(self) -> None:
        init_db(TEST_DB)

        coverage = get_calculated_benchmark_price_coverage(TEST_DB)

        self.assertFalse(coverage.has_calculated_prices)
        self.assertEqual(coverage.benchmark_ticker, BENCHMARK_TICKER)
        self.assertEqual(coverage.row_count, 0)
        self.assertIsNone(coverage.min_date)

    def test_calculated_benchmark_price_coverage_detects_available_rows(self) -> None:
        init_db(TEST_DB)
        _insert_price("2025-01-31", BENCHMARK_TICKER, 100.0)
        _insert_price("2025-02-03", BENCHMARK_TICKER, 110.0)
        _insert_price("2025-02-04", BENCHMARK_TICKER, 111.0)

        coverage = get_calculated_benchmark_price_coverage(TEST_DB)

        self.assertTrue(coverage.has_calculated_prices)
        self.assertEqual(coverage.row_count, 2)
        self.assertEqual(coverage.min_date, "2025-02-03")
        self.assertEqual(coverage.max_date, "2025-02-04")

    def test_benchmark_coverage_handles_empty_database_without_no_such_table_error(self) -> None:
        TEST_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(TEST_DB)
        conn.close()

        coverage = get_calculated_benchmark_price_coverage(TEST_DB)

        self.assertFalse(coverage.has_calculated_prices)
        self.assertEqual(coverage.row_count, 0)

    def test_benchmark_refresh_command_is_available_for_dashboard_warning(self) -> None:
        self.assertIn("refresh_benchmark.py", BENCHMARK_REFRESH_COMMAND)
        self.assertIn("--db data/db/sma_dashboard.db", BENCHMARK_REFRESH_COMMAND)
        self.assertIn("--benchmark ^GSPTSE", BENCHMARK_REFRESH_COMMAND)
        self.assertIn("--start-date 2025-02-01", BENCHMARK_REFRESH_COMMAND)

    def test_dashboard_freshness_reports_latest_dates(self) -> None:
        init_db(TEST_DB)
        _insert_holding("2025-02-03", "RY.TO", 100.0)
        _insert_price("2025-02-04", "RY.TO", 101.0)
        _insert_price("2025-02-05", BENCHMARK_TICKER, 100.0)

        freshness = get_dashboard_freshness(TEST_DB)

        self.assertEqual(freshness.latest_model_update_date, "2025-02-03")
        self.assertEqual(freshness.latest_current_holding_price_date, "2025-02-04")
        self.assertEqual(freshness.latest_benchmark_price_date, "2025-02-05")
        self.assertEqual(freshness.current_holding_price_status, "complete")
        self.assertEqual(freshness.missing_current_holding_price_tickers, ())

    def test_dashboard_freshness_reports_missing_current_holding_prices(self) -> None:
        init_db(TEST_DB)
        _insert_holding("2025-02-03", "AAPL", 40.0)
        _insert_holding("2025-02-03", "RY.TO", 60.0)
        _insert_price("2025-02-04", "RY.TO", 101.0)

        freshness = get_dashboard_freshness(TEST_DB)

        self.assertIsNone(freshness.latest_current_holding_price_date)
        self.assertEqual(freshness.current_holding_price_status, "missing_prices")
        self.assertEqual(freshness.missing_current_holding_price_tickers, ("AAPL",))

    def test_refresh_dashboard_market_data_refreshes_each_current_holding_and_benchmark(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-03", "AAPL", 40.0)
        _insert_holding("2025-02-03", "RY.TO", 60.0)
        _insert_holding("2025-02-03", "CASH.TO", 0.0)
        _insert_price("2025-02-04", "AAPL", 100.0)
        _insert_price("2025-02-05", "RY.TO", 100.0)
        _insert_price("2025-02-06", BENCHMARK_TICKER, 100.0)

        with (
            patch("sma_dashboard.ingestion.refresh_market_data", side_effect=[(2, 2), (3, 0)]) as refresh_market,
            patch("sma_dashboard.performance.refresh_benchmark_prices_with_summary") as refresh_benchmark,
        ):
            refresh_benchmark.return_value = SimpleNamespace(rows_written=4)
            summary = refresh_dashboard_market_data(TEST_DB)

        self.assertEqual([call.args[0] for call in refresh_market.call_args_list], [["AAPL"], ["RY.TO"]])
        refresh_benchmark.assert_called_once()
        self.assertEqual(refresh_benchmark.call_args.kwargs["start_date"], "2025-02-07")
        self.assertEqual(summary.holding_price_rows_written, 5)
        self.assertEqual(summary.fx_rows_written, 2)
        self.assertEqual(summary.benchmark_rows_written, 4)
        self.assertEqual(summary.holding_tickers, ("AAPL", "RY.TO"))
        self.assertEqual(summary.missing_holding_price_tickers_after, ())
        self.assertEqual(summary.failed_tickers, ())

    def test_refresh_dashboard_market_data_preserves_partial_failures_as_warnings(self) -> None:
        init_db(TEST_DB)
        _insert_seeded_return("2025-01-31", 0.0)
        _insert_holding("2025-02-03", "AAPL", 50.0)
        _insert_holding("2025-02-03", "RY.TO", 50.0)
        _insert_price("2025-02-04", "RY.TO", 100.0)

        with (
            patch("sma_dashboard.ingestion.refresh_market_data", side_effect=[RuntimeError("rate limit"), (1, 0)]),
            patch("sma_dashboard.performance.refresh_benchmark_prices_with_summary") as refresh_benchmark,
        ):
            refresh_benchmark.return_value = SimpleNamespace(rows_written=0)
            summary = refresh_dashboard_market_data(TEST_DB)

        self.assertEqual(summary.holding_price_rows_written, 1)
        self.assertTrue(any("AAPL" in warning and "rate limit" in warning for warning in summary.warnings))
        self.assertEqual(summary.missing_holding_price_tickers_after, ("AAPL",))
        self.assertEqual(summary.failed_tickers, ("AAPL",))

    def test_pending_model_update_notice_detects_manifest_newer_than_holdings(self) -> None:
        init_db(TEST_DB)
        _insert_holding("2025-02-03", "RY.TO", 100.0)
        TEST_MODEL_FILE.write_text("", encoding="utf-8")
        TEST_MANIFEST.write_text(
            "file,model_date,notes\n"
            "data/raw/test_dashboard_support_model.xlsx,2025-02-10,test\n",
            encoding="utf-8",
        )

        notice = pending_model_update_notice(TEST_DB, TEST_MANIFEST)

        self.assertIsNotNone(notice)
        self.assertIn("2025-02-10", notice)
        self.assertIn("2025-02-03", notice)

    def test_formatting_helpers(self) -> None:
        self.assertEqual(format_percent(0.1234), "12.34%")
        self.assertEqual(format_number(3.14159), "3.14")
        self.assertEqual(format_ratio(18.4), "18.40")
        self.assertEqual(format_currency_cad(2_400_000_000), "C$2.4B")
        self.assertEqual(format_currency_cad(850_000_000), "C$850M")
        self.assertEqual(format_currency_cad(12_500_000), "C$12.5M")
        self.assertEqual(format_currency_cad_full(100000), "C$100,000")
        self.assertEqual(format_percent(None), "—")

    def test_starting_capital_defaults_and_invalid_values(self) -> None:
        self.assertEqual(DEFAULT_STARTING_CAPITAL, 100000.0)
        self.assertEqual(normalize_starting_capital(None), 100000.0)
        self.assertEqual(normalize_starting_capital(0), 100000.0)
        self.assertEqual(normalize_starting_capital(-1), 100000.0)
        self.assertEqual(normalize_starting_capital("bad"), 100000.0)
        self.assertEqual(normalize_starting_capital(250000), 250000.0)

    def test_growth_chart_rebases_portfolio_and_benchmark_to_starting_capital(self) -> None:
        portfolio = _performance_frame(
            ["2025-01-31", "2025-02-03", "2025-02-04"],
            [0.02, 0.05, 0.10],
            ["seeded", "calculated", "calculated"],
        )
        benchmark = _performance_frame(
            ["2025-01-31", "2025-02-03", "2025-02-04"],
            [0.01, 0.03, 0.04],
            ["seeded", "calculated", "calculated"],
        )

        chart = build_growth_chart_data(portfolio, benchmark, starting_capital=100000, selected_range="1M")
        first_values = chart.sort_values("date").groupby("series")["growth"].first().to_dict()

        self.assertAlmostEqual(first_values["Portfolio"], 100000.0)
        self.assertAlmostEqual(first_values["Benchmark"], 100000.0)
        self.assertIn("growth_display", chart.columns)
        self.assertEqual(chart.loc[chart["series"] == "Portfolio", "growth_display"].iloc[0], "C$100,000")

    def test_growth_chart_keeps_latest_benchmark_observation_for_short_ranges(self) -> None:
        portfolio = _performance_frame(
            ["2025-06-18", "2025-06-19", "2025-06-20", "2025-06-23", "2025-06-24"],
            [0.01, 0.02, 0.03, 0.04, 0.05],
        )
        benchmark = _performance_frame(
            ["2025-06-18", "2025-06-19", "2025-06-20", "2025-06-23", "2025-06-24"],
            [0.005, 0.01, 0.015, 0.02, 0.025],
        )

        for range_key in ["5D", "1M"]:
            chart = build_growth_chart_data(portfolio, benchmark, selected_range=range_key)
            benchmark_dates = chart.loc[chart["series"] == "Benchmark", "date"].dt.strftime("%Y-%m-%d").tolist()

            self.assertIn("2025-06-24", benchmark_dates)
            self.assertEqual(benchmark_dates[-1], "2025-06-24")

    def test_compressed_observation_axis_keeps_final_benchmark_observation(self) -> None:
        chart = pd.DataFrame(
            {
                "date": ["2025-06-23", "2025-06-23", "2025-06-24", "2025-06-24"],
                "series": ["Portfolio", "Benchmark", "Portfolio", "Benchmark"],
                "growth": [100000, 100000, 100100, 100050],
            }
        )

        enriched = add_observation_axis_fields(chart)
        final_benchmark = enriched[(enriched["series"] == "Benchmark") & (enriched["date"] == "2025-06-24")]

        self.assertEqual(len(final_benchmark), 1)
        self.assertEqual(final_benchmark.iloc[0]["observation_index"], 1)

    def test_benchmark_lag_warning_when_benchmark_ends_before_portfolio(self) -> None:
        portfolio = _performance_frame(["2025-06-23", "2025-06-24"], [0.01, 0.02])
        benchmark = _performance_frame(["2025-06-23"], [0.01])

        warning = benchmark_lag_warning(portfolio, benchmark, BENCHMARK_TICKER)

        self.assertIsNotNone(warning)
        self.assertIn("2025-06-23", warning)
        self.assertIn("2025-06-24", warning)

    def test_no_benchmark_lag_warning_when_benchmark_is_current(self) -> None:
        portfolio = _performance_frame(["2025-06-23", "2025-06-24"], [0.01, 0.02])
        benchmark = _performance_frame(["2025-06-23", "2025-06-24"], [0.01, 0.015])

        self.assertIsNone(benchmark_lag_warning(portfolio, benchmark, BENCHMARK_TICKER))

    def test_five_day_range_selects_most_recent_five_available_observations(self) -> None:
        frame = _performance_frame(
            ["2025-02-03", "2025-02-04", "2025-02-05", "2025-02-06", "2025-02-07", "2025-02-10"],
            [0.01, 0.02, 0.03, 0.04, 0.05, 0.06],
        )

        filtered = filter_performance_window(frame, "5D")

        self.assertEqual(
            filtered["date"].dt.strftime("%Y-%m-%d").tolist(),
            ["2025-02-04", "2025-02-05", "2025-02-06", "2025-02-07", "2025-02-10"],
        )

    def test_five_day_growth_chart_contains_latest_five_valid_observation_dates(self) -> None:
        portfolio = _performance_frame(
            ["2025-06-16", "2025-06-17", "2025-06-18", "2025-06-19", "2025-06-23", "2025-06-24"],
            [0.01, 0.02, 0.01, 0.03, 0.04, 0.05],
        )
        benchmark = _performance_frame(
            ["2025-06-16", "2025-06-17", "2025-06-18", "2025-06-19", "2025-06-23", "2025-06-24"],
            [0.005, 0.01, 0.0, 0.02, 0.03, 0.04],
        )

        chart = build_growth_chart_data(portfolio, benchmark, selected_range="5D")
        observed = [date.strftime("%Y-%m-%d") for date in get_observed_chart_dates(chart)]

        self.assertEqual(observed, ["2025-06-17", "2025-06-18", "2025-06-19", "2025-06-23", "2025-06-24"])
        self.assertNotIn("2025-06-20", observed)
        self.assertNotIn("2025-06-21", observed)

    def test_one_month_axis_ticks_are_observed_dates_not_weekend_calendar_ticks(self) -> None:
        chart = pd.DataFrame(
            {
                "date": [
                    "2025-06-16",
                    "2025-06-17",
                    "2025-06-18",
                    "2025-06-19",
                    "2025-06-23",
                    "2025-06-24",
                ],
                "series": ["Portfolio"] * 6,
                "growth": [100000, 100100, 100050, 100200, 100300, 100350],
            }
        )

        ticks = select_x_axis_tick_dates(chart, "1M")
        tick_labels = [date.strftime("%Y-%m-%d") for date in ticks]

        self.assertEqual(tick_labels, ["2025-06-16", "2025-06-17", "2025-06-18", "2025-06-19", "2025-06-23", "2025-06-24"])
        self.assertNotIn("2025-06-20", tick_labels)
        self.assertNotIn("2025-06-21", tick_labels)

    def test_x_axis_tick_dates_deduplicate_long_form_series_rows(self) -> None:
        chart = pd.DataFrame(
            {
                "date": ["2025-06-18", "2025-06-18", "2025-06-19", "2025-06-19"],
                "series": ["Portfolio", "Benchmark", "Portfolio", "Benchmark"],
                "growth": [100000, 100000, 100100, 99950],
            }
        )

        ticks = select_x_axis_tick_dates(chart, "5D")

        self.assertEqual([date.strftime("%Y-%m-%d") for date in ticks], ["2025-06-18", "2025-06-19"])

    def test_observed_chart_dates_are_sorted_chronologically(self) -> None:
        chart = pd.DataFrame(
            {
                "date": ["2025-06-24", "2025-06-18", "2025-06-23", "2025-06-18"],
                "series": ["Portfolio", "Portfolio", "Portfolio", "Benchmark"],
                "growth": [100300, 100000, 100200, 99900],
            }
        )

        observed = get_observed_chart_dates(chart)

        self.assertEqual([date.strftime("%Y-%m-%d") for date in observed], ["2025-06-18", "2025-06-23", "2025-06-24"])

    def test_axis_mode_policy_by_range(self) -> None:
        for range_key in ["5D", "1M", "6M", "YTD", "1Y"]:
            self.assertTrue(should_use_compressed_trading_axis(range_key))
        for range_key in ["5Y", "10Y", "Since Inception"]:
            self.assertFalse(should_use_compressed_trading_axis(range_key))

    def test_observation_axis_fields_compress_weekend_calendar_gaps(self) -> None:
        chart = pd.DataFrame(
            {
                "date": ["2025-06-19", "2025-06-19", "2025-06-23", "2025-06-23", "2025-06-24"],
                "series": ["Portfolio", "Benchmark", "Portfolio", "Benchmark", "Portfolio"],
                "growth": [100000, 100000, 100200, 100100, 100300],
            }
        )

        enriched = add_observation_axis_fields(chart)
        index_by_date = enriched.drop_duplicates("date").set_index("date")["observation_index"].to_dict()

        self.assertEqual(index_by_date["2025-06-19"], 0)
        self.assertEqual(index_by_date["2025-06-23"], 1)
        self.assertEqual(index_by_date["2025-06-24"], 2)

    def test_five_day_observation_axis_labels_show_each_observed_date_once(self) -> None:
        chart = pd.DataFrame(
            {
                "date": ["2025-06-17", "2025-06-18", "2025-06-19", "2025-06-23", "2025-06-24"] * 2,
                "series": ["Portfolio"] * 5 + ["Benchmark"] * 5,
                "growth": [100000, 100100, 100050, 100200, 100300] * 2,
            }
        )

        labels = select_observation_axis_labels(chart, "5D")

        self.assertEqual([label["index"] for label in labels], [0, 1, 2, 3, 4])
        self.assertEqual([label["label"] for label in labels], ["Jun 17", "Jun 18", "Jun 19", "Jun 23", "Jun 24"])

    def test_medium_range_observation_axis_uses_readable_subset(self) -> None:
        dates = pd.bdate_range("2025-06-02", periods=22)
        chart = pd.DataFrame({"date": dates, "series": "Portfolio", "growth": range(len(dates))})

        labels = select_observation_axis_labels(chart, "1M")

        self.assertLessEqual(len(labels), 6)
        self.assertEqual(labels[0]["index"], 0)
        self.assertEqual(labels[-1]["index"], len(dates) - 1)
        self.assertEqual(len({label["index"] for label in labels}), len(labels))

    def test_calendar_axis_ticks_for_five_year_range_are_annual_observed_dates(self) -> None:
        dates = pd.to_datetime(["2021-01-04", "2022-01-04", "2023-01-03", "2024-01-02", "2025-01-02", "2026-01-02"])
        chart = pd.DataFrame({"date": dates, "series": "Portfolio", "growth": range(len(dates))})

        ticks = select_calendar_axis_tick_dates(chart, "5Y")

        self.assertEqual([tick.year for tick in ticks], [2021, 2022, 2023, 2024, 2025, 2026])

    def test_calendar_axis_ticks_for_ten_year_range_are_well_distributed(self) -> None:
        dates = pd.to_datetime([f"{year}-01-04" for year in range(2016, 2027)])
        chart = pd.DataFrame({"date": dates, "series": "Portfolio", "growth": range(len(dates))})

        ticks = select_calendar_axis_tick_dates(chart, "10Y")

        self.assertIn(ticks[0].year, [2016])
        self.assertEqual(ticks[-1].year, 2026)
        self.assertLessEqual(max(b.year - a.year for a, b in zip(ticks, ticks[1:])), 2)

    def test_since_inception_calendar_ticks_cover_full_range_without_large_initial_gap(self) -> None:
        dates = pd.to_datetime([f"{year}-01-31" for year in range(2013, 2027)])
        chart = pd.DataFrame({"date": dates, "series": "Portfolio", "growth": range(len(dates))})

        ticks = select_calendar_axis_tick_dates(chart, "Since Inception")

        self.assertEqual(ticks[0].year, 2013)
        self.assertLessEqual(ticks[1].year, 2014)
        self.assertEqual(ticks[-1].year, 2026)
        self.assertLessEqual(max(b.year - a.year for a, b in zip(ticks, ticks[1:])), 1)

    def test_growth_chart_uses_compressed_axis_for_short_ranges_and_date_tooltip(self) -> None:
        from sma_dashboard.dashboard import _growth_chart

        chart_data = pd.DataFrame(
            {
                "date": ["2025-06-18", "2025-06-19", "2025-06-23"],
                "series": ["Portfolio"] * 3,
                "growth": [100000, 100100, 100200],
                "phase": ["Calculated"] * 3,
                "growth_display": ["C$100,000", "C$100,100", "C$100,200"],
            }
        )

        spec = _growth_chart(chart_data, "5D", 100000).to_dict()

        self.assertEqual(spec["encoding"]["x"]["field"], "observation_index")
        self.assertEqual(spec["encoding"]["tooltip"][0]["field"], "date")

    def test_growth_chart_uses_temporal_axis_for_long_ranges(self) -> None:
        from sma_dashboard.dashboard import _growth_chart

        chart_data = pd.DataFrame(
            {
                "date": ["2021-01-04", "2022-01-04", "2023-01-03"],
                "series": ["Portfolio"] * 3,
                "growth": [100000, 101000, 102000],
                "phase": ["Seeded"] * 3,
                "growth_display": ["C$100,000", "C$101,000", "C$102,000"],
            }
        )

        spec = _growth_chart(chart_data, "5Y", 100000).to_dict()

        self.assertEqual(spec["encoding"]["x"]["field"], "date")
        self.assertEqual(spec["encoding"]["x"]["type"], "temporal")

    def test_calendar_range_start_dates_are_relative_to_latest_available_date(self) -> None:
        latest = pd.Timestamp("2025-06-15")

        self.assertEqual(get_range_start_date(latest, "1M"), pd.Timestamp("2025-05-15"))
        self.assertEqual(get_range_start_date(latest, "6M"), pd.Timestamp("2024-12-15"))
        self.assertEqual(get_range_start_date(latest, "1Y"), pd.Timestamp("2024-06-15"))
        self.assertEqual(get_range_start_date(latest, "5Y"), pd.Timestamp("2020-06-15"))
        self.assertEqual(get_range_start_date(latest, "10Y"), pd.Timestamp("2015-06-15"))

    def test_ytd_and_since_inception_ranges(self) -> None:
        frame = _performance_frame(
            ["2024-12-31", "2025-01-15", "2025-02-01"],
            [0.01, 0.02, 0.03],
        )

        ytd = filter_performance_window(frame, "YTD")
        since_inception = filter_performance_window(frame, "Since Inception")

        self.assertEqual(get_range_start_date(pd.Timestamp("2025-02-01"), "YTD"), pd.Timestamp("2025-01-01"))
        self.assertEqual(ytd["date"].dt.strftime("%Y-%m-%d").tolist(), ["2025-01-15", "2025-02-01"])
        self.assertEqual(len(since_inception), 3)

    def test_qtd_range_start_uses_latest_observation_quarter(self) -> None:
        frame = _performance_frame(
            ["2025-03-31", "2025-04-01", "2025-04-15", "2025-06-30"],
            [0.01, 0.02, 0.03, 0.04],
        )

        qtd = filter_performance_window(frame, "QTD")

        self.assertEqual(get_range_start_date(pd.Timestamp("2025-06-30"), "QTD"), pd.Timestamp("2025-04-01"))
        self.assertEqual(qtd["date"].dt.strftime("%Y-%m-%d").tolist(), ["2025-04-01", "2025-04-15", "2025-06-30"])
        self.assertTrue(should_use_compressed_trading_axis("QTD"))

    def test_qtd_growth_chart_preserves_final_portfolio_and_benchmark_observations(self) -> None:
        portfolio = _performance_frame(["2025-04-01", "2025-04-02", "2025-06-30"], [0.01, 0.02, 0.05])
        benchmark = _performance_frame(["2025-04-01", "2025-04-02", "2025-06-30"], [0.005, 0.01, 0.03])

        chart = build_growth_chart_data(portfolio, benchmark, selected_range="QTD")

        self.assertEqual(chart.loc[chart["series"] == "Portfolio", "date"].max().strftime("%Y-%m-%d"), "2025-06-30")
        self.assertEqual(chart.loc[chart["series"] == "Benchmark", "date"].max().strftime("%Y-%m-%d"), "2025-06-30")

    def test_trailing_annualized_returns_use_common_dates(self) -> None:
        portfolio = _performance_frame(["2024-06-30", "2025-06-30"], [0.0, 0.10])
        benchmark = _performance_frame(["2024-06-30", "2025-06-30"], [0.0, 0.05])

        result = calculate_trailing_annualized_returns(portfolio, benchmark, periods=(1,))

        self.assertEqual(result.iloc[0]["status"], "Full")
        self.assertAlmostEqual(result.iloc[0]["portfolio"], 0.10, places=3)
        self.assertAlmostEqual(result.iloc[0]["benchmark"], 0.05, places=3)
        self.assertAlmostEqual(result.iloc[0]["excess"], 0.05, places=3)

    def test_trailing_annualized_returns_insufficient_history(self) -> None:
        portfolio = _performance_frame(["2025-01-31", "2025-06-30"], [0.0, 0.10])
        benchmark = _performance_frame(["2025-01-31", "2025-06-30"], [0.0, 0.05])

        result = calculate_trailing_annualized_returns(portfolio, benchmark, periods=(1,))

        self.assertEqual(result.iloc[0]["status"], "Insufficient history")
        self.assertTrue(pd.isna(result.iloc[0]["portfolio"]))

    def test_trailing_annualized_returns_do_not_use_non_common_start_dates(self) -> None:
        portfolio = _performance_frame(["2024-06-30", "2025-06-30"], [0.0, 0.10])
        benchmark = _performance_frame(["2024-08-15", "2025-06-30"], [0.0, 0.05])

        result = calculate_trailing_annualized_returns(portfolio, benchmark, periods=(1,), max_start_lag_days=31)

        self.assertEqual(result.iloc[0]["status"], "Insufficient history")

    def test_format_qtd_holding_returns_table(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "AAA.TO",
                    "weight": 4.25,
                    "qtd_return": 0.1234,
                    "start_price_date": "2025-03-31",
                    "end_price_date": "2025-06-30",
                }
            ]
        )

        display = format_qtd_holding_returns_table(frame)

        self.assertEqual(display.loc[0, "Weight"], "4.25%")
        self.assertEqual(display.loc[0, "QTD CAD Return"], "12.34%")

    def test_selected_ranges_do_not_crash_with_less_data_than_requested(self) -> None:
        frame = _performance_frame(["2025-02-03", "2025-02-04"], [0.01, 0.02])

        filtered = filter_performance_window(frame, "5D")
        chart = build_growth_chart_data(frame, None, selected_range="10Y")

        self.assertEqual(len(filtered), 2)
        self.assertFalse(chart.empty)

    def test_growth_y_axis_domain_does_not_start_at_zero_for_short_period_data(self) -> None:
        chart = pd.DataFrame({"growth": [100000.0, 100250.0, 100500.0]})

        lower, upper = calculate_growth_y_axis_domain(chart, starting_capital=100000.0)

        self.assertGreater(lower, 0.0)
        self.assertLess(lower, 100000.0)
        self.assertGreater(upper, 100500.0)

    def test_growth_y_axis_domain_includes_visible_portfolio_and_benchmark_values(self) -> None:
        chart = pd.DataFrame(
            {
                "series": ["Portfolio", "Benchmark", "Portfolio", "Benchmark"],
                "growth": [100000.0, 99850.0, 100425.0, 100200.0],
            }
        )

        lower, upper = calculate_growth_y_axis_domain(chart, starting_capital=100000.0)

        self.assertLessEqual(lower, chart["growth"].min())
        self.assertGreaterEqual(upper, chart["growth"].max())

    def test_growth_y_axis_domain_applies_padding_for_larger_visible_moves(self) -> None:
        chart = pd.DataFrame({"growth": [100000.0, 102000.0]})

        lower, upper = calculate_growth_y_axis_domain(chart, starting_capital=100000.0)

        self.assertAlmostEqual(lower, 99800.0)
        self.assertAlmostEqual(upper, 102200.0)

    def test_growth_y_axis_domain_enforces_minimum_span_for_tiny_moves(self) -> None:
        chart = pd.DataFrame({"growth": [100000.0, 100010.0]})

        lower, upper = calculate_growth_y_axis_domain(chart, starting_capital=100000.0)

        self.assertAlmostEqual(upper - lower, 1500.0)
        self.assertLessEqual(lower, 100000.0)
        self.assertGreaterEqual(upper, 100010.0)

    def test_growth_y_axis_domain_uses_data_driven_span_when_move_is_large(self) -> None:
        chart = pd.DataFrame({"growth": [100000.0, 110000.0]})

        lower, upper = calculate_growth_y_axis_domain(chart, starting_capital=100000.0)

        self.assertAlmostEqual(lower, 99000.0)
        self.assertAlmostEqual(upper, 111000.0)

    def test_growth_y_axis_domain_handles_empty_or_missing_data(self) -> None:
        self.assertIsNone(calculate_growth_y_axis_domain(pd.DataFrame(), starting_capital=100000.0))
        self.assertIsNone(calculate_growth_y_axis_domain(pd.DataFrame({"value": [1.0]}), starting_capital=100000.0))
        self.assertIsNone(
            calculate_growth_y_axis_domain(pd.DataFrame({"growth": [None, float("nan")]}), starting_capital=100000.0)
        )

    def test_growth_y_axis_domain_minimum_span_scales_with_starting_capital(self) -> None:
        chart = pd.DataFrame({"growth": [100000.0, 100010.0]})

        small_lower, small_upper = calculate_growth_y_axis_domain(chart, starting_capital=100000.0)
        large_lower, large_upper = calculate_growth_y_axis_domain(chart, starting_capital=200000.0)

        self.assertAlmostEqual(small_upper - small_lower, 1500.0)
        self.assertAlmostEqual(large_upper - large_lower, 3000.0)

    def test_dividend_yield_decimal_formats_as_percentage(self) -> None:
        valuation = pd.DataFrame(
            [
                {
                    "ticker": "AAA.TO",
                    "weight": 4.25,
                    "pe_trailing": 18.4,
                    "pe_forward": None,
                    "pb": 2.5,
                    "ev_ebitda": 10.0,
                    "dividend_yield": 0.035,
                    "market_cap": 2_400_000_000,
                }
            ]
        )

        display = format_valuation_table(valuation)

        self.assertEqual(display.loc[0, "Dividend Yield"], "3.50%")
        self.assertEqual(display.loc[0, "Weight"], "4.25%")
        self.assertEqual(display.loc[0, "Trailing P/E"], "18.40")
        self.assertEqual(display.loc[0, "Forward P/E"], "—")
        self.assertEqual(display.loc[0, "Market Cap (CAD)"], "C$2.4B")
        self.assertNotIn("pe_trailing", display.columns)
        self.assertNotIn("dividend_yield", display.columns)

    def test_normalized_sub_one_percent_dividend_yield_formats_correctly(self) -> None:
        valuation = pd.DataFrame([{"ticker": "AAA.TO", "weight": 4.25, "dividend_yield": 0.0092}])

        display = format_valuation_table(valuation)

        self.assertEqual(display.loc[0, "Dividend Yield"], "0.92%")

    def test_interactive_valuation_table_formats_market_cap_and_preserves_other_numeric_dtypes(self) -> None:
        valuation = pd.DataFrame(
            [
                {
                    "ticker": "AAA.TO",
                    "weight": 4.25,
                    "pe_trailing": 18.4,
                    "pe_forward": None,
                    "pb": 2.5,
                    "ev_ebitda": 10.0,
                    "dividend_yield": 0.035,
                    "market_cap": 2_400_000_000,
                }
            ]
        )

        display = prepare_valuation_table_for_display(valuation)

        for column in (
            "Weight",
            "Trailing P/E",
            "Forward P/E",
            "Price / Book",
            "EV / EBITDA",
            "Dividend Yield",
        ):
            self.assertTrue(pd.api.types.is_numeric_dtype(display[column]), column)
        self.assertEqual(display.loc[0, "Ticker"], "AAA.TO")
        self.assertEqual(display.loc[0, "Market Cap (CAD)"], "C$2,400,000,000")
        self.assertFalse(pd.api.types.is_numeric_dtype(display["Market Cap (CAD)"]))

    def test_interactive_valuation_table_sorts_ratios_and_formats_market_cap(self) -> None:
        valuation = pd.DataFrame(
            [
                {"ticker": "HIGH.TO", "pe_trailing": 100.0, "market_cap": 850_000_000},
                {"ticker": "LOW.TO", "pe_trailing": 9.5, "market_cap": 2_400_000_000},
                {"ticker": "MID.TO", "pe_trailing": 18.4, "market_cap": 12_500_000},
            ]
        )

        display = prepare_valuation_table_for_display(valuation)

        pe_order = display.sort_values("Trailing P/E")["Ticker"].tolist()

        self.assertEqual(pe_order, ["LOW.TO", "MID.TO", "HIGH.TO"])
        self.assertEqual(
            display.set_index("Ticker")["Market Cap (CAD)"].to_dict(),
            {
                "HIGH.TO": "C$850,000,000",
                "LOW.TO": "C$2,400,000,000",
                "MID.TO": "C$12,500,000",
            },
        )

    def test_weighted_dividend_yield_formats_as_percentage(self) -> None:
        display = format_weighted_valuation_averages(
            {
                "pe_trailing": 18.4,
                "pe_forward": None,
                "pb": 2.5,
                "ev_ebitda": 10.0,
                "dividend_yield": 0.0125,
                "market_cap": 850_000_000,
            }
        )

        values = dict(display.to_records(index=False))

        self.assertEqual(values["Dividend Yield"], "1.25%")
        self.assertEqual(values["Forward P/E"], "—")
        self.assertEqual(values["Market Cap (CAD)"], "C$850M")

    def test_user_facing_label_mapping(self) -> None:
        self.assertEqual(label_for("pe_trailing"), "Trailing P/E")
        self.assertEqual(label_for("market_cap_cad"), "Market Cap (CAD)")
        self.assertEqual(label_for("publisher"), "Source")

    def test_trade_log_table_formats_labels_and_weight_change(self) -> None:
        trades = pd.DataFrame(
            [{"date": "2025-02-03", "ticker": "AAA.TO", "action": "buy", "weight_change": 1.25, "notes": None}]
        )

        display = format_trade_log_table(trades)

        self.assertEqual(display.loc[0, "Action"], "Buy")
        self.assertEqual(display.loc[0, "Weight Change"], "1.25%")
        self.assertEqual(display.loc[0, "Notes"], "—")
        self.assertNotIn("weight_change", display.columns)

    def test_news_table_formats_labels_weight_and_date(self) -> None:
        news = pd.DataFrame(
            [{"published_at": "2025-02-03T10:30:00", "ticker": "AAA.TO", "weight": 4.25, "title": "Headline", "publisher": "Source", "link": None}]
        )

        display = format_news_table(news)

        self.assertEqual(display.loc[0, "Published"], "2025-02-03")
        self.assertEqual(display.loc[0, "Weight"], "4.25%")
        self.assertEqual(display.loc[0, "Source"], "Source")
        self.assertEqual(display.loc[0, "Link"], "—")
        self.assertNotIn("publisher", display.columns)

    def test_risk_metric_formatting_uses_percentages_and_plain_ratios(self) -> None:
        metrics = SimpleNamespace(
            annualized_volatility=0.1234,
            maximum_drawdown=-0.0875,
            sharpe_ratio=1.1,
            beta=0.95,
            alpha=0.0125,
            tracking_error=0.045,
            information_ratio=0.45,
        )

        values = dict(format_risk_metrics_table(metrics).to_records(index=False))

        self.assertEqual(values["Annualized Volatility"], "12.34%")
        self.assertEqual(values["Maximum Drawdown"], "-8.75%")
        self.assertEqual(values["Alpha"], "1.25%")
        self.assertEqual(values["Tracking Error"], "4.50%")
        self.assertEqual(values["Sharpe Ratio"], "1.10")
        self.assertEqual(values["Beta"], "0.95")
        self.assertEqual(values["Information Ratio"], "0.45")

    def test_performance_chart_frame_splits_seeded_and_calculated_phases(self) -> None:
        portfolio = pd.DataFrame(
            {
                "date": ["2025-01-31", "2025-02-03"],
                "cumulative_return": [0.02, 0.05],
                "phase": ["seeded", "calculated"],
            }
        )
        benchmark = pd.DataFrame(
            {
                "date": ["2025-01-31", "2025-02-03"],
                "cumulative_return": [0.01, 0.04],
                "phase": ["seeded", "calculated"],
            }
        )

        chart = build_performance_chart_frame(portfolio, benchmark)

        self.assertIn("Portfolio (seeded)", chart.columns)
        self.assertIn("Benchmark (calculated)", chart.columns)
        self.assertAlmostEqual(chart.loc[pd.Timestamp("2025-01-31"), "Portfolio (seeded)"], 0.02)
        self.assertTrue(pd.isna(chart.loc[pd.Timestamp("2025-01-31"), "Portfolio (calculated)"]))

    def test_rolling_metrics_return_empty_when_history_is_insufficient(self) -> None:
        portfolio = pd.DataFrame({"date": ["2025-02-03", "2025-02-04"], "daily_return": [0.01, 0.02]})

        rolling = calculate_rolling_metrics(portfolio_returns=portfolio, benchmark_returns=None, window=3)

        self.assertTrue(rolling.empty)

    def test_rolling_metrics_calculate_with_small_synthetic_window(self) -> None:
        portfolio = pd.DataFrame(
            {
                "date": ["2025-02-03", "2025-02-04", "2025-02-05"],
                "daily_return": [0.01, 0.02, -0.01],
            }
        )
        benchmark = pd.DataFrame(
            {
                "date": ["2025-02-03", "2025-02-04", "2025-02-05"],
                "benchmark_return": [0.005, 0.01, -0.005],
            }
        )

        rolling = calculate_rolling_metrics(portfolio, benchmark, window=2)

        self.assertFalse(rolling.empty)
        self.assertIn("rolling_sharpe", rolling.columns)
        self.assertIn("rolling_tracking_error", rolling.columns)

    def test_prepare_rolling_metrics_chart_data_preserves_latest_valid_date(self) -> None:
        rolling = pd.DataFrame(
            {
                "date": ["2025-02-03", "2025-02-04", "2025-02-05"],
                "rolling_sharpe": [0.8, 0.9, 1.1],
                "rolling_tracking_error": [0.03, 0.035, 0.04],
            }
        )

        chart_data = prepare_rolling_metrics_chart_data(rolling)

        self.assertEqual(chart_data["date"].iloc[-1].date().isoformat(), "2025-02-05")

    def test_prepare_rolling_metrics_chart_data_deduplicates_date_rows(self) -> None:
        rolling = pd.DataFrame(
            {
                "date": ["2025-02-03", "2025-02-03", "2025-02-04"],
                "rolling_sharpe": [0.8, 0.9, 1.1],
                "rolling_tracking_error": [0.03, 0.035, 0.04],
            }
        )

        chart_data = prepare_rolling_metrics_chart_data(rolling)

        self.assertEqual(chart_data["date"].dt.date.astype(str).tolist(), ["2025-02-03", "2025-02-04"])
        self.assertAlmostEqual(chart_data["rolling_sharpe"].iloc[0], 0.9)

    def test_prepare_rolling_metrics_chart_data_converts_tracking_error_to_percent_display_only(self) -> None:
        rolling = pd.DataFrame(
            {
                "date": ["2025-02-05"],
                "rolling_sharpe": [1.25],
                "rolling_tracking_error": [0.045],
            }
        )

        chart_data = prepare_rolling_metrics_chart_data(rolling)

        self.assertAlmostEqual(chart_data["rolling_tracking_error"].iloc[0], 0.045)
        self.assertAlmostEqual(chart_data["tracking_error_pct"].iloc[0], 4.5)
        self.assertEqual(chart_data["tracking_error_display"].iloc[0], "4.50%")

    def test_prepare_rolling_metrics_chart_data_handles_empty_or_insufficient_rows(self) -> None:
        self.assertTrue(prepare_rolling_metrics_chart_data(pd.DataFrame()).empty)
        all_missing = pd.DataFrame(
            {
                "date": ["2025-02-05"],
                "rolling_sharpe": [None],
                "rolling_tracking_error": [None],
            }
        )

        self.assertTrue(prepare_rolling_metrics_chart_data(all_missing).empty)

    def test_latest_rolling_metrics_extracts_latest_values(self) -> None:
        rolling = pd.DataFrame(
            {
                "date": ["2025-02-03", "2025-02-05"],
                "rolling_sharpe": [0.8, 1.1],
                "rolling_tracking_error": [0.03, 0.04],
            }
        )

        latest = latest_rolling_metrics(rolling)

        self.assertIsNotNone(latest)
        self.assertEqual(pd.Timestamp(latest["date"]).date().isoformat(), "2025-02-05")
        self.assertAlmostEqual(latest["rolling_sharpe"], 1.1)
        self.assertAlmostEqual(latest["rolling_tracking_error"], 0.04)

    def test_rolling_metrics_chart_data_does_not_forward_fill_missing_values(self) -> None:
        rolling = pd.DataFrame(
            {
                "date": ["2025-02-03", "2025-02-04"],
                "rolling_sharpe": [0.8, 0.9],
                "rolling_tracking_error": [0.03, None],
            }
        )

        chart_data = prepare_rolling_metrics_chart_data(rolling)

        self.assertTrue(pd.isna(chart_data["rolling_tracking_error"].iloc[-1]))
        self.assertTrue(pd.isna(chart_data["tracking_error_pct"].iloc[-1]))

    def test_rolling_metrics_axis_tick_dates_use_calendar_boundaries(self) -> None:
        chart_data = prepare_rolling_metrics_chart_data(
            pd.DataFrame(
                {
                    "date": ["2025-02-03", "2025-03-14", "2025-04-30"],
                    "rolling_sharpe": [0.8, 0.9, 1.0],
                    "rolling_tracking_error": [0.03, 0.035, 0.04],
                }
            )
        )

        ticks = select_rolling_metrics_axis_tick_dates(chart_data)

        self.assertEqual(ticks, [pd.Timestamp("2025-02-01"), pd.Timestamp("2025-03-01"), pd.Timestamp("2025-04-01")])

    def test_rolling_metrics_axis_monthly_labels_are_evenly_calendar_spaced(self) -> None:
        dates = pd.to_datetime(["2025-01-31", "2025-02-14", "2025-03-17", "2025-04-15"])
        chart_data = prepare_rolling_metrics_chart_data(
            pd.DataFrame(
                {
                    "date": dates,
                    "rolling_sharpe": [1.0 + index / 100 for index in range(len(dates))],
                    "rolling_tracking_error": [0.03 + index / 1000 for index in range(len(dates))],
                }
            )
        )

        ticks = select_rolling_metrics_axis_tick_dates(chart_data)
        labels = [tick.strftime("%b %Y") for tick in ticks]

        self.assertEqual(
            ticks,
            [
                pd.Timestamp("2025-01-01"),
                pd.Timestamp("2025-02-01"),
                pd.Timestamp("2025-03-01"),
                pd.Timestamp("2025-04-01"),
            ],
        )
        self.assertEqual(len(labels), len(set(labels)))

    def test_rolling_metrics_axis_does_not_force_latest_observed_date_tick(self) -> None:
        chart_data = prepare_rolling_metrics_chart_data(
            pd.DataFrame(
                {
                    "date": ["2025-02-03", "2025-03-14", "2025-04-30"],
                    "rolling_sharpe": [0.8, 0.9, 1.0],
                    "rolling_tracking_error": [0.03, 0.035, 0.04],
                }
            )
        )

        ticks = select_rolling_metrics_axis_tick_dates(chart_data)

        self.assertEqual(chart_data["date"].iloc[-1].date().isoformat(), "2025-04-30")
        self.assertNotIn(pd.Timestamp("2025-04-30"), ticks)

    def test_rolling_metrics_axis_uses_quarterly_and_annual_ticks_for_longer_history(self) -> None:
        quarterly_data = prepare_rolling_metrics_chart_data(
            pd.DataFrame(
                {
                    "date": pd.date_range("2022-02-15", periods=30, freq="MS"),
                    "rolling_sharpe": [1.0] * 30,
                    "rolling_tracking_error": [0.03] * 30,
                }
            )
        )
        annual_data = prepare_rolling_metrics_chart_data(
            pd.DataFrame(
                {
                    "date": pd.date_range("2018-02-15", periods=90, freq="MS"),
                    "rolling_sharpe": [1.0] * 90,
                    "rolling_tracking_error": [0.03] * 90,
                }
            )
        )

        quarterly_ticks = select_rolling_metrics_axis_tick_dates(quarterly_data)
        annual_ticks = select_rolling_metrics_axis_tick_dates(annual_data)

        self.assertTrue(all(tick.month in {1, 4, 7, 10} and tick.day == 1 for tick in quarterly_ticks))
        self.assertTrue(all(tick.month == 1 and tick.day == 1 for tick in annual_ticks))

    def test_rolling_metrics_chart_uses_independent_y_axes(self) -> None:
        from sma_dashboard.dashboard import _rolling_metrics_chart

        chart_data = prepare_rolling_metrics_chart_data(
            pd.DataFrame(
                {
                    "date": ["2025-02-03", "2025-02-04"],
                    "rolling_sharpe": [0.8, 0.9],
                    "rolling_tracking_error": [0.03, 0.04],
                }
            )
        )

        spec = _rolling_metrics_chart(chart_data).to_dict()

        self.assertEqual(spec["resolve"]["scale"]["y"], "independent")
        layer_specs = spec["layer"]
        axis_titles = [layer["encoding"]["y"]["title"] for layer in layer_specs]
        self.assertIn("Rolling Sharpe Ratio", axis_titles)
        self.assertIn("Tracking Error, Annualized %", axis_titles)

    def test_rolling_metrics_chart_uses_explicit_calendar_x_axis_ticks(self) -> None:
        from sma_dashboard.dashboard import _rolling_metrics_chart

        chart_data = prepare_rolling_metrics_chart_data(
            pd.DataFrame(
                {
                    "date": ["2025-02-03", "2025-02-04", "2025-02-05"],
                    "rolling_sharpe": [0.8, 0.9, 1.0],
                    "rolling_tracking_error": [0.03, 0.04, 0.05],
                }
            )
        )

        spec = _rolling_metrics_chart(chart_data).to_dict()

        axis_values = spec["layer"][0]["encoding"]["x"]["axis"]["values"]
        self.assertEqual(len(axis_values), 1)
        self.assertEqual(axis_values[0], {"year": 2025, "month": 2, "date": 1})


def _insert_trade(trade_date: str, ticker: str, action: str, weight_change: float) -> None:
    conn = sqlite3.connect(TEST_DB)
    try:
        conn.execute(
            """
            INSERT INTO trades (date, ticker, action, weight_change, notes)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (trade_date, ticker, action, weight_change),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_seeded_return(seed_date: str, return_pct: float) -> None:
    conn = sqlite3.connect(TEST_DB)
    try:
        conn.execute(
            """
            INSERT INTO seeded_returns (date, return_pct, source, notes)
            VALUES (?, ?, 'manager reported', NULL)
            """,
            (seed_date, return_pct),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_holding(snapshot_date: str, ticker: str, weight: float) -> None:
    conn = sqlite3.connect(TEST_DB)
    try:
        conn.execute(
            """
            INSERT INTO holdings (date, ticker, weight, shares, cost_basis)
            VALUES (?, ?, ?, NULL, NULL)
            """,
            (snapshot_date, ticker, weight),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_price(price_date: str, ticker: str, adj_close: float) -> None:
    conn = sqlite3.connect(TEST_DB)
    try:
        conn.execute(
            """
            INSERT INTO prices (date, ticker, close, adj_close)
            VALUES (?, ?, ?, ?)
            """,
            (price_date, ticker, adj_close, adj_close),
        )
        conn.commit()
    finally:
        conn.close()


def _performance_frame(
    dates: list[str],
    cumulative_returns: list[float],
    phases: list[str] | None = None,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": dates,
            "cumulative_return": cumulative_returns,
            "phase": phases or ["calculated"] * len(dates),
        }
    )


if __name__ == "__main__":
    unittest.main()
