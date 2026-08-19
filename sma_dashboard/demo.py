from __future__ import annotations

import argparse
import math
import os
import sqlite3
from pathlib import Path

import pandas as pd

from sma_dashboard.db import PROJECT_ROOT, init_db
from sma_dashboard.ingestion import FX_PAIR
from sma_dashboard.performance import BENCHMARK_TICKER


DEFAULT_DEMO_DB = PROJECT_ROOT / "data" / "db" / "sma_demo.db"


def build_demo_database(output_path: Path | str = DEFAULT_DEMO_DB) -> Path:
    """Build a deterministic, fully synthetic offline dashboard database."""
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.building")
    temporary.unlink(missing_ok=True)
    init_db(temporary)

    seeded_dates = pd.date_range("2024-01-31", "2025-01-31", freq="ME")
    price_dates = pd.bdate_range("2025-01-31", "2026-06-30")
    currencies = {"RY.TO": "CAD", "SHOP.TO": "CAD", "AAPL": "USD", "MSFT": "USD"}
    snapshots = {
        "2025-02-01": {"RY.TO": 30.0, "SHOP.TO": 25.0, "AAPL": 20.0, "MSFT": 15.0},
        "2025-07-15": {"RY.TO": 28.0, "SHOP.TO": 22.0, "AAPL": 24.0, "MSFT": 18.0},
        "2026-01-15": {"RY.TO": 25.0, "SHOP.TO": 27.0, "AAPL": 22.0, "MSFT": 20.0},
        "2026-04-15": {"RY.TO": 27.0, "SHOP.TO": 24.0, "AAPL": 21.0, "MSFT": 22.0},
    }

    with sqlite3.connect(temporary) as conn:
        conn.executemany(
            """
            INSERT INTO seeded_returns (date, return_pct, benchmark_return_pct, source, notes)
            VALUES (?, ?, ?, 'synthetic demo', 'Generated example; not investment performance')
            """,
            [
                (
                    timestamp.date().isoformat(),
                    round(0.65 + 1.15 * math.sin(index / 2.2), 4),
                    round(0.55 + 0.95 * math.sin(index / 2.4 + 0.25), 4),
                )
                for index, timestamp in enumerate(seeded_dates)
            ],
        )

        previous: dict[str, float] | None = None
        for snapshot_date, weights in snapshots.items():
            conn.executemany(
                """
                INSERT INTO holdings (date, ticker, weight, currency, shares, cost_basis)
                VALUES (?, ?, ?, ?, NULL, NULL)
                """,
                [(snapshot_date, ticker, weight, currencies[ticker]) for ticker, weight in weights.items()],
            )
            if previous is not None:
                for ticker, weight in weights.items():
                    change = weight - previous.get(ticker, 0.0)
                    if change == 0:
                        continue
                    conn.execute(
                        """
                        INSERT INTO trades (date, ticker, action, weight_change, notes)
                        VALUES (?, ?, ?, ?, 'Synthetic quarterly rebalance')
                        """,
                        (snapshot_date, ticker, "add" if change > 0 else "trim", change),
                    )
            conn.execute(
                """
                INSERT INTO model_updates_applied (model_date, file_name, source, notes)
                VALUES (?, ?, 'synthetic demo', 'Generated example; no private data')
                """,
                (snapshot_date, f"synthetic_model_{snapshot_date}.xlsx"),
            )
            previous = weights

        bases = {"RY.TO": 100.0, "SHOP.TO": 85.0, "AAPL": 180.0, "MSFT": 320.0}
        trends = {"RY.TO": 0.00022, "SHOP.TO": 0.00038, "AAPL": 0.00031, "MSFT": 0.00034}
        price_rows: list[tuple[str, str, float, float]] = []
        for index, timestamp in enumerate(price_dates):
            for offset, ticker in enumerate(currencies):
                cycle = 0.035 * math.sin(index / (12.0 + offset * 2.0) + offset)
                price = bases[ticker] * math.exp(trends[ticker] * index + cycle)
                price_rows.append((timestamp.date().isoformat(), ticker, price, price))
            benchmark = 100.0 * math.exp(0.00024 * index + 0.018 * math.sin(index / 22.0))
            price_rows.append((timestamp.date().isoformat(), BENCHMARK_TICKER, benchmark, benchmark))
        conn.executemany("INSERT INTO prices (date, ticker, close, adj_close) VALUES (?, ?, ?, ?)", price_rows)
        conn.executemany(
            "INSERT INTO fx_rates (date, pair, rate) VALUES (?, ?, ?)",
            [
                (timestamp.date().isoformat(), FX_PAIR, 0.74 + 0.015 * math.sin(index / 30.0))
                for index, timestamp in enumerate(price_dates)
            ],
        )
        transcript_path = PROJECT_ROOT / "sample_data" / "2025_Q1_call.txt"
        if transcript_path.exists():
            conn.execute(
                """
                INSERT INTO transcripts (date, quarter_label, full_text, notes)
                VALUES ('2025-03-28', '2025_Q1', ?, 'Synthetic demo transcript')
                """,
                (transcript_path.read_text(encoding="utf-8"),),
            )
        conn.commit()
    conn.close()

    os.replace(temporary, output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the deterministic synthetic SMA dashboard demo database.")
    parser.add_argument("--output", type=Path, default=DEFAULT_DEMO_DB)
    args = parser.parse_args()
    print(build_demo_database(args.output))


if __name__ == "__main__":
    main()
