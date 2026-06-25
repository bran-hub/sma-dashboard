# Architecture

The SMA Oversight Dashboard is a local-first Python application. It keeps private inputs on the user's machine, stores normalized portfolio data in SQLite, and renders analysis through Streamlit.

## Data Flow

1. Private manager model-update workbooks are placed under `data/raw/`.
2. Ingestion parses the workbook, detects the table header, validates rows, normalizes tickers, and writes holdings and trades to SQLite.
3. yfinance is used to fetch prices, FX rates, valuation metrics, and recent news.
4. Performance calculations read holdings, prices, FX rates, benchmark prices, and seeded historical returns from SQLite.
5. The Streamlit dashboard presents performance, risk, holdings, trades, valuation, and news from the local database.

## Main Components

| Component | Responsibility |
|---|---|
| `sma_dashboard/ingestion.py` | Single-file model-update ingestion, validation, ticker normalization, price and FX loading |
| `sma_dashboard/batch_ingestion.py` | Ordered ingestion of multiple private model updates through a local manifest |
| `sma_dashboard/seeded_returns.py` | Loading historical seeded returns from a private local export |
| `sma_dashboard/performance.py` | CAD TWR, benchmark comparison, period returns, risk metrics |
| `sma_dashboard/valuation.py` | Per-holding and portfolio-weighted valuation metrics |
| `sma_dashboard/news.py` | Holding-level news aggregation |
| `sma_dashboard/dashboard.py` | Streamlit dashboard |
| `sma_dashboard/dashboard_support.py` | Dashboard-safe orchestration and data-quality helpers |

## Storage

SQLite is used because the project needs reproducible local state and relational joins across holdings, trades, prices, FX rates, seeded returns, and rejected ingestion rows. Local database files are ignored by git.

## Public Portfolio Scope

The public package includes code, tests, configuration templates, and synthetic examples only. It does not include real holdings, model-update workbooks, seeded return exports, local databases, or secrets.
