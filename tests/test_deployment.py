from __future__ import annotations

import os
from pathlib import Path


def test_hosted_entrypoint_prepares_network_free_synthetic_demo() -> None:
    import streamlit_app

    demo_db = streamlit_app.prepare_hosted_demo()

    assert demo_db.is_file()
    assert demo_db.name == "sma_demo.db"
    assert os.environ["SMA_DASHBOARD_DB"] == str(Path("data/db/sma_demo.db"))
    assert os.environ["SMA_DEMO_MODE"] == "1"
    assert os.environ["SMA_ENABLE_CHAT"] == "1"
    assert os.environ["SMA_CHAT_MODE"] == "mock"
