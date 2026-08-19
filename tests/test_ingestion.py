from __future__ import annotations

import unittest
import sqlite3
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from sma_dashboard.db import init_db
from sma_dashboard.ingestion import (
    MarketDataRefreshError,
    _parse_row,
    ingest_model_update,
    is_usd_listed,
    normalize_ticker,
    refresh_market_data,
    ticker_currency,
)
from sma_dashboard.holdings import HoldingsDataError


MAPPING = {
    "columns": {
        "date": "Date",
        "ticker": "Ticker",
        "weight": "Weight",
        "shares": "Shares",
        "cost_basis": "Cost Basis",
        "action": "Action",
        "weight_change": "Weight Change",
        "notes": "Notes",
    },
    "weight_input": "percent",
    "weight_change_input": "percent",
}

TEST_DB = Path("data/db/test_ingestion.sqlite")
TEST_XLSX = Path("data/raw/test_model_update.xlsx")
TEST_MAPPING = Path("data/raw/test_column_mapping.json")
TEST_OVERRIDES = Path("data/raw/test_ticker_overrides.json")


class IngestionParsingTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)
        TEST_XLSX.unlink(missing_ok=True)
        TEST_MAPPING.unlink(missing_ok=True)
        TEST_OVERRIDES.unlink(missing_ok=True)

    def test_parse_valid_holding_and_trade_row(self) -> None:
        row = pd.Series(
            {
                "Date": "2025-02-03",
                "Ticker": "shop.to",
                "Weight": "4.5%",
                "Shares": 10,
                "Cost Basis": 1200,
                "Action": "add",
                "Weight Change": 1.25,
                "Notes": "Manager added.",
            }
        )

        holding, trade = _parse_row(row, MAPPING, None)

        self.assertEqual(holding, ("2025-02-03", "SHOP.TO", 4.5, "CAD", 10.0, 1200.0))
        self.assertEqual(trade, ("2025-02-03", "SHOP.TO", "add", 1.25, "Manager added."))

    def test_parse_rejects_action_sign_mismatch(self) -> None:
        row = pd.Series(
            {
                "Date": "2025-02-03",
                "Ticker": "RY.TO",
                "Weight": 5.0,
                "Action": "trim",
                "Weight Change": 0.5,
            }
        )

        with self.assertRaisesRegex(ValueError, "requires a negative"):
            _parse_row(row, MAPPING, None)

    def test_parse_uses_model_date_when_date_column_missing(self) -> None:
        row = pd.Series({"Ticker": "AAPL", "Weight": 3.0})

        holding, trade = _parse_row(row, MAPPING, "2025-02-03")

        self.assertEqual(holding[0], "2025-02-03")
        self.assertEqual(holding[1], "AAPL")
        self.assertIsNone(trade)

    def test_usd_detection_uses_locked_suffix_rule(self) -> None:
        self.assertTrue(is_usd_listed("AAPL"))
        self.assertFalse(is_usd_listed("SHOP.TO"))
        self.assertFalse(is_usd_listed("ABC.V"))
        self.assertFalse(is_usd_listed("ABC.NE"))
        self.assertFalse(is_usd_listed("ABC.CN"))
        with self.assertRaisesRegex(ValueError, "Currency is ambiguous"):
            ticker_currency("VOD.L")
        self.assertEqual(ticker_currency("VOD.L", {"VOD.L": "USD"}), "USD")

    def test_duplicate_ingestion_rejected_and_replace_is_idempotent(self) -> None:
        frame = pd.DataFrame([{"Date": "2025-02-03", "Ticker": "RY.TO", "Weight": 5.0, "Action": "buy", "Weight Change": 5.0}])
        with (
            patch("sma_dashboard.ingestion.pd.read_excel", return_value=frame),
            patch("sma_dashboard.ingestion.load_column_mapping", return_value=MAPPING),
        ):
            ingest_model_update(TEST_XLSX, db_path=TEST_DB, skip_market_data=True)
            with self.assertRaisesRegex(HoldingsDataError, "already been applied"):
                ingest_model_update(TEST_XLSX, db_path=TEST_DB, skip_market_data=True)
            ingest_model_update(TEST_XLSX, db_path=TEST_DB, skip_market_data=True, replace_date=True)

        conn = sqlite3.connect(TEST_DB)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_updates_applied").fetchone()[0], 1)
        finally:
            conn.close()

    def test_market_data_failure_leaves_no_partial_model_update(self) -> None:
        frame = pd.DataFrame([{"Date": "2025-02-03", "Ticker": "RY.TO", "Weight": 100.0}])
        with (
            patch("sma_dashboard.ingestion.pd.read_excel", return_value=frame),
            patch("sma_dashboard.ingestion.load_column_mapping", return_value=MAPPING),
            patch("sma_dashboard.ingestion._collect_market_data", side_effect=RuntimeError("network down")),
        ):
            with self.assertRaisesRegex(RuntimeError, "network down"):
                ingest_model_update(TEST_XLSX, db_path=TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_updates_applied").fetchone()[0], 0)
        finally:
            conn.close()

    def test_ingest_writes_malformed_rows_to_rejected_rows(self) -> None:
        frame = pd.DataFrame(
            [
                {"Date": "2025-02-03", "Ticker": "RY.TO", "Weight": 5.0},
                {"Date": "2025-02-03", "Ticker": "", "Weight": 3.0},
            ]
        )

        with (
            patch("sma_dashboard.ingestion.pd.read_excel", return_value=frame),
            patch("sma_dashboard.ingestion.load_column_mapping", return_value=MAPPING),
        ):
            result = ingest_model_update(
                TEST_XLSX,
                db_path=TEST_DB,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 1)
        self.assertEqual(result.rejected_rows, 1)

        conn = sqlite3.connect(TEST_DB)
        try:
            rejected = conn.execute(
                "SELECT source_file, reason FROM rejected_rows"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(rejected[0], str(TEST_XLSX))
        self.assertIn("ticker", rejected[1])

    def test_invalid_dates_are_rejected_not_raised(self) -> None:
        frame = pd.DataFrame([{"Date": "not-a-date", "Ticker": "RY.TO", "Weight": 5.0}])

        with (
            patch("sma_dashboard.ingestion.pd.read_excel", return_value=frame),
            patch("sma_dashboard.ingestion.load_column_mapping", return_value=MAPPING),
        ):
            result = ingest_model_update(
                TEST_XLSX,
                db_path=TEST_DB,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 0)
        self.assertEqual(result.rejected_rows, 1)

    def test_usd_market_data_stores_raw_prices_and_fx_rates(self) -> None:
        init_db(TEST_DB)
        price_frame = pd.DataFrame(
            {"Close": [100.0], "Adj Close": [98.0]},
            index=pd.to_datetime(["2025-02-03"]),
        )
        fx_frame = pd.DataFrame(
            {"Close": [0.70]},
            index=pd.to_datetime(["2025-02-03"]),
        )

        with (
            patch("sma_dashboard.ingestion._download_price_frame", return_value=price_frame),
            patch("sma_dashboard.ingestion._download_fx_frame", return_value=fx_frame),
            patch("sma_dashboard.ingestion._tomorrow_iso", return_value="2025-02-04"),
        ):
            prices_written, fx_written = refresh_market_data(
                ["AAPL"],
                db_path=TEST_DB,
                fallback_start_date="2025-02-03",
            )

        self.assertEqual(prices_written, 1)
        self.assertEqual(fx_written, 1)

        conn = sqlite3.connect(TEST_DB)
        try:
            price = conn.execute(
                "SELECT close, adj_close FROM prices WHERE ticker = 'AAPL'"
            ).fetchone()
            fx = conn.execute(
                "SELECT pair, rate FROM fx_rates WHERE pair = 'CADUSD=X'"
            ).fetchone()
        finally:
            conn.close()

        self.assertAlmostEqual(price[0], 100.0)
        self.assertAlmostEqual(price[1], 98.0)
        self.assertEqual(fx, ("CADUSD=X", 0.70))

    def test_new_ticker_backfills_from_since_inception_date(self) -> None:
        init_db(TEST_DB)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute(
                """
                INSERT INTO seeded_returns (date, return_pct, source, notes)
                VALUES ('2025-01-31', 1.0, 'manager reported', NULL)
                """
            )
            conn.commit()
        finally:
            conn.close()

        captured: dict[str, str] = {}

        def fake_download_price(ticker: str, start: str, end: str) -> pd.DataFrame:
            captured["ticker"] = ticker
            captured["start"] = start
            captured["end"] = end
            return pd.DataFrame(
                {"Close": [50.0], "Adj Close": [50.0]},
                index=pd.to_datetime(["2025-02-03"]),
            )

        with (
            patch("sma_dashboard.ingestion._download_price_frame", side_effect=fake_download_price),
            patch("sma_dashboard.ingestion._tomorrow_iso", return_value="2025-02-04"),
        ):
            refresh_market_data(
                ["RY.TO"],
                db_path=TEST_DB,
                fallback_start_date="2025-02-03",
            )

        self.assertEqual(captured["ticker"], "RY.TO")
        self.assertEqual(captured["start"], "2025-01-31")
        self.assertEqual(captured["end"], "2025-02-04")

    def test_empty_price_download_fails_clearly_after_retry(self) -> None:
        init_db(TEST_DB)

        with (
            patch("sma_dashboard.ingestion._download_price_frame", return_value=pd.DataFrame()) as download,
            patch("sma_dashboard.ingestion._tomorrow_iso", return_value="2025-02-04"),
        ):
            with self.assertRaisesRegex(MarketDataRefreshError, "BAD.TO"):
                refresh_market_data(
                    ["BAD.TO"],
                    db_path=TEST_DB,
                    fallback_start_date="2025-02-03",
                )

        self.assertEqual(download.call_count, 2)

    def test_empty_then_successful_price_download_retry_stores_prices(self) -> None:
        init_db(TEST_DB)
        price_frame = pd.DataFrame(
            {"Close": [100.0], "Adj Close": [100.0]},
            index=pd.to_datetime(["2025-02-03"]),
        )

        with (
            patch("sma_dashboard.ingestion._download_price_frame", side_effect=[pd.DataFrame(), price_frame]),
            patch("sma_dashboard.ingestion._tomorrow_iso", return_value="2025-02-04"),
        ):
            prices_written, fx_written = refresh_market_data(
                ["RY.TO"],
                db_path=TEST_DB,
                fallback_start_date="2025-02-03",
            )

        self.assertEqual(prices_written, 1)
        self.assertEqual(fx_written, 0)

    def test_model_update_date_provided_by_model_date(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame([["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "1.0%", "BUY"]])

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            result = ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 1)
        self.assertEqual(_holdings(), [("2025-02-03", "RY.TO", 5.0)])

    def test_model_update_date_derived_from_filename(self) -> None:
        _write_manager_mapping()
        dated_file = Path("data/raw/Manager - Model_Changes_2024-12-19.xlsx")
        raw = _manager_raw_frame([["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "1.0%", "BUY"]])

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            result = ingest_model_update(
                dated_file,
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 1)
        self.assertEqual(_holdings(), [("2024-12-19", "RY.TO", 5.0)])

    def test_missing_model_date_fails_clearly(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame([["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "1.0%", "BUY"]])

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            with self.assertRaisesRegex(ValueError, "Missing model update date"):
                ingest_model_update(
                    TEST_XLSX,
                    db_path=TEST_DB,
                    mapping_path=TEST_MAPPING,
                    skip_market_data=True,
                )

    def test_header_detection_metadata_column_mapping_and_footer_ignored(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame(
            [
                ["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "1.0%", "BUY"],
                ["TOTAL", None, None, "100.0%", None, None, None],
                [None, None, None, "100.00", None, None, None],
                [None, None, None, None, None, None, None],
            ]
        )

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            result = ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 1)
        self.assertEqual(result.trades_written, 1)
        self.assertEqual(result.rejected_rows, 0)
        self.assertEqual(_holdings(), [("2025-02-03", "RY.TO", 5.0)])

    def test_bloomberg_ticker_normalization_and_overrides(self) -> None:
        self.assertEqual(normalize_ticker("RY CN"), "RY.TO")
        self.assertEqual(normalize_ticker("CCL/B CN"), "CCL-B.TO")
        self.assertEqual(normalize_ticker("EMP/A CN"), "EMP-A.TO")
        self.assertEqual(normalize_ticker("TOI CN", {"TOI CN": "TOI.V"}), "TOI.V")

    def test_ticker_override_file_is_used(self) -> None:
        _write_manager_mapping()
        TEST_OVERRIDES.write_text(json.dumps({"TOI CN": "TOI.V"}), encoding="utf-8")
        raw = _manager_raw_frame([["Topicus", "TOI CN", "CUSIP1", "2.0%", "0.0%", "2.0%", "BUY"]])

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                ticker_overrides_path=TEST_OVERRIDES,
                skip_market_data=True,
            )

        self.assertEqual(_holdings(), [("2025-02-03", "TOI.V", 2.0)])

    def test_cash_rows_are_skipped_counted_and_weights_not_renormalized(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame(
            [
                ["Royal Bank", "RY CN", "CUSIP1", "60.0%", "55.0%", "5.0%", "BUY"],
                ["CANADIAN DOLLAR", "17", "17", "40.0%", None, None, "17"],
            ]
        )

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            result = ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 1)
        self.assertEqual(result.skipped_cash_rows, 1)
        self.assertEqual(result.rejected_rows, 0)
        self.assertEqual(_holdings(), [("2025-02-03", "RY.TO", 60.0)])

    def test_blank_numeric_and_unrecognized_instructions_create_no_trade(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame(
            [
                ["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "1.0%", ""],
                ["CCL", "CCL/B CN", "CUSIP2", "3.0%", "2.0%", "1.0%", "17"],
                ["Empire", "EMP/A CN", "CUSIP3", "2.0%", "1.0%", "1.0%", "HOLD"],
            ]
        )

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            result = ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 3)
        self.assertEqual(result.trades_written, 0)

    def test_buy_and_sell_create_valid_trades(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame(
            [
                ["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "1.0%", "BUY"],
                ["CCL", "CCL/B CN", "CUSIP2", "3.0%", "4.0%", "-1.0%", "SELL"],
            ]
        )

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            result = ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                skip_market_data=True,
            )

        self.assertEqual(result.trades_written, 2)
        self.assertEqual(_trades(), [("RY.TO", "buy", 1.0), ("CCL-B.TO", "sell", -1.0)])

    def test_action_sign_mismatch_goes_to_rejected_rows(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame([["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "-1.0%", "BUY"]])

        with patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw):
            result = ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
                skip_market_data=True,
            )

        self.assertEqual(result.holdings_written, 0)
        self.assertEqual(result.rejected_rows, 1)
        self.assertIn("positive weight_change", _rejected_reason())

    def test_market_data_fetch_uses_normalized_security_tickers_only(self) -> None:
        _write_manager_mapping()
        raw = _manager_raw_frame(
            [
                ["Royal Bank", "RY CN", "CUSIP1", "5.0%", "4.0%", "1.0%", "BUY"],
                ["CANADIAN DOLLAR", "17", "17", "95.0%", None, None, None],
            ]
        )
        captured: dict[str, list[str]] = {}

        def fake_collect(tickers: list[str], db_path: Path, update_date: str, currencies: dict[str, str]):
            captured["tickers"] = tickers
            captured["date"] = [update_date]
            from sma_dashboard.ingestion import MarketDataRows
            return MarketDataRows([], [])

        with (
            patch("sma_dashboard.ingestion.pd.read_excel", return_value=raw),
            patch("sma_dashboard.ingestion._collect_market_data", side_effect=fake_collect),
        ):
            result = ingest_model_update(
                TEST_XLSX,
                model_date="2025-02-03",
                db_path=TEST_DB,
                mapping_path=TEST_MAPPING,
            )

        self.assertEqual(result.skipped_cash_rows, 1)
        self.assertEqual(captured["tickers"], ["RY.TO"])


def _write_manager_mapping() -> None:
    TEST_MAPPING.write_text(
        json.dumps(
            {
                "sheet_name": 0,
                "required_headers": ["Security Description", "Ticker", "NEW WEIGHT"],
                "columns": {
                    "security_description": "Security Description",
                    "raw_ticker": "Ticker",
                    "cusip": "CUSIP",
                    "weight": "NEW WEIGHT",
                    "previous_weight": "PREVIOUS WEIGHT",
                    "weight_change": "Changes",
                    "action": "INSTRUCTION",
                },
                "weight_input": "percent",
                "weight_change_input": "percent",
            }
        ),
        encoding="utf-8",
    )


def _manager_raw_frame(data_rows: list[list[object]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["Manager Model Update", None, None, None, None, None, None],
            ["Generated by export", None, None, None, None, None, None],
            ["Security Description", "Ticker", "CUSIP", "NEW WEIGHT", "PREVIOUS WEIGHT", "Changes", "INSTRUCTION"],
            *data_rows,
        ]
    )


def _holdings() -> list[tuple[str, str, float]]:
    conn = sqlite3.connect(TEST_DB)
    try:
        return conn.execute(
            "SELECT date, ticker, weight FROM holdings ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()


def _trades() -> list[tuple[str, str, float]]:
    conn = sqlite3.connect(TEST_DB)
    try:
        return conn.execute(
            "SELECT ticker, action, weight_change FROM trades ORDER BY ticker DESC"
        ).fetchall()
    finally:
        conn.close()


def _rejected_reason() -> str:
    conn = sqlite3.connect(TEST_DB)
    try:
        return conn.execute("SELECT reason FROM rejected_rows").fetchone()[0]
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
