# Roadmap

## Implemented

- **M1: Data foundation**: SQLite schema, model-update ingestion, ticker normalization, rejected-row auditing, price and FX storage.
- **M2: Performance engine**: CAD time-weighted returns, benchmark comparison, seeded/calculated track record, risk metrics.
- **M3: Valuation and news**: yfinance valuation metrics, portfolio-weighted averages, holding-level news feed.
- **M4: Streamlit dashboard**: performance charts, trailing returns, rolling risk metrics, holdings, trades, valuation, and news.

## Future Work

- **M5: Chatbot layer**: optional conversational interface over portfolio data and manager commentary. Not implemented in this portfolio package.
- **Corporate actions**: manual adjustment workflow for non-trivial events such as mergers, spinoffs, ticker changes, and special dividends.
- **Sector and factor exposure**: additional portfolio analytics beyond current holdings and valuation views.
- **Synthetic demo dataset**: fuller fake dataset for public demos without private holdings or returns.
- **Deployment research**: possible hosted version only after privacy, access-control, and data-permission requirements are defined.
