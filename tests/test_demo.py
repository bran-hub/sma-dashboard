from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path

from streamlit.testing.v1 import AppTest

import sma_dashboard.dashboard as dashboard_module
import sma_dashboard.dashboard_support as dashboard_support_module
from sma_dashboard.demo import build_demo_database
from sma_dashboard.performance import calculate_daily_portfolio_returns, calculate_rolling_metrics


TEST_DB = Path("data/db/test_demo.db")


class DemoDatabaseTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)

    def test_demo_database_builds_offline_with_dashboard_history(self) -> None:
        path = build_demo_database(TEST_DB)
        conn = sqlite3.connect(path)
        try:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("seeded_returns", "holdings", "trades", "prices", "fx_rates", "transcripts")
            }
        finally:
            conn.close()
        self.assertGreaterEqual(counts["seeded_returns"], 12)
        self.assertGreaterEqual(counts["holdings"], 16)
        self.assertGreater(counts["trades"], 0)
        self.assertGreater(counts["prices"], 1000)
        self.assertGreater(counts["fx_rates"], 250)
        self.assertEqual(counts["transcripts"], 1)

        returns = calculate_daily_portfolio_returns(path)
        rolling = calculate_rolling_metrics(returns, db_path=path, window=252)
        self.assertGreater(len(returns), 300)
        self.assertFalse(rolling.empty)

    def test_demo_dashboard_renders_without_live_services(self) -> None:
        path = build_demo_database(TEST_DB).resolve()
        with (
            patch.object(dashboard_module, "DASHBOARD_DEFAULT_DB_PATH", path),
            patch.object(dashboard_support_module, "DASHBOARD_DEFAULT_DB_PATH", path),
            patch.dict(
                "os.environ",
                {"SMA_DEMO_MODE": "1", "SMA_ENABLE_CHAT": "1", "SMA_CHAT_MODE": "mock"},
                clear=False,
            ),
        ):
            dashboard_path = Path(__file__).resolve().parents[1] / "dashboard.py"
            app = AppTest.from_file(dashboard_path).run(timeout=60)

        self.assertEqual([str(item.value) for item in app.exception], [])
        self.assertIn("SMA Dashboard", [item.value for item in app.title])
        self.assertGreaterEqual(len(app.dataframe), 6)
        self.assertTrue(
            any("Offline mock mode" in item.value for item in app.caption),
            "The synthetic dashboard should expose the no-key assistant mode.",
        )
