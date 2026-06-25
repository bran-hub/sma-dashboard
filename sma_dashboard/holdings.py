from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pandas as pd

from sma_dashboard.db import DEFAULT_DB_PATH, connect


class HoldingsDataError(ValueError):
    """Raised when holdings snapshots needed by M3 modules are unavailable."""


def get_latest_holdings_snapshot(
    db_path: Path | str = DEFAULT_DB_PATH,
    as_of_date: str | None = None,
) -> pd.DataFrame:
    """Return the latest holdings snapshot on or before as_of_date.

    If as_of_date is omitted, the most recent snapshot in holdings is used.
    We keep this shared query small so valuation and news do not duplicate
    database logic.
    """
    date_filter = "WHERE date <= ?" if as_of_date else ""
    params: tuple[str, ...] = (as_of_date,) if as_of_date else ()
    with closing(connect(db_path)) as conn:
        latest = conn.execute(
            f"SELECT MAX(date) FROM holdings {date_filter}",
            params,
        ).fetchone()[0]
        if latest is None:
            raise HoldingsDataError("No holdings snapshot found.")
        frame = pd.read_sql_query(
            """
            SELECT date, ticker, weight, shares, cost_basis
            FROM holdings
            WHERE date = ?
            ORDER BY ticker
            """,
            conn,
            params=(latest,),
        )
    frame["weight_decimal"] = _weights_to_decimal(frame["weight"])
    return frame


def _weights_to_decimal(weights: pd.Series) -> pd.Series:
    numeric = weights.astype(float)
    if numeric.abs().max() > 1.0 or numeric.abs().sum() > 1.5:
        return numeric / 100.0
    return numeric
