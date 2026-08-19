# SMA Oversight Dashboard

[![Tests](https://github.com/bran-hub/sma-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/bran-hub/sma-dashboard/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A privacy-first Streamlit dashboard for independently auditing a separately managed account. It turns periodic model updates and market data into a reproducible CAD performance record, holdings view, trade log, risk dashboard, valuation overview, and optional portfolio assistant.

The repository is a synthetic portfolio project: no real holdings, account files, manager reports, returns, or credentials are included.

## Try the synthetic demo

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python run_demo.py
```

`run_demo.py` deterministically builds `data/db/sma_demo.db` and launches Streamlit without downloading market data. The generated holdings, trades, prices, FX rates, returns, and manager transcript are fictional and safe to regenerate or delete. The assistant starts in offline mock mode and needs no API key.

## What it demonstrates

- Atomic, idempotent workbook ingestion with rejected-row auditing.
- Explicit CAD/USD security currency, including local overrides for ambiguous symbols.
- CAD time-weighted returns, a Canadian equity benchmark, trailing returns, and rolling risk metrics.
- Holdings snapshots that become effective on the next observed trading day.
- Buy-and-hold weight drift between snapshots, with residual cash treated as zero-return exposure.
- Local SQLite storage and a Streamlit UI with clear missing-data states.
- Optional live valuation and news enrichment through yfinance.
- Deterministic portfolio questions over synthetic holdings, trades, performance, and commentary, with optional Anthropic tool-calling mode.

## Architecture

| Layer | Implementation |
|---|---|
| UI | Streamlit and Altair |
| Analytics | pandas-based CAD TWR and risk calculations |
| Storage | SQLite |
| Inputs | Excel model updates, seeded return CSVs, yfinance |
| Quality | pytest and GitHub Actions on Python 3.11/3.12 |

See [Architecture](docs/ARCHITECTURE.md), [Methodology](docs/METHODOLOGY.md), [Privacy](docs/PRIVACY.md), and [Roadmap](docs/ROADMAP.md).

## Use with private local data

Initialize the database:

```powershell
python init_db.py --db data/db/sma_dashboard.db
```

Copy the example ticker override files to ignored `.local.json` files if needed, then ingest one update:

```powershell
python ingestion.py `
  --file data/raw/manager_model_update_YYYY-MM-DD.xlsx `
  --model-date YYYY-MM-DD `
  --db data/db/sma_dashboard.db `
  --ticker-overrides config/ticker_overrides.local.json `
  --ticker-currency-overrides config/ticker_currency_overrides.local.json
```

Use `--skip-market-data` for offline parsing and validation. Re-running an applied model date is rejected by default; use `--replace-date` only for an intentional atomic replacement.

Load a sequence from a private manifest:

```powershell
python ingest_model_updates.py `
  --manifest data/raw/model_updates_manifest.csv `
  --db data/db/sma_dashboard.db `
  --config config/column_mapping.json `
  --ticker-overrides config/ticker_overrides.local.json `
  --ticker-currency-overrides config/ticker_currency_overrides.local.json
```

Run the app:

```powershell
$env:SMA_DASHBOARD_DB = "data/db/sma_dashboard.db"
streamlit run dashboard.py
```

## Optional portfolio assistant

The public demo uses `SMA_CHAT_MODE=mock`: questions are classified locally and answered by deterministic Python tools against the selected SQLite database. No prompt or portfolio data leaves the machine.

For opt-in live Anthropic mode:

```powershell
python -m pip install -e ".[chatbot]"
$env:SMA_ENABLE_CHAT = "1"
$env:SMA_CHAT_MODE = "anthropic"
streamlit run dashboard.py
```

Copy `.streamlit/secrets.toml.example` to the ignored `.streamlit/secrets.toml` and add `ANTHROPIC_API_KEY`. Live mode may send the user's question and tool results to Anthropic; review the data-sharing implications before enabling it. The API key is never required for the demo or CI.

## Methodology in brief

Performance is daily, CAD-denominated time-weighted return. A holdings snapshot dated on day _t_ is applied starting with the next observed trading day, preventing same-day look-ahead. Target weights initialize the portfolio at each new snapshot; security weights then drift with relative returns until the next snapshot. Weight below 100% is residual cash earning 0%; long-only snapshots above 100% are rejected. Missing prices or required FX rates are surfaced rather than silently dropping and renormalizing positions.

This is an oversight and software-engineering project, not brokerage, accounting, tax, or investment-advice software. yfinance data is convenient for demonstration but does not carry production service guarantees.

## Tests

```powershell
python -m pytest
python -m sma_dashboard.demo --output data/db/demo-smoke.db
```

The suite covers schema migrations, ingestion rollback/idempotency, currency classification, performance timing and drift, valuation, dashboard helpers, transcripts, and the optional assistant tools.

## Privacy

Keep real inputs in ignored paths such as `data/raw/`, `data/db/`, `.streamlit/secrets.toml`, and local override files. Public releases are produced through an allowlisted export with filename/content deny checks; private development history is never merged into the public repository. See [Privacy](docs/PRIVACY.md).

## License

MIT. See [LICENSE](LICENSE).
