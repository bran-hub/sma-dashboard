from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from sma_dashboard.ingestion import DEFAULT_MAPPING_PATH, IngestionResult, main


def test_single_file_cli_logs_successful_ingestion() -> None:
    result = IngestionResult(1, 2, 0, 0, 0, 0, model_date="2025-02-14")

    with (
        patch.object(
            sys,
            "argv",
            [
                "ingestion.py",
                "--file",
                "model.xlsx",
                "--db",
                "audit.db",
                "--replace-date",
                "--notes",
                "delayed update",
                "--ingested-by",
                "analyst",
            ],
        ),
        patch("sma_dashboard.ingestion.ingest_model_update", return_value=result) as ingest,
    ):
        main()

    ingest.assert_called_once_with(
        Path("model.xlsx"),
        model_date=None,
        db_path=Path("audit.db"),
        mapping_path=DEFAULT_MAPPING_PATH,
        ticker_overrides_path=None,
        ticker_currency_overrides_path=None,
        skip_market_data=False,
        replace_date=True,
        source="real data",
        ingested_by="analyst",
        notes="delayed update",
    )
