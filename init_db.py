from __future__ import annotations

import argparse
from pathlib import Path

from sma_dashboard.db import DEFAULT_DB_PATH, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the SMA dashboard SQLite database.")
    parser.add_argument("--db", "--db-path", dest="db_path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    path = init_db(args.db_path)
    print(f"Initialized database at {path}")


if __name__ == "__main__":
    main()
