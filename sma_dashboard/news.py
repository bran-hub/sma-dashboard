from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from sma_dashboard.db import DEFAULT_DB_PATH
from sma_dashboard.holdings import get_latest_holdings_snapshot


def get_holding_news(
    db_path: Path | str = DEFAULT_DB_PATH,
    as_of_date: str | None = None,
) -> pd.DataFrame:
    """Aggregate yfinance news across the current holdings snapshot."""
    holdings = get_latest_holdings_snapshot(db_path, as_of_date)
    rows: list[dict[str, Any]] = []
    for holding in holdings.itertuples(index=False):
        for item in _fetch_ticker_news(holding.ticker) or []:
            rows.append(_normalize_news_item(item, holding.ticker, float(holding.weight), float(holding.weight_decimal)))
    if not rows:
        return pd.DataFrame(
            columns=["ticker", "weight", "weight_decimal", "title", "publisher", "link", "published_at"]
        )
    frame = pd.DataFrame(rows)
    frame["_sort_date"] = pd.to_datetime(frame["published_at"], errors="coerce", utc=True)
    frame = frame.sort_values("_sort_date", ascending=False, na_position="last")
    return frame.drop(columns=["_sort_date"]).reset_index(drop=True)


def _fetch_ticker_news(ticker: str) -> list[dict[str, Any]]:
    import yfinance as yf

    return yf.Ticker(ticker).news or []


def _normalize_news_item(item: dict[str, Any], ticker: str, weight: float, weight_decimal: float) -> dict[str, Any]:
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    return {
        "ticker": ticker,
        "weight": weight,
        "weight_decimal": weight_decimal,
        "title": _first_present(item, content, "title", "headline") or "",
        "publisher": _first_present(item, content, "publisher", "provider", "source") or None,
        "link": _extract_link(item, content),
        "published_at": _extract_published_at(item, content),
    }


def _first_present(primary: dict[str, Any], secondary: dict[str, Any], *keys: str) -> Any | None:
    for source in (primary, secondary):
        for key in keys:
            value = source.get(key)
            if value:
                if isinstance(value, dict):
                    return value.get("displayName") or value.get("name")
                return value
    return None


def _extract_link(primary: dict[str, Any], secondary: dict[str, Any]) -> str | None:
    for source in (primary, secondary):
        for key in ("link", "url", "canonicalUrl"):
            value = source.get(key)
            if isinstance(value, dict):
                nested = value.get("url")
                if nested:
                    return nested
            elif value:
                return value
    return None


def _extract_published_at(primary: dict[str, Any], secondary: dict[str, Any]) -> str | None:
    value = _first_present(primary, secondary, "providerPublishTime", "pubDate", "publishedAt", "displayTime")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    try:
        return pd.to_datetime(value, utc=True).isoformat()
    except (TypeError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch recent yfinance news for current SMA holdings.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--as-of-date")
    args = parser.parse_args()
    print(get_holding_news(args.db_path, args.as_of_date).to_string(index=False))


if __name__ == "__main__":
    main()
