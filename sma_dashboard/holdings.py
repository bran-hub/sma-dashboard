from __future__ import annotations

import sqlite3
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
            SELECT date, ticker, weight, currency, shares, cost_basis
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


def log_model_update_applied(
    db_path: Path | str,
    model_date: str,
    file_name: str | None = None,
    source: str | None = None,
    ingested_by: str | None = None,
    notes: str | None = None,
) -> None:
    """Log a successfully applied model update.

    Raises:
        HoldingsDataError: If the model date is already logged.
    """
    try:
        with closing(connect(db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO model_updates_applied
                        (model_date, file_name, source, ingested_by, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (model_date, file_name, source, ingested_by, notes),
                )
    except sqlite3.IntegrityError as exc:
        raise HoldingsDataError(
            f"Model date {model_date} has already been applied. "
            "Use --replace-date to override."
        ) from exc


def ensure_model_update_not_applied(
    db_path: Path | str,
    model_date: str,
) -> None:
    """Raise before ingestion if a model date is already in the audit trail."""
    with closing(connect(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM model_updates_applied WHERE model_date = ? LIMIT 1",
            (model_date,),
        ).fetchone()
    if exists is not None:
        raise HoldingsDataError(
            f"Model date {model_date} has already been applied. "
            "Use --replace-date to override."
        )


def get_model_updates_applied(
    db_path: Path | str,
) -> pd.DataFrame:
    """Return applied model updates ordered from newest model date to oldest."""
    with closing(connect(db_path)) as conn:
        return pd.read_sql_query(
            "SELECT * FROM model_updates_applied ORDER BY model_date DESC",
            conn,
            parse_dates=["ingested_at"],
        )


def get_pending_model_updates(
    db_path: Path | str,
    manifest_df: pd.DataFrame,
) -> pd.DataFrame:
    """Return manifest rows whose model dates have not been applied."""
    applied = get_model_updates_applied(db_path)
    applied_dates = set(applied["model_date"].astype(str))
    manifest_dates = manifest_df["model_date"].astype(str)
    return manifest_df[~manifest_dates.isin(applied_dates)].copy()
