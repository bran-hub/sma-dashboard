# Architecture

The dashboard is a local-first Python application. Private files stay on the user's machine, normalized records live in SQLite, pure calculation functions produce analytics, and Streamlit renders the result.

## Data flow

1. A model-update workbook is parsed and validated in memory.
2. Required price and FX rows are collected before persistence.
3. Holdings, trades, rejected rows, prices, FX, and the ingestion audit record are written in one SQLite transaction.
4. Performance reads dated holdings snapshots and stored market data. A snapshot is effective on the next observed trading day; weights drift until another snapshot takes effect.
5. Streamlit reads the same database for performance, risk, holdings, trades, valuation, and news.

## Components

| Component | Responsibility |
|---|---|
| `sma_dashboard/ingestion.py` | Workbook parsing, validation, normalization, currency rules, atomic persistence |
| `sma_dashboard/batch_ingestion.py` | Ordered manifest-driven ingestion |
| `sma_dashboard/performance.py` | CAD returns, benchmark, weight drift, risk metrics |
| `sma_dashboard/valuation.py` | Holding and weighted portfolio valuation |
| `sma_dashboard/news.py` | Holding-level news enrichment |
| `sma_dashboard/demo.py` | Deterministic synthetic demo database |
| `sma_dashboard/transcripts.py` | Local transcript storage and retrieval |
| `sma_dashboard/chatbot.py` | Offline mock answers and optional Anthropic tool loop |
| `sma_dashboard/dashboard.py` | Streamlit presentation |

## Boundaries

SQLite is the reproducible local source of truth. Network access is confined to explicit market-data enrichment and opt-in Anthropic mode. The synthetic demo follows the complete stored-data path and requires no network calls.
