"""Hosted synthetic-demo entrypoint for Streamlit Community Cloud."""

from __future__ import annotations

import os
from pathlib import Path


DEMO_DB = Path("data/db/sma_demo.db")


def prepare_hosted_demo() -> Path:
    """Create a local synthetic database and force network-free demo settings."""
    os.environ["SMA_DASHBOARD_DB"] = str(DEMO_DB)
    os.environ["SMA_DEMO_MODE"] = "1"
    os.environ["SMA_ENABLE_CHAT"] = "1"
    os.environ["SMA_CHAT_MODE"] = "mock"

    from sma_dashboard.demo import build_demo_database

    if not DEMO_DB.exists():
        build_demo_database(DEMO_DB)
    return DEMO_DB.resolve()


prepare_hosted_demo()

from sma_dashboard.dashboard import run_app  # noqa: E402


if __name__ == "__main__":
    run_app()
