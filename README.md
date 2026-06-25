# SMA Oversight Dashboard

A local Python dashboard for independently monitoring a Separately Managed Account (SMA): model updates, portfolio performance, valuation metrics, trade history, and holding-level news in one Streamlit app.

## Why This Project Exists

External managers often provide periodic holdings updates and performance reporting, but those reports are difficult to audit independently. This project rebuilds the oversight layer from local source files and public market data so an account owner can validate performance, review allocation changes, and monitor current holdings without relying only on manager-provided summaries.

The repo is packaged as a privacy-safe portfolio project. It demonstrates data ingestion, SQLite modeling, CAD performance calculations, dashboard design, and defensive handling of incomplete market data without including private holdings, returns, account files, or secrets.

## What The Dashboard Does

- Ingests external manager model-update workbooks into a local SQLite database.
- Normalizes tickers into yfinance-compatible symbols.
- Stores holdings snapshots, trade records, prices, FX rates, rejected rows, and seeded historical returns.
- Calculates CAD time-weighted returns against a Canadian equity benchmark.
- Displays performance, risk, valuation, holdings, trades, and news in a local Streamlit dashboard.
- Surfaces data-quality warnings when required prices or FX data are missing.

## Key Features

- **Performance view**: hypothetical CAD growth, period returns, benchmark comparison, rolling risk metrics, and trailing annualized returns.
- **Risk metrics**: volatility, maximum drawdown, Sharpe ratio, beta, alpha, tracking error, and information ratio.
- **Two-phase track record**: historical manager-reported returns can seed the period before detailed holdings data is available; later periods are calculated from holdings and market data.
- **Valuation layer**: per-holding fundamentals and portfolio-weighted averages using yfinance data.
- **Trade audit**: parsed buys, sells, trims, and adds with date/action filters.
- **News feed**: recent yfinance news tagged to current holdings and portfolio weights.
- **Privacy-first local workflow**: raw workbooks, local databases, seeded return files, local overrides, `.env` files, and Streamlit secrets are ignored by git.

## Architecture

The project is intentionally small and local-first:

| Layer | Choice |
|---|---|
| Language | Python |
| App UI | Streamlit |
| Storage | SQLite |
| Market data | yfinance |
| Data processing | pandas |
| Tests | pytest |

Core modules live under `sma_dashboard/`:

- `ingestion.py`: model-update parsing, ticker normalization, row validation, price and FX loading.
- `batch_ingestion.py`: ordered ingestion of multiple private model-update files from a local manifest.
- `seeded_returns.py`: loading historical seeded return data from a private local export.
- `performance.py`: CAD TWR, benchmark comparison, period returns, and risk metrics.
- `valuation.py`: holding-level and weighted-average valuation metrics.
- `news.py`: holding-level news aggregation.
- `dashboard.py` and `dashboard_support.py`: Streamlit UI and dashboard-safe helpers.

More detail is available in:

- [Architecture](docs/ARCHITECTURE.md)
- [Design Decisions](docs/DESIGN_DECISIONS.md)
- [Privacy](docs/PRIVACY.md)
- [Roadmap](docs/ROADMAP.md)

## Design Decisions And Tradeoffs

- **Local-first instead of hosted**: simpler privacy posture for a personal finance project; no authentication or deployment layer is required for the current scope.
- **SQLite instead of flat files**: holdings, trades, prices, FX rates, seeded returns, and rejected rows need relational joins and reproducible local state.
- **yfinance only**: free and sufficient for a portfolio project; a production version would likely use a paid market-data vendor.
- **TWR only**: time-weighted return isolates manager investment decisions from account-owner cash-flow timing. Money-weighted return and IRR are intentionally out of scope.
- **CAD-only performance output**: Canadian listings are treated as CAD; USD-listed holdings are converted using stored `CADUSD=X` FX rates.
- **Strict missing-data behavior**: calculations fail or warn when active holdings lack required prices or FX data rather than silently renormalizing the remaining holdings.
- **No chatbot in the current implementation**: a conversational layer is future work and is not included in this portfolio package.

## Privacy And Data Handling

This repository does not include private holdings, real model-update workbooks, private seeded returns, account databases, API keys, Streamlit secrets, or local ticker override files.

Private local inputs should stay in ignored paths such as:

- `data/raw/`
- `data/db/`
- `config/ticker_overrides.local.json`
- `.env` / `.env.*`
- `.streamlit/secrets.toml`

Only synthetic examples and placeholders should be committed. See [Privacy](docs/PRIVACY.md) for the repo packaging rules.

## Local Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Initialize a local SQLite database:

```powershell
python init_db.py --db data/db/sma_dashboard.db
```

Ingest a private model-update workbook:

```powershell
python ingestion.py --file data/raw/manager_model_update_YYYY-MM-DD.xlsx --model-date YYYY-MM-DD
```

For offline validation without market-data calls:

```powershell
python ingestion.py --file data/raw/manager_model_update_YYYY-MM-DD.xlsx --model-date YYYY-MM-DD --skip-market-data
```

Run the dashboard:

```powershell
streamlit run dashboard.py
```

The dashboard defaults to `data/db/sma_dashboard.db`. You can override the SQLite database path from the sidebar.

## Testing And Quality

Run the test suite:

```powershell
pytest
```

The tests cover schema creation, ingestion behavior, seeded returns, performance calculations, valuation logic, dashboard helpers, and news handling. The code is organized so core calculations can be tested independently of the Streamlit interface.

## Roadmap

Implemented:

- M1: SQLite schema, model-update ingestion, row validation, price/FX storage.
- M2: CAD performance engine, benchmark comparison, seeded/calculated track record.
- M3: valuation metrics and holding-level news.
- M4: local Streamlit dashboard.

Future work:

- M5: optional chatbot layer over portfolio data and manager commentary.
- Corporate-action adjustment workflow.
- Sector/factor exposure analysis.
- More complete synthetic sample dataset for public demos.
