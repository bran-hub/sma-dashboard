from __future__ import annotations

import os
import subprocess
import sys

from sma_dashboard.demo import build_demo_database


def main() -> None:
    demo_db = build_demo_database()
    env = os.environ.copy()
    env["SMA_DASHBOARD_DB"] = str(demo_db)
    env["SMA_DEMO_MODE"] = "1"
    env["SMA_ENABLE_CHAT"] = "1"
    env["SMA_CHAT_MODE"] = "mock"
    subprocess.run([sys.executable, "-m", "streamlit", "run", "dashboard.py"], check=True, env=env)


if __name__ == "__main__":
    main()
