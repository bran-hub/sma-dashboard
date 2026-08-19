"""Tests for M5 chatbot tool executors (chatbot.py).

These tests exercise the tool executor functions directly — they don't call
the Anthropic API (no mocking of the agent loop). The agent loop itself is
tested by verifying that tool executors return the expected shapes and that
TOOL_DEFINITIONS match SPEC.md §5.
"""

from __future__ import annotations

import gc
import sqlite3
import unittest
from pathlib import Path

from sma_dashboard.chatbot import (
    TOOL_DEFINITIONS,
    execute_get_holdings,
    execute_get_performance,
    execute_get_trades,
    execute_get_valuation,
    execute_render_chart,
    execute_search_transcripts,
    run_mock_agent_loop,
)
from sma_dashboard.db import init_db
from sma_dashboard.transcripts import load_transcript


TEST_DB_DIR = Path("data/db")
TEST_INPUT_DIR = Path("data/raw")


def _fresh_test_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.unlink(missing_ok=True)
    return path


def _insert_holding(db_path: Path, date: str, ticker: str, weight: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO holdings (date, ticker, weight) VALUES (?, ?, ?)",
            (date, ticker, weight),
        )


def _insert_price(db_path: Path, date: str, ticker: str, price: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO prices (date, ticker, close, adj_close) VALUES (?, ?, ?, ?)",
            (date, ticker, price, price),
        )


def _insert_seeded_return(db_path: Path, date: str, return_pct: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO seeded_returns (date, return_pct, benchmark_return_pct, source) VALUES (?, ?, ?, ?)",
            (date, return_pct, 0.0, "test"),
        )


def _insert_trade(
    db_path: Path, date: str, ticker: str, action: str, weight_change: float
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO trades (date, ticker, action, weight_change) VALUES (?, ?, ?, ?)",
            (date, ticker, action, weight_change),
        )


class ToolDefinitionsTests(unittest.TestCase):
    """Verify TOOL_DEFINITIONS match SPEC.md §5 exactly."""

    def _tool(self, name: str) -> dict:
        for t in TOOL_DEFINITIONS:
            if t.get("name") == name:
                return t
        self.fail(f"Tool '{name}' not found in TOOL_DEFINITIONS")

    def test_all_tools_present(self) -> None:
        names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "get_holdings",
            "get_performance",
            "get_valuation",
            "get_trades",
            "web_search",
            "search_transcripts",
            "render_chart",
        }
        self.assertEqual(names, expected)

    def test_web_search_is_builtin_type(self) -> None:
        ws = self._tool("web_search")
        self.assertEqual(ws.get("type"), "web_search_20250305")

    def test_get_performance_has_period_enum(self) -> None:
        tool = self._tool("get_performance")
        period_prop = tool["input_schema"]["properties"]["period"]
        self.assertIn("since_inception", period_prop["enum"])
        self.assertIn("custom", period_prop["enum"])

    def test_render_chart_required_fields(self) -> None:
        tool = self._tool("render_chart")
        required = tool["input_schema"].get("required", [])
        self.assertIn("chart_type", required)
        self.assertIn("title", required)
        self.assertIn("data", required)

    def test_get_valuation_metrics_enum(self) -> None:
        tool = self._tool("get_valuation")
        items = tool["input_schema"]["properties"]["metrics"]["items"]
        self.assertIn("pe_trailing", items["enum"])
        self.assertIn("market_cap", items["enum"])

    def test_get_trades_action_enum(self) -> None:
        tool = self._tool("get_trades")
        action_enum = tool["input_schema"]["properties"]["action"]["enum"]
        self.assertEqual(set(action_enum), {"buy", "sell", "trim", "add"})


class ExecuteGetHoldingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = _fresh_test_path(TEST_DB_DIR, "test_chatbot_holdings.db")
        self.empty_db_path = _fresh_test_path(TEST_DB_DIR, "test_chatbot_empty.db")
        init_db(self.db_path)
        _insert_holding(self.db_path, "2025-03-15", "RY.TO", 15.0)
        _insert_holding(self.db_path, "2025-03-15", "TD.TO", 12.0)

    def tearDown(self) -> None:
        gc.collect()
        self.db_path.unlink(missing_ok=True)
        self.empty_db_path.unlink(missing_ok=True)

    def test_returns_holdings(self) -> None:
        result = execute_get_holdings(self.db_path)
        self.assertNotIn("error", result)
        self.assertEqual(result["count"], 2)
        tickers = [h["ticker"] for h in result["holdings"]]
        self.assertIn("RY.TO", tickers)

    def test_filter_by_ticker(self) -> None:
        result = execute_get_holdings(self.db_path, ticker="RY.TO")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["holdings"][0]["ticker"], "RY.TO")

    def test_unknown_ticker_returns_error(self) -> None:
        result = execute_get_holdings(self.db_path, ticker="FAKE.TO")
        self.assertIn("error", result)

    def test_empty_db_returns_error(self) -> None:
        init_db(self.empty_db_path)
        result = execute_get_holdings(self.empty_db_path)
        self.assertIn("error", result)


class ExecuteGetTradesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = _fresh_test_path(TEST_DB_DIR, "test_chatbot_trades.db")
        init_db(self.db_path)
        _insert_trade(self.db_path, "2025-01-10", "RY.TO", "buy", 0.15)
        _insert_trade(self.db_path, "2025-02-20", "TD.TO", "trim", -0.05)
        _insert_trade(self.db_path, "2025-03-01", "SHOP.TO", "add", 0.03)

    def tearDown(self) -> None:
        gc.collect()
        self.db_path.unlink(missing_ok=True)

    def test_returns_all_trades(self) -> None:
        result = execute_get_trades(self.db_path)
        self.assertEqual(result["count"], 3)

    def test_limit(self) -> None:
        result = execute_get_trades(self.db_path, limit=2)
        self.assertEqual(result["count"], 2)

    def test_filter_by_ticker(self) -> None:
        result = execute_get_trades(self.db_path, ticker="RY.TO")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["trades"][0]["ticker"], "RY.TO")

    def test_filter_by_action(self) -> None:
        result = execute_get_trades(self.db_path, action="buy")
        self.assertEqual(result["count"], 1)

    def test_empty_result(self) -> None:
        result = execute_get_trades(self.db_path, ticker="NOTEXIST.TO")
        self.assertEqual(result["count"], 0)


class ExecuteSearchTranscriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = _fresh_test_path(TEST_DB_DIR, "test_chatbot_transcripts.db")
        init_db(self.db_path)
        txt = _fresh_test_path(TEST_INPUT_DIR, "2025_Q1_call.txt")
        self.transcript_path = txt
        txt.write_text("Q1 content about tech.", encoding="utf-8")
        load_transcript(txt, db_path=self.db_path, call_date="2025-03-28")

    def tearDown(self) -> None:
        gc.collect()
        self.db_path.unlink(missing_ok=True)
        self.transcript_path.unlink(missing_ok=True)

    def test_returns_most_recent(self) -> None:
        result = execute_search_transcripts(self.db_path)
        self.assertEqual(result["count"], 1)
        self.assertIn("Q1 content", result["transcripts"][0]["full_text"])

    def test_filter_by_quarter(self) -> None:
        result = execute_search_transcripts(self.db_path, quarter_label="2025_Q1")
        self.assertEqual(result["count"], 1)

    def test_no_match_returns_error(self) -> None:
        result = execute_search_transcripts(self.db_path, quarter_label="2024_Q4")
        self.assertIn("error", result)


class ExecuteRenderChartTests(unittest.TestCase):
    def test_line_chart(self) -> None:
        data = {"x": ["2025-01", "2025-02"], "series": [{"name": "Portfolio", "values": [1.0, 1.05]}]}
        result = execute_render_chart("line", "Portfolio Growth", data)
        self.assertEqual(result["chart_type"], "line")
        self.assertEqual(result["title"], "Portfolio Growth")
        self.assertEqual(result["data"], data)

    def test_table(self) -> None:
        data = {"columns": ["Ticker", "Weight"], "rows": [["RY.TO", 15.0]]}
        result = execute_render_chart("table", "Holdings", data)
        self.assertEqual(result["chart_type"], "table")

    def test_bar_chart(self) -> None:
        data = {"x": ["RY.TO", "TD.TO"], "series": [{"name": "Weight", "values": [15.0, 12.0]}]}
        result = execute_render_chart("bar", "Weights", data)
        self.assertEqual(result["chart_type"], "bar")


class ExecuteGetPerformanceTests(unittest.TestCase):
    """Performance tool executor — minimal smoke test without live market data."""

    def setUp(self) -> None:
        self.db_path = _fresh_test_path(TEST_DB_DIR, "test_chatbot_performance.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        gc.collect()
        self.db_path.unlink(missing_ok=True)

    def test_returns_error_without_data(self) -> None:
        # No seeded returns or holdings — should return error dict, not raise
        result = execute_get_performance(self.db_path)
        self.assertIn("error", result)

    def test_with_seeded_data(self) -> None:
        _insert_seeded_return(self.db_path, "2025-01-31", 1.5)
        result = execute_get_performance(self.db_path, period="since_inception")
        # May fail on benchmark but should at least return portfolio_twr
        self.assertIn("portfolio_twr", result)


class MockAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = _fresh_test_path(TEST_DB_DIR, "test_chatbot_mock.db")
        init_db(self.db_path)
        _insert_holding(self.db_path, "2025-03-15", "RY.TO", 60.0)
        _insert_holding(self.db_path, "2025-03-15", "AAPL", 30.0)
        _insert_trade(self.db_path, "2025-03-15", "RY.TO", "add", 0.05)
        self.transcript_path = _fresh_test_path(TEST_INPUT_DIR, "2025_Q1_call.txt")
        self.transcript_path.write_text(
            "SYNTHETIC commentary about portfolio positioning.",
            encoding="utf-8",
        )
        load_transcript(self.transcript_path, db_path=self.db_path, call_date="2025-03-28")

    def tearDown(self) -> None:
        gc.collect()
        self.db_path.unlink(missing_ok=True)
        self.transcript_path.unlink(missing_ok=True)

    def test_holdings_question_returns_local_chart(self) -> None:
        response, history = run_mock_agent_loop(
            "What are the current holdings?", [], self.db_path
        )
        self.assertIn("2", response.text)
        self.assertEqual(response.charts[0]["chart_type"], "bar")
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])

    def test_transcript_question_returns_synthetic_commentary(self) -> None:
        response, _ = run_mock_agent_loop(
            "What did the manager say last quarter?", [], self.db_path
        )
        self.assertIn("SYNTHETIC commentary", response.text)

    def test_trade_question_returns_table(self) -> None:
        response, _ = run_mock_agent_loop("Show recent trades", [], self.db_path)
        self.assertEqual(response.charts[0]["chart_type"], "table")


if __name__ == "__main__":
    unittest.main()
