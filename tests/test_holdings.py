from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from sma_dashboard.db import init_db
from sma_dashboard.holdings import (
    HoldingsDataError,
    ensure_model_update_not_applied,
    get_model_updates_applied,
    get_pending_model_updates,
    log_model_update_applied,
)


TEST_DB = Path("data/db/test_holdings.sqlite")


class ModelUpdateTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db(TEST_DB)

    def tearDown(self) -> None:
        TEST_DB.unlink(missing_ok=True)

    def test_log_and_retrieve_model_update(self) -> None:
        log_model_update_applied(
            TEST_DB,
            "2025-02-14",
            file_name="model.xlsx",
            source="real data",
            ingested_by="analyst",
            notes="delayed update",
        )

        applied = get_model_updates_applied(TEST_DB)

        self.assertEqual(len(applied), 1)
        self.assertEqual(applied.loc[0, "model_date"], "2025-02-14")
        self.assertEqual(applied.loc[0, "file_name"], "model.xlsx")
        self.assertEqual(applied.loc[0, "source"], "real data")
        self.assertEqual(applied.loc[0, "ingested_by"], "analyst")
        self.assertEqual(applied.loc[0, "notes"], "delayed update")
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(applied["ingested_at"]))

    def test_duplicate_model_date_has_clear_replace_guidance(self) -> None:
        log_model_update_applied(TEST_DB, "2025-02-14")

        with self.assertRaisesRegex(
            HoldingsDataError,
            "already been applied.*--replace-date",
        ):
            log_model_update_applied(TEST_DB, "2025-02-14")

        with self.assertRaisesRegex(
            HoldingsDataError,
            "already been applied.*--replace-date",
        ):
            ensure_model_update_not_applied(TEST_DB, "2025-02-14")

    def test_pending_model_updates_excludes_applied_dates(self) -> None:
        log_model_update_applied(TEST_DB, "2025-02-14")
        manifest = pd.DataFrame(
            [
                {"model_date": "2025-02-14", "file": "applied.xlsx", "notes": "done"},
                {"model_date": "2025-03-01", "file": "pending.xlsx", "notes": "new"},
            ]
        )

        pending = get_pending_model_updates(TEST_DB, manifest)

        self.assertEqual(pending["model_date"].tolist(), ["2025-03-01"])
        self.assertEqual(pending["file"].tolist(), ["pending.xlsx"])


if __name__ == "__main__":
    unittest.main()
