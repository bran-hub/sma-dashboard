"""CLI script to load a quarterly manager call transcript into the database.

Usage:
    python load_transcript.py sample_data/2025_Q1_call.txt --db data/db/sma_dashboard.db

The script parses the quarter_label from the filename (YYYY_QN_call.txt convention),
prompts for the exact call date (since quarter-end naming doesn't capture it),
and writes the full transcript text to the `transcripts` table.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from sma_dashboard.db import DEFAULT_DB_PATH, init_db
from sma_dashboard.transcripts import load_transcript, _parse_quarter_label


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a quarterly manager call transcript into the SMA dashboard database."
    )
    parser.add_argument(
        "file",
        type=Path,
        help="Path to the .txt transcript file (YYYY_QN_call.txt naming convention).",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Path to the SQLite database (default: data/db/sma_dashboard.db).",
    )
    parser.add_argument(
        "--date",
        dest="call_date",
        help=(
            "ISO date (YYYY-MM-DD) of the call. "
            "If not supplied, the script will prompt for it interactively."
        ),
    )
    parser.add_argument(
        "--notes",
        help="Optional freeform notes to store alongside the transcript.",
    )
    args = parser.parse_args()

    file_path = args.file
    if not file_path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    # Validate filename convention early so we fail before prompting
    try:
        quarter_label = _parse_quarter_label(file_path.name)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Resolve call date
    call_date = args.call_date
    if not call_date:
        call_date = _prompt_call_date(quarter_label)

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", call_date):
        print(f"Error: call date must be ISO format YYYY-MM-DD, got: {call_date}", file=sys.stderr)
        sys.exit(1)

    init_db(args.db_path)

    transcript = load_transcript(
        file_path=file_path,
        db_path=args.db_path,
        call_date=call_date,
        notes=args.notes,
    )

    char_count = len(transcript.full_text)
    print(f"Loaded transcript:")
    print(f"  quarter_label : {transcript.quarter_label}")
    print(f"  date          : {transcript.date}")
    print(f"  characters    : {char_count:,}")
    if transcript.notes:
        print(f"  notes         : {transcript.notes}")
    print(f"  db_path       : {args.db_path}")


def _prompt_call_date(quarter_label: str) -> str:
    print(f"Quarter: {quarter_label}")
    print("Enter the exact call date (YYYY-MM-DD):", end=" ", flush=True)
    return input().strip()


if __name__ == "__main__":
    main()
