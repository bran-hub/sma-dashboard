"""M5: Transcript storage and retrieval for quarterly manager call transcripts.

Transcripts are stored as full text in the SQLite `transcripts` table. No
chunking or embeddings — naive recency/quarter/date filter only (v1). Full
text is passed to Claude as context.

Filename convention: YYYY_QN_call.txt (e.g. 2025_Q1_call.txt). The loader
parses quarter_label from the filename; the call date is supplied separately
since quarter-end naming doesn't capture the specific call date.
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from sma_dashboard.db import DEFAULT_DB_PATH, connect


@dataclass(frozen=True)
class Transcript:
    id: int
    date: str
    quarter_label: str
    full_text: str
    notes: str | None


def load_transcript(
    file_path: Path | str,
    db_path: Path | str = DEFAULT_DB_PATH,
    call_date: str | None = None,
    notes: str | None = None,
) -> Transcript:
    """Parse filename for quarter_label, read text, insert into DB.

    Args:
        file_path: Path to a .txt file named YYYY_QN_call.txt.
        db_path: SQLite database path.
        call_date: ISO date (YYYY-MM-DD) of the actual call. Required.
        notes: Optional freeform notes stored alongside the transcript.

    Returns:
        The inserted Transcript dataclass.

    Raises:
        ValueError: If filename does not follow YYYY_QN_call.txt convention
            or if call_date is not provided.
    """
    path = Path(file_path)
    quarter_label = _parse_quarter_label(path.name)

    if not call_date:
        raise ValueError(
            "call_date is required. Provide an ISO date (YYYY-MM-DD) for the call."
        )

    full_text = path.read_text(encoding="utf-8")

    with closing(connect(db_path)) as conn:
        with conn:
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO transcripts (date, quarter_label, full_text, notes)
                VALUES (?, ?, ?, ?)
                """,
                (call_date, quarter_label, full_text, notes),
            )
            row_id = cursor.lastrowid

    return Transcript(
        id=row_id,
        date=call_date,
        quarter_label=quarter_label,
        full_text=full_text,
        notes=notes,
    )


def search_transcripts(
    db_path: Path | str = DEFAULT_DB_PATH,
    quarter_label: str | None = None,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
    n_most_recent: int = 1,
) -> list[Transcript]:
    """Retrieve transcripts by quarter label, date range, or recency.

    Priority: quarter_label > date_range > n_most_recent.
    When no filter is supplied, returns the N most recent transcripts
    (default N=1, i.e. the latest call).

    Args:
        db_path: SQLite database path.
        quarter_label: Exact quarter label match, e.g. '2025_Q1'.
        date_range_start: ISO date. Returns transcripts on or after this date.
        date_range_end: ISO date. Returns transcripts on or before this date.
        n_most_recent: Number of most-recent transcripts when no other filter
            is active. Default 1.

    Returns:
        List of Transcript objects, most recent first.
    """
    query = "SELECT id, date, quarter_label, full_text, notes FROM transcripts WHERE 1 = 1"
    params: list[str | int] = []
    using_filter = False

    if quarter_label:
        query += " AND quarter_label = ?"
        params.append(quarter_label)
        using_filter = True

    if date_range_start:
        query += " AND date >= ?"
        params.append(date_range_start)
        using_filter = True

    if date_range_end:
        query += " AND date <= ?"
        params.append(date_range_end)
        using_filter = True

    query += " ORDER BY date DESC"

    if not using_filter:
        query += " LIMIT ?"
        params.append(n_most_recent)

    with closing(connect(db_path)) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        Transcript(
            id=row[0],
            date=row[1],
            quarter_label=row[2],
            full_text=row[3],
            notes=row[4],
        )
        for row in rows
    ]


def _parse_quarter_label(filename: str) -> str:
    """Parse YYYY_QN_call.txt filename into quarter_label like '2025_Q1'.

    Raises:
        ValueError: If the filename doesn't match the expected pattern.
    """
    match = re.match(r"(\d{4}_Q[1-4])_call\.txt$", filename, re.IGNORECASE)
    if not match:
        raise ValueError(
            f"Filename '{filename}' does not follow the YYYY_QN_call.txt convention "
            "(e.g. 2025_Q1_call.txt)."
        )
    return match.group(1)
