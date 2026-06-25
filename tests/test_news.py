from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from sma_dashboard.db import init_db
from sma_dashboard.news import get_holding_news


TEST_DB = Path("data/db/test_news.sqlite")


class NewsTests(unittest.TestCase):
    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)

    def test_news_aggregation_sorting_and_holding_tags(self) -> None:
        _load_holdings_fixture()
        news = {
            "RY.TO": [
                {
                    "title": "Royal Bank older headline",
                    "publisher": "Wire",
                    "link": "https://example.com/ry",
                    "providerPublishTime": 1_735_689_600,
                }
            ],
            "AAPL": [
                {
                    "title": "Apple newer headline",
                    "publisher": "News Co",
                    "link": "https://example.com/aapl",
                    "providerPublishTime": 1_735_776_000,
                }
            ],
        }

        with patch("sma_dashboard.news._fetch_ticker_news", side_effect=lambda ticker: news[ticker]):
            frame = get_holding_news(TEST_DB)

        self.assertEqual(frame["title"].tolist(), ["Apple newer headline", "Royal Bank older headline"])
        self.assertEqual(frame.iloc[0]["ticker"], "AAPL")
        self.assertEqual(frame.iloc[0]["weight"], 25.0)
        self.assertAlmostEqual(frame.iloc[0]["weight_decimal"], 0.25)
        self.assertEqual(frame.iloc[0]["publisher"], "News Co")
        self.assertEqual(frame.iloc[0]["link"], "https://example.com/aapl")

    def test_malformed_or_incomplete_news_items_are_handled_gracefully(self) -> None:
        _load_holdings_fixture()
        news = {
            "RY.TO": [{"providerPublishTime": "not-a-date"}],
            "AAPL": [
                {
                    "content": {
                        "title": "Nested content headline",
                        "provider": {"displayName": "Nested Source"},
                        "canonicalUrl": {"url": "https://example.com/nested"},
                        "pubDate": "2025-01-03T12:00:00Z",
                    }
                }
            ],
        }

        with patch("sma_dashboard.news._fetch_ticker_news", side_effect=lambda ticker: news[ticker]):
            frame = get_holding_news(TEST_DB)

        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.iloc[0]["title"], "Nested content headline")
        self.assertEqual(frame.iloc[0]["publisher"], "Nested Source")
        self.assertEqual(frame.iloc[0]["link"], "https://example.com/nested")
        self.assertEqual(frame.iloc[1]["ticker"], "RY.TO")
        self.assertEqual(frame.iloc[1]["title"], "")
        self.assertTrue(pd.isna(frame.iloc[1]["published_at"]))

    def test_latest_holdings_snapshot_selection_for_news(self) -> None:
        _load_holdings_fixture()
        called: list[str] = []

        def fake_fetch(ticker: str) -> list[dict[str, object]]:
            called.append(ticker)
            return []

        with patch("sma_dashboard.news._fetch_ticker_news", side_effect=fake_fetch):
            frame = get_holding_news(TEST_DB)

        self.assertEqual(called, ["AAPL", "RY.TO"])
        self.assertEqual(
            frame.columns.tolist(),
            ["ticker", "weight", "weight_decimal", "title", "publisher", "link", "published_at"],
        )


def _load_holdings_fixture() -> None:
    init_db(TEST_DB)
    _insert_holding("2025-01-15", "OLD.TO", 100.0)
    _insert_holding("2025-02-01", "RY.TO", 75.0)
    _insert_holding("2025-02-01", "AAPL", 25.0)


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


if __name__ == "__main__":
    unittest.main()
