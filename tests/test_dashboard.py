"""Tests for Streamlit dashboard configuration."""

from pathlib import Path
from unittest.mock import patch

import pandas as pd

from sma_dashboard.dashboard import _render_model_update_status, _valuation_column_config


def test_valuation_column_config_formats_decimal_dividend_yields_as_percentages() -> None:
    config = _valuation_column_config()

    dividend_yield = config["Dividend Yield"]
    assert dividend_yield["type_config"]["type"] == "number"
    assert dividend_yield["type_config"]["format"] == "percent"


def test_model_update_status_displays_latest_update_and_history() -> None:
    applied = pd.DataFrame(
        [
            {
                "model_date": "2025-03-01",
                "file_name": "model.xlsx",
                "ingested_at": pd.Timestamp("2025-03-02 14:30:00"),
            }
        ]
    )

    with (
        patch("sma_dashboard.dashboard.get_model_updates_applied", return_value=applied),
        patch("sma_dashboard.dashboard.st.success") as success,
        patch("sma_dashboard.dashboard.st.dataframe") as dataframe,
    ):
        _render_model_update_status(Path("audit.db"))

    success.assert_called_once_with(
        "Latest model applied: **2025-03-01** (ingested 2025-03-02 14:30)"
    )
    displayed = dataframe.call_args.args[0]
    assert displayed.columns.tolist() == ["model_date", "file_name", "ingested_at"]


def test_model_update_status_handles_empty_history() -> None:
    with (
        patch(
            "sma_dashboard.dashboard.get_model_updates_applied",
            return_value=pd.DataFrame(),
        ),
        patch("sma_dashboard.dashboard.st.info") as info,
        patch("sma_dashboard.dashboard.st.dataframe") as dataframe,
    ):
        _render_model_update_status(Path("audit.db"))

    info.assert_called_once_with("No model updates have been applied yet.")
    dataframe.assert_not_called()
