from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from sma_dashboard.db import init_db


TEST_DB = Path("data/db/test_schema.sqlite")


class SchemaTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)

    def test_init_db_creates_all_locked_tables(self) -> None:
        init_db(TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()

        self.assertTrue(
            {
                "holdings",
                "trades",
                "prices",
                "fx_rates",
                "seeded_returns",
                "corporate_actions",
                "transcripts",
                "rejected_rows",
            }.issubset(tables)
        )

    def test_seeded_returns_schema_includes_benchmark_return_pct(self) -> None:
        init_db(TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        try:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(seeded_returns)")
            }
        finally:
            conn.close()

        self.assertIn("benchmark_return_pct", columns)

    def test_init_db_migrates_existing_seeded_returns_table_idempotently(self) -> None:
        TEST_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(TEST_DB)
        try:
            conn.execute(
                """
                CREATE TABLE seeded_returns (
                    date TEXT PRIMARY KEY,
                    return_pct REAL NOT NULL,
                    source TEXT NOT NULL,
                    notes TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        init_db(TEST_DB)
        init_db(TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        try:
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(seeded_returns)")
            ]
        finally:
            conn.close()

        self.assertEqual(columns.count("benchmark_return_pct"), 1)

    def test_trade_sign_convention_enforced_by_schema(self) -> None:
        init_db(TEST_DB)

        conn = sqlite3.connect(TEST_DB)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO trades (date, ticker, action, weight_change)
                    VALUES ('2025-02-03', 'RY.TO', 'trim', 0.50)
                    """
                )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
