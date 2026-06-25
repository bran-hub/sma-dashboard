from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sma_dashboard.batch_ingestion import ingest_manifest, load_manifest
from sma_dashboard.db import init_db
from sma_dashboard.ingestion import IngestionResult


TEST_DB = Path("data/db/test_batch_ingestion.sqlite")
TEST_MANIFEST = Path("data/raw/test_model_updates_manifest.csv")
TEST_FILE_A = Path("data/raw/test_batch_model_a.xlsx")
TEST_FILE_B = Path("data/raw/test_batch_model_b.xlsx")


class BatchIngestionTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)
        TEST_MANIFEST.unlink(missing_ok=True)
        TEST_FILE_A.unlink(missing_ok=True)
        TEST_FILE_B.unlink(missing_ok=True)

    def test_valid_manifest_loads_files_in_ascending_model_date_order(self) -> None:
        _touch_files()
        _write_manifest(
            [
                "file,model_date,notes",
                f"{TEST_FILE_B.as_posix()},2025-03-01,later",
                f"{TEST_FILE_A.as_posix()},2025-02-01,earlier",
            ]
        )

        entries = load_manifest(TEST_MANIFEST)

        self.assertEqual([entry.model_date for entry in entries], ["2025-02-01", "2025-03-01"])
        self.assertEqual(entries[0].file, TEST_FILE_A.resolve())

    def test_missing_required_manifest_columns_fail_clearly(self) -> None:
        _write_manifest(["file,notes", f"{TEST_FILE_A.as_posix()},missing date"])

        with self.assertRaisesRegex(ValueError, "model_date"):
            load_manifest(TEST_MANIFEST)

    def test_malformed_model_date_fails_clearly(self) -> None:
        _touch_files()
        _write_manifest(["file,model_date", f"{TEST_FILE_A.as_posix()},Feb 1 2025"])

        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            load_manifest(TEST_MANIFEST)

    def test_missing_file_path_fails_clearly(self) -> None:
        _write_manifest(["file,model_date", "data/raw/does_not_exist.xlsx,2025-02-01"])

        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_manifest(TEST_MANIFEST)

    def test_duplicate_file_rows_fail_clearly(self) -> None:
        _touch_files()
        _write_manifest(
            [
                "file,model_date",
                f"{TEST_FILE_A.as_posix()},2025-02-01",
                f"{TEST_FILE_A.as_posix()},2025-03-01",
            ]
        )

        with self.assertRaisesRegex(ValueError, "Duplicate manifest file"):
            load_manifest(TEST_MANIFEST)

    def test_duplicate_model_date_rows_fail_clearly(self) -> None:
        _touch_files()
        _write_manifest(
            [
                "file,model_date",
                f"{TEST_FILE_A.as_posix()},2025-02-01",
                f"{TEST_FILE_B.as_posix()},2025-02-01",
            ]
        )

        with self.assertRaisesRegex(ValueError, "Duplicate manifest model_date"):
            load_manifest(TEST_MANIFEST)

    def test_batch_passes_model_date_to_single_file_ingestion(self) -> None:
        _touch_files()
        _write_manifest(
            [
                "file,model_date",
                f"{TEST_FILE_B.as_posix()},2025-03-01",
                f"{TEST_FILE_A.as_posix()},2025-02-01",
            ]
        )
        calls: list[tuple[Path, str]] = []

        def fake_ingest(excel_path: Path, **kwargs: object) -> IngestionResult:
            calls.append((Path(excel_path), str(kwargs["model_date"])))
            return IngestionResult(1, 1, 0, 0, 0, 0)

        with patch("sma_dashboard.batch_ingestion.ingest_model_update", side_effect=fake_ingest):
            result = ingest_manifest(TEST_MANIFEST, db_path=TEST_DB)

        self.assertEqual(calls, [(TEST_FILE_A.resolve(), "2025-02-01"), (TEST_FILE_B.resolve(), "2025-03-01")])
        self.assertEqual(result.files_processed, 2)
        self.assertEqual(result.holdings_written, 2)
        self.assertEqual(result.trades_written, 2)

    def test_skip_market_data_is_passed_through(self) -> None:
        _touch_files()
        _write_manifest(["file,model_date", f"{TEST_FILE_A.as_posix()},2025-02-01"])
        captured: dict[str, object] = {}

        def fake_ingest(excel_path: Path, **kwargs: object) -> IngestionResult:
            captured.update(kwargs)
            return IngestionResult(0, 0, 0, 0, 0, 0)

        with patch("sma_dashboard.batch_ingestion.ingest_model_update", side_effect=fake_ingest):
            ingest_manifest(TEST_MANIFEST, db_path=TEST_DB, skip_market_data=True)

        self.assertTrue(captured["skip_market_data"])

    def test_replace_date_prevents_duplicate_holdings_and_trades(self) -> None:
        init_db(TEST_DB)
        _insert_existing_model_update("2025-02-01")
        _touch_files()
        _write_manifest(["file,model_date", f"{TEST_FILE_A.as_posix()},2025-02-01"])

        def fake_ingest(excel_path: Path, **kwargs: object) -> IngestionResult:
            _insert_new_model_update(str(kwargs["model_date"]))
            return IngestionResult(1, 1, 0, 0, 0, 0)

        with patch("sma_dashboard.batch_ingestion.ingest_model_update", side_effect=fake_ingest):
            ingest_manifest(TEST_MANIFEST, db_path=TEST_DB, replace_date=True)

        self.assertEqual(_count_rows("holdings"), 1)
        self.assertEqual(_count_rows("trades"), 1)


def _write_manifest(lines: list[str]) -> None:
    TEST_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    TEST_MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _touch_files() -> None:
    TEST_FILE_A.parent.mkdir(parents=True, exist_ok=True)
    TEST_FILE_A.write_text("synthetic", encoding="utf-8")
    TEST_FILE_B.write_text("synthetic", encoding="utf-8")


def _insert_existing_model_update(model_date: str) -> None:
    with closing(sqlite3.connect(TEST_DB)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO holdings (date, ticker, weight, shares, cost_basis) VALUES (?, 'OLD.TO', 1.0, NULL, NULL)",
                (model_date,),
            )
            conn.execute(
                "INSERT INTO trades (date, ticker, action, weight_change, notes) VALUES (?, 'OLD.TO', 'buy', 1.0, NULL)",
                (model_date,),
            )


def _insert_new_model_update(model_date: str) -> None:
    with closing(sqlite3.connect(TEST_DB)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO holdings (date, ticker, weight, shares, cost_basis) VALUES (?, 'NEW.TO', 2.0, NULL, NULL)",
                (model_date,),
            )
            conn.execute(
                "INSERT INTO trades (date, ticker, action, weight_change, notes) VALUES (?, 'NEW.TO', 'buy', 2.0, NULL)",
                (model_date,),
            )


def _count_rows(table: str) -> int:
    with closing(sqlite3.connect(TEST_DB)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
