from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "db" / "sma_dashboard.sqlite"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> Path:
    """Create all M1 database tables if they do not already exist."""
    path = Path(db_path)
    with closing(connect(path)) as conn:
        with conn:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            _run_migrations(conn)
    return path


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply small additive migrations for existing local SQLite databases."""
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(seeded_returns)").fetchall()
    }
    if "benchmark_return_pct" not in columns:
        conn.execute("ALTER TABLE seeded_returns ADD COLUMN benchmark_return_pct REAL")
