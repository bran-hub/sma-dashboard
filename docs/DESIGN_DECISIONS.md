# Design Decisions

## Time-Weighted Return Only

The project uses time-weighted return as the sole portfolio return metric. TWR isolates manager investment decisions from account-owner contribution and withdrawal timing, which matches the oversight use case. Money-weighted return and IRR are intentionally out of scope.

## CAD-Only Performance

All performance output is CAD-denominated. Canadian listings use stored adjusted prices directly. USD-listed holdings are converted using stored `CADUSD=X` rates so historical calculations are reproducible.

## Strict Missing-Data Handling

The calculation layer does not silently ignore missing active-holding prices or FX data, and it does not renormalize remaining holdings when data is incomplete. Strict mode raises clear errors; dashboard mode keeps available history visible and surfaces concise warnings.

## SQLite Over Flat Files

The project stores normalized data in SQLite rather than maintaining many CSV files. This gives the dashboard a single queryable source for holdings, trades, prices, FX rates, seeded returns, and rejected ingestion rows.

## yfinance For A Portfolio Project

yfinance keeps the project free to run and easy to inspect. It is appropriate for a personal portfolio project, while a production investment system would likely use a paid vendor with stronger service guarantees.

## Local-First Privacy

The app is designed for a single local user. Hosting, authentication, permissions, and shared access are deferred because the current priority is a privacy-safe local workflow.

## Chatbot Deferred

The current implementation stops at the Streamlit dashboard. A chatbot layer over portfolio data and manager commentary is future work and is not included in this package.
