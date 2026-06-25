from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from sma_dashboard.db import init_db
from sma_dashboard.seeded_returns import load_seeded_returns_csv


TEST_DB = Path("data/db/test_seeded_returns.sqlite")
TEST_CSV = Path("data/raw/test_seeded_returns_export.csv")


class SeededReturnsLoaderTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)
        TEST_CSV.unlink(missing_ok=True)

    def test_loader_detects_header_ignores_noise_parses_and_sorts(self) -> None:
        _write_csv(
            [
                ["Portfolio Performance Report"],
                ["Generated", "metadata"],
                [],
                ["Date", "Return", "S&P/TSX Composite", "", ""],
                ["Mar 26", "3.00%", "2.00%", "", ""],
                ["Feb 25", "-1.51%", "-2.00%", "", ""],
                ["Jan 25", "2.47%", "1.25%", "", ""],
                ["Footer", "not a return", "", "", ""],
                [],
            ]
        )

        result = load_seeded_returns_csv(TEST_CSV, TEST_DB, cutoff_date="2025-01-31")

        self.assertEqual(result.rows_loaded, 1)
        self.assertEqual(result.rows_skipped_after_cutoff, 2)
        self.assertEqual(result.malformed_rows_rejected, 1)
        self.assertEqual(_seeded_rows(), [("2025-01-31", 2.47, 1.25)])

    def test_loader_loads_reverse_chronological_rows_in_ascending_order(self) -> None:
        _write_csv(
            [
                ["Metadata"],
                ["Date", "Return", "S&P/TSX Composite"],
                ["Jan 25", "2.00%", "1.00%"],
                ["Dec 24", "1.00%", "0.50%"],
            ]
        )

        result = load_seeded_returns_csv(TEST_CSV, TEST_DB, cutoff_date="2025-01-31")

        self.assertEqual(result.rows_loaded, 2)
        self.assertEqual(_seeded_rows(), [("2024-12-31", 1.0, 0.5), ("2025-01-31", 2.0, 1.0)])

    def test_loader_is_idempotent(self) -> None:
        _write_csv(
            [
                ["Date", "Return", "S&P/TSX Composite"],
                ["Jan 25", "2.47%", "1.25%"],
            ]
        )

        load_seeded_returns_csv(TEST_CSV, TEST_DB)
        load_seeded_returns_csv(TEST_CSV, TEST_DB)

        self.assertEqual(_count_seeded_rows(), 1)
        self.assertEqual(_seeded_rows(), [("2025-01-31", 2.47, 1.25)])

    def test_loader_allows_missing_seeded_benchmark_value(self) -> None:
        _write_csv(
            [
                ["Date", "Return", "S&P/TSX Composite"],
                ["Jan 25", "2.47%", ""],
            ]
        )

        result = load_seeded_returns_csv(TEST_CSV, TEST_DB)

        self.assertEqual(result.rows_loaded, 1)
        self.assertEqual(_seeded_rows(), [("2025-01-31", 2.47, None)])


def _write_csv(rows: list[list[str]]) -> None:
    TEST_CSV.parent.mkdir(parents=True, exist_ok=True)
    TEST_CSV.write_text(
        "\n".join(",".join(row) for row in rows),
        encoding="utf-8",
    )


def _seeded_rows() -> list[tuple[str, float, float | None]]:
    init_db(TEST_DB)
    conn = sqlite3.connect(TEST_DB)
    try:
        return conn.execute(
            """
            SELECT date, return_pct, benchmark_return_pct
            FROM seeded_returns
            ORDER BY date
            """
        ).fetchall()
    finally:
        conn.close()


def _count_seeded_rows() -> int:
    conn = sqlite3.connect(TEST_DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM seeded_returns").fetchone()[0]
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
