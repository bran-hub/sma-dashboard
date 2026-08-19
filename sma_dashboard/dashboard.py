from __future__ import annotations

import os
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from sma_dashboard.dashboard_support import (
    BENCHMARK_REFRESH_COMMAND,
    DEFAULT_STARTING_CAPITAL,
    DASHBOARD_DEFAULT_DB_PATH,
    PERFORMANCE_RANGE_OPTIONS,
    SETUP_COMMANDS,
    add_observation_axis_fields,
    apply_date_filter,
    benchmark_lag_warning,
    build_growth_chart_data,
    calculate_trailing_annualized_returns,
    calculate_growth_y_axis_domain,
    database_overview,
    filter_performance_window,
    format_annualized_returns_table,
    format_currency_cad_full,
    format_holdings_table,
    format_news_table,
    format_percent,
    format_qtd_holding_returns_table,
    format_ratio,
    format_risk_metrics_table,
    format_trade_log_table,
    format_weighted_valuation_averages,
    get_calculated_benchmark_price_coverage,
    get_dashboard_freshness,
    list_trade_filter_values,
    load_trade_log,
    normalize_starting_capital,
    pending_model_update_notice,
    performance_range_note,
    prepare_valuation_table_for_display,
    prepare_rolling_metrics_chart_data,
    refresh_dashboard_market_data,
    resolve_dashboard_db_path,
    select_calendar_axis_tick_dates,
    select_observation_axis_labels,
    select_rolling_metrics_axis_tick_dates,
    should_use_compressed_trading_axis,
    validate_dashboard_database,
    latest_rolling_metrics,
)
from sma_dashboard.holdings import (
    HoldingsDataError,
    get_latest_holdings_snapshot,
    get_model_updates_applied,
)
from sma_dashboard.news import get_holding_news
from sma_dashboard.performance import (
    BENCHMARK_TICKER,
    PerformanceDataError,
    calculate_benchmark_twr_series,
    calculate_daily_portfolio_returns,
    calculate_qtd_holding_price_returns,
    calculate_risk_metrics,
    calculate_rolling_metrics,
    calculate_twr_series,
    calculate_twr_series_with_quality,
    get_benchmark_returns,
    get_period_returns,
)
from sma_dashboard.valuation import ValuationDataError, get_portfolio_valuation


def run_app() -> None:
    st.set_page_config(page_title="SMA Dashboard", layout="wide")
    st.title("SMA Dashboard")

    controls = _sidebar_controls()
    db_path = controls["db_path"]
    demo_mode = os.environ.get("SMA_DEMO_MODE", "0") == "1"

    validation = validate_dashboard_database(db_path)
    if not validation.is_valid:
        _render_database_setup_warning(validation)
        return

    _render_market_data_refresh(db_path, controls)
    _render_data_overview(db_path)
    _render_performance(db_path, controls)
    _render_rolling_metrics(db_path, controls)
    _render_holdings_and_valuation(db_path, include_live_data=not demo_mode)
    _render_model_update_status(db_path)
    _render_trade_log(db_path)
    _render_news(db_path, include_live_data=not demo_mode)
    if os.environ.get("SMA_ENABLE_CHAT", "0") == "1":
        _render_chat(db_path)


def _sidebar_controls() -> dict[str, object]:
    with st.sidebar:
        st.header("Controls")
        db_path_text = st.text_input("SQLite database path", value=str(DASHBOARD_DEFAULT_DB_PATH.relative_to(DASHBOARD_DEFAULT_DB_PATH.parents[2])))
        resolved_db_path = resolve_dashboard_db_path(db_path_text)
        st.caption(f"Resolved DB: {resolved_db_path}")
        benchmark = st.text_input("Benchmark ticker", value=BENCHMARK_TICKER)
        starting_capital_input = st.number_input(
            "Starting Capital (CAD)",
            value=float(DEFAULT_STARTING_CAPITAL),
            step=10_000.0,
            format="%.0f",
        )
        starting_capital = normalize_starting_capital(starting_capital_input)
        if starting_capital != float(starting_capital_input):
            st.warning("Starting capital must be positive. Using C$100,000.")
        use_date_filter = st.checkbox("Filter performance dates", value=False)
        start_date = end_date = None
        if use_date_filter:
            start_date = st.date_input("Start date", value=None)
            end_date = st.date_input("End date", value=None)
        demo_mode = os.environ.get("SMA_DEMO_MODE", "0") == "1"
        auto_refresh = st.checkbox("Auto-refresh market data on launch", value=not demo_mode)
        refresh = st.button("Refresh market data now")
    return {
        "db_path": resolved_db_path,
        "benchmark": benchmark.strip() or BENCHMARK_TICKER,
        "starting_capital": starting_capital,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "auto_refresh": auto_refresh,
        "refresh": refresh,
    }


def _render_database_setup_warning(validation) -> None:
    st.warning(f"{validation.message} Selected path: `{validation.path}`")
    if validation.missing_tables:
        st.info("Missing required tables: " + ", ".join(validation.missing_tables))
    st.markdown("Run these setup commands from the project root:")
    st.code("\n".join(SETUP_COMMANDS), language="powershell")


def _render_market_data_refresh(db_path: Path, controls: dict[str, object]) -> None:
    benchmark = str(controls["benchmark"])
    refresh_key = f"market_data_refreshed::{db_path.resolve()}::{benchmark}"
    should_auto_refresh = bool(controls.get("auto_refresh")) and not st.session_state.get(refresh_key, False)
    should_manual_refresh = bool(controls.get("refresh"))

    if should_auto_refresh or should_manual_refresh:
        with st.spinner("Refreshing market data..."):
            summary = refresh_dashboard_market_data(db_path, benchmark)
        st.session_state[refresh_key] = True
        st.cache_data.clear()
        _render_refresh_summary(summary)

    freshness = get_dashboard_freshness(db_path, benchmark)
    with st.expander("Data freshness", expanded=False):
        st.write(
            {
                "Latest model update": freshness.latest_model_update_date,
                "Latest current-holding prices": freshness.latest_current_holding_price_date,
                "Current-holding price status": freshness.current_holding_price_status,
                "Missing current-holding prices": ", ".join(freshness.missing_current_holding_price_tickers) or None,
                "Latest benchmark price": freshness.latest_benchmark_price_date,
                "Latest calculated return": freshness.latest_calculated_return_date,
            }
        )
    if freshness.missing_current_holding_price_tickers:
        st.warning(
            "Current holdings missing stored prices: "
            + ", ".join(freshness.missing_current_holding_price_tickers)
        )
    notice = pending_model_update_notice(db_path)
    if notice:
        st.info(notice)


def _render_refresh_summary(summary) -> None:
    if summary.refreshed_anything:
        st.success(
            "Market data refreshed: "
            f"{summary.holding_price_rows_written} holding price rows, "
            f"{summary.fx_rows_written} FX rows, "
            f"{summary.benchmark_rows_written} benchmark rows."
        )
    else:
        latest_dates = [
            value
            for value in (summary.latest_holding_price_after, summary.latest_benchmark_price_after)
            if value is not None
        ]
        latest = min(latest_dates) if latest_dates else None
        message = f"Market data current through {latest}." if latest else "No new market data was available."
        st.info(message)
    if summary.missing_holding_price_tickers_after:
        st.warning(
            "Current holdings still missing stored prices after refresh: "
            + ", ".join(summary.missing_holding_price_tickers_after)
        )
    if summary.failed_tickers:
        st.error(
            "Market-data refresh failed for: "
            + ", ".join(summary.failed_tickers)
            + ". Check ticker overrides before relying on calculated returns."
        )
    for warning in summary.warnings:
        st.warning(warning)


def _render_data_overview(db_path: Path) -> None:
    try:
        overview = database_overview(db_path)
    except Exception as exc:
        st.error(f"Could not read database: {exc}")
        return
    with st.expander("Data availability", expanded=False):
        st.dataframe(pd.DataFrame([overview]), hide_index=True, use_container_width=True)
        if overview["seeded_returns"] == 0:
            st.warning("No seeded returns found.")
        if overview["holdings"] == 0:
            st.warning("No holdings found.")
        if overview["prices"] == 0:
            st.warning("No prices found.")


def _render_performance(db_path: Path, controls: dict[str, object]) -> None:
    st.header("Performance")
    end_date = controls["end_date"]
    start_date = controls["start_date"]
    benchmark = str(controls["benchmark"])
    try:
        portfolio_result = calculate_twr_series_with_quality(db_path, end_date=end_date, strict=False)
        portfolio_full = portfolio_result.data
        portfolio = apply_date_filter(portfolio_full, start_date, end_date)
    except PerformanceDataError as exc:
        st.warning(f"Performance unavailable: {exc}")
        return
    if portfolio_result.issues:
        st.error(
            "Calculated portfolio returns are blocked because required holding price or FX data is incomplete. "
            "Refresh market data or fix ticker overrides before relying on performance."
        )
        for issue in portfolio_result.issues[:10]:
            st.warning(issue.message)
        if len(portfolio_result.issues) > 10:
            st.warning(f"{len(portfolio_result.issues) - 10} additional price coverage issue(s) not shown.")
        return

    benchmark_series = None
    benchmark_full = None
    try:
        benchmark_full = calculate_benchmark_twr_series(db_path, end_date=end_date, benchmark_ticker=benchmark)
        benchmark_series = apply_date_filter(benchmark_full, start_date, end_date)
    except PerformanceDataError as exc:
        st.warning(f"Benchmark comparison unavailable: {exc}")

    coverage = get_calculated_benchmark_price_coverage(db_path, benchmark)
    if not coverage.has_calculated_prices:
        st.warning(
            f"Calculated benchmark prices for {benchmark} are missing from {coverage.start_date} onward. "
            "Seeded benchmark returns can still display, but the calculated benchmark line needs stored price data."
        )
        st.code(_benchmark_refresh_command(benchmark), language="powershell")
    lag_warning = benchmark_lag_warning(portfolio, benchmark_series, benchmark)
    if lag_warning:
        st.warning(lag_warning)
        st.code(_benchmark_refresh_command(benchmark), language="powershell")

    selected_range = st.radio("Chart range", PERFORMANCE_RANGE_OPTIONS, horizontal=True, index=len(PERFORMANCE_RANGE_OPTIONS) - 1)
    starting_capital = float(controls["starting_capital"])
    growth_data = build_growth_chart_data(
        portfolio,
        benchmark_series,
        starting_capital=starting_capital,
        selected_range=selected_range,
    )
    st.subheader(f"Hypothetical Growth of {format_currency_cad_full(starting_capital)}")
    if growth_data.empty:
        st.warning("No performance data available for the selected date range.")
    else:
        st.altair_chart(_growth_chart(growth_data, selected_range, starting_capital), use_container_width=True)
        st.caption("Y-axis auto-scaled to selected period for readability.")
        note = performance_range_note(portfolio, filter_performance_window(portfolio, selected_range), selected_range)
        if note:
            st.caption(note)
        st.caption("Short-range charts use trading-day spacing; weekends and market holidays are omitted.")
        st.caption("Seeded monthly points and calculated daily points are shown as separate phase series.")

    _render_period_returns(db_path, end_date)
    _render_trailing_annualized_returns(portfolio_full, benchmark_full)
    _render_risk_metrics(db_path, benchmark)


def _render_period_returns(db_path: Path, end_date: str | None) -> None:
    try:
        returns = get_period_returns(db_path, end_date=end_date)
    except PerformanceDataError as exc:
        st.warning(f"Period returns unavailable: {exc}")
        return
    st.subheader("Period Returns")
    cols = st.columns(len(returns))
    for col, (period, value) in zip(cols, returns.items()):
        col.metric(period, format_percent(value))


def _render_trailing_annualized_returns(portfolio: pd.DataFrame, benchmark: pd.DataFrame | None) -> None:
    st.subheader("Trailing Annualized Returns")
    returns = calculate_trailing_annualized_returns(portfolio, benchmark)
    if returns.empty:
        st.info("Annualized returns unavailable.")
        return
    st.dataframe(format_annualized_returns_table(returns), hide_index=True, use_container_width=True)


def _benchmark_refresh_command(benchmark: str) -> str:
    if benchmark == BENCHMARK_TICKER:
        return BENCHMARK_REFRESH_COMMAND
    return (
        "python refresh_benchmark.py --db data/db/sma_dashboard.db "
        f"--benchmark {benchmark} --start-date 2025-02-01"
    )


def _growth_chart(data: pd.DataFrame, selected_range: str, starting_capital: float) -> alt.Chart:
    chart_data = add_observation_axis_fields(data) if should_use_compressed_trading_axis(selected_range) else data
    y_domain = calculate_growth_y_axis_domain(data, starting_capital)
    y_scale = alt.Scale(zero=False, domain=list(y_domain)) if y_domain else alt.Scale(zero=False)
    x_encoding = _growth_x_encoding(chart_data, selected_range)
    return (
        alt.Chart(chart_data)
        .mark_line(point=True)
        .encode(
            x=x_encoding,
            y=alt.Y("growth:Q", title="Value (CAD)", scale=y_scale, axis=alt.Axis(format="$,.0f")),
            color=alt.Color("series:N", title="Series"),
            strokeDash=alt.StrokeDash("phase:N", title="Phase"),
            order=alt.Order("date:T"),
            tooltip=[
                alt.Tooltip("date:T", title="Date", format="%Y-%m-%d"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("growth_display:N", title="Growth"),
                alt.Tooltip("phase:N", title="Phase"),
            ],
        )
        .properties(height=360)
    )


def _growth_x_encoding(data: pd.DataFrame, selected_range: str) -> alt.X:
    if should_use_compressed_trading_axis(selected_range):
        labels = select_observation_axis_labels(data, selected_range)
        values = [label["index"] for label in labels]
        label_expr = _axis_label_expr(labels)
        max_index = max(values) if values else 0
        return alt.X(
            "observation_index:Q",
            title="Date",
            scale=alt.Scale(domain=[-0.1, max_index + 0.1]),
            axis=alt.Axis(values=values, labelExpr=label_expr, labelAngle=0),
        )

    x_ticks = [date.to_pydatetime() for date in select_calendar_axis_tick_dates(data, selected_range)]
    return alt.X(
        "date:T",
        title="Date",
        axis=alt.Axis(format="%Y", labelAngle=0, values=x_ticks),
    )


def _axis_label_expr(labels: list[dict[str, object]]) -> str:
    if not labels:
        return "''"
    expression = "''"
    for label in reversed(labels):
        text = str(label["label"]).replace("\\", "\\\\").replace("'", "\\'")
        expression = f"datum.value === {label['index']} ? '{text}' : {expression}"
    return expression


def _render_risk_metrics(db_path: Path, benchmark: str) -> None:
    st.subheader("Risk Metrics")
    try:
        portfolio_returns = calculate_daily_portfolio_returns(db_path)
        benchmark_returns = get_benchmark_returns(
            db_path,
            benchmark_ticker=benchmark,
            start_date=portfolio_returns["date"].min(),
            end_date=portfolio_returns["date"].max(),
        )
        metrics = calculate_risk_metrics(portfolio_returns, benchmark_returns)
    except PerformanceDataError as exc:
        st.warning(f"Risk metrics unavailable: {exc}")
        return
    st.dataframe(format_risk_metrics_table(metrics), hide_index=True, use_container_width=True)


def _render_rolling_metrics(db_path: Path, controls: dict[str, object]) -> None:
    st.header("Rolling Metrics")
    try:
        portfolio_returns = calculate_daily_portfolio_returns(db_path)
        benchmark_returns = get_benchmark_returns(
            db_path,
            benchmark_ticker=str(controls["benchmark"]),
            start_date=portfolio_returns["date"].min(),
            end_date=portfolio_returns["date"].max(),
        )
        rolling = calculate_rolling_metrics(portfolio_returns, benchmark_returns)
    except PerformanceDataError as exc:
        st.warning(f"Rolling metrics unavailable because benchmark or return data is incomplete: {exc}")
        return
    chart_data = prepare_rolling_metrics_chart_data(rolling)
    latest = latest_rolling_metrics(rolling)
    if chart_data.empty or latest is None:
        st.info("Not enough daily calculated return history for 12-month rolling metrics.")
        return
    cols = st.columns(3)
    cols[0].metric("Latest Rolling Sharpe", format_ratio(latest["rolling_sharpe"]))
    cols[1].metric("Latest Tracking Error", format_percent(latest["rolling_tracking_error"]))
    cols[2].metric("Latest Metric Date", pd.to_datetime(latest["date"]).date().isoformat())
    st.altair_chart(_rolling_metrics_chart(chart_data), use_container_width=True)
    st.caption(
        "Rolling metrics are calculated over trailing 12-month windows using valid daily observations. "
        "Tracking error is annualized and shown on the right axis."
    )


def _rolling_metrics_chart(data: pd.DataFrame) -> alt.LayerChart:
    x_ticks = [date.to_pydatetime() for date in select_rolling_metrics_axis_tick_dates(data)]
    base = alt.Chart(data).encode(
        x=alt.X(
            "date:T",
            title="Date",
            axis=alt.Axis(format="%b %Y", labelAngle=0, values=x_ticks),
        ),
        tooltip=[
            alt.Tooltip("date:T", title="Date", format="%Y-%m-%d"),
            alt.Tooltip("rolling_sharpe_display:N", title="Rolling Sharpe"),
            alt.Tooltip("tracking_error_display:N", title="Tracking Error"),
        ],
    )
    sharpe = base.mark_line(color="#2563eb").encode(
        y=alt.Y(
            "rolling_sharpe:Q",
            title="Rolling Sharpe Ratio",
            scale=alt.Scale(zero=False),
            axis=alt.Axis(titleColor="#2563eb"),
        )
    )
    tracking_error = base.mark_line(color="#dc2626").encode(
        y=alt.Y(
            "tracking_error_pct:Q",
            title="Tracking Error, Annualized %",
            scale=alt.Scale(zero=False),
            axis=alt.Axis(titleColor="#dc2626", format=".1f"),
        )
    )
    return alt.layer(sharpe, tracking_error).resolve_scale(y="independent").properties(height=320)


def _render_holdings_and_valuation(db_path: Path, include_live_data: bool = True) -> None:
    st.header("Holdings And Valuation")
    try:
        holdings = get_latest_holdings_snapshot(db_path)
    except HoldingsDataError as exc:
        st.warning(str(exc))
        return
    st.subheader("Latest Holdings")
    st.dataframe(
        format_holdings_table(holdings),
        hide_index=True,
        use_container_width=True,
    )
    if not include_live_data:
        st.info("Live valuation enrichment is disabled in synthetic demo mode.")
        _render_qtd_holding_returns(db_path)
        return
    try:
        valuation = get_portfolio_valuation(db_path)
    except (ValuationDataError, HoldingsDataError) as exc:
        st.warning(f"Valuation data unavailable: {exc}")
        return
    except Exception as exc:
        st.warning(f"Valuation data unavailable from yfinance: {exc}")
        return
    if valuation.holdings.empty:
        st.warning("No valuation data available.")
        return
    st.subheader("Holding Valuation Metrics")
    st.dataframe(
        prepare_valuation_table_for_display(valuation.holdings),
        hide_index=True,
        use_container_width=True,
        column_config=_valuation_column_config(),
    )
    st.subheader("Portfolio-Weighted Valuation Averages")
    st.dataframe(format_weighted_valuation_averages(valuation.weighted_averages), hide_index=True, use_container_width=True)
    _render_qtd_holding_returns(db_path)


def _valuation_column_config() -> dict[str, object]:
    """Column configuration for valuation table display.

    - Dividend Yield: format decimal yields as percentages
    - Market Cap: now a text column (pre-formatted as C$X,XXX,XXX in prepare_valuation_table_for_display)
    - All others: numeric with appropriate decimal places
    """
    return {
        "Weight": st.column_config.NumberColumn(
            "Weight", format="%.2f%%"
        ),
        "Trailing P/E": st.column_config.NumberColumn(
            "Trailing P/E", format="%.2f"
        ),
        "Forward P/E": st.column_config.NumberColumn(
            "Forward P/E", format="%.2f"
        ),
        "Price / Book": st.column_config.NumberColumn(
            "Price / Book", format="%.2f"
        ),
        "EV / EBITDA": st.column_config.NumberColumn(
            "EV / EBITDA", format="%.2f"
        ),
        "Dividend Yield": st.column_config.NumberColumn(
            "Dividend Yield", format="percent"
        ),
        "Market Cap (CAD)": st.column_config.TextColumn(
            "Market Cap (CAD)"
        ),
    }


def _render_model_update_status(db_path: Path) -> None:
    """Display model update ingestion status and applied-update history."""
    st.subheader("Model Update Status")

    try:
        applied = get_model_updates_applied(db_path)
        if applied.empty:
            st.info("No model updates have been applied yet.")
            return

        latest = applied.iloc[0]
        st.success(
            f"Latest model applied: **{latest['model_date']}** "
            f"(ingested {latest['ingested_at'].strftime('%Y-%m-%d %H:%M')})"
        )
        st.dataframe(
            applied[["model_date", "file_name", "ingested_at"]].copy(),
            hide_index=True,
            use_container_width=True,
        )
    except Exception as exc:
        st.warning(f"Could not display model update status: {exc}")


def _render_qtd_holding_returns(db_path: Path) -> None:
    st.subheader("Current Holdings QTD Price Returns")
    try:
        ranking = calculate_qtd_holding_price_returns(db_path)
    except PerformanceDataError as exc:
        st.warning(f"QTD holding returns unavailable: {exc}")
        return
    if ranking.issues:
        st.warning(f"Excluded {len(ranking.issues)} holdings from QTD return ranking because required price or FX data was missing.")
    if ranking.returns.empty:
        st.info("No current holdings have enough stored price data for QTD return ranking.")
        return
    cols = st.columns(2)
    cols[0].markdown("Top 5")
    cols[0].dataframe(format_qtd_holding_returns_table(ranking.top), hide_index=True, use_container_width=True)
    cols[1].markdown("Bottom 5")
    cols[1].dataframe(format_qtd_holding_returns_table(ranking.bottom), hide_index=True, use_container_width=True)


def _render_trade_log(db_path: Path) -> None:
    st.header("Trade Log")
    try:
        tickers, actions = list_trade_filter_values(db_path)
    except Exception as exc:
        st.warning(f"Trade log unavailable: {exc}")
        return
    if not tickers and not actions:
        st.info("No trades found.")
        return
    cols = st.columns(4)
    ticker = cols[0].selectbox("Ticker", [""] + tickers)
    action = cols[1].selectbox("Action", [""] + actions)
    start_date = cols[2].date_input("Trade start", value=None)
    end_date = cols[3].date_input("Trade end", value=None)
    trades = load_trade_log(
        db_path,
        ticker=ticker or None,
        action=action or None,
        start_date=start_date.isoformat() if start_date else None,
        end_date=end_date.isoformat() if end_date else None,
    )
    if trades.empty:
        st.info("No trades found for the selected filters.")
        return
    st.dataframe(format_trade_log_table(trades), hide_index=True, use_container_width=True)


def _render_news(db_path: Path, include_live_data: bool = True) -> None:
    st.header("News Feed")
    if not include_live_data:
        st.info("Live news enrichment is disabled in synthetic demo mode.")
        return
    try:
        news = get_holding_news(db_path)
    except HoldingsDataError as exc:
        st.warning(str(exc))
        return
    except Exception as exc:
        st.warning(f"News unavailable from yfinance: {exc}")
        return
    if news.empty:
        st.info("No news available.")
        return
    st.dataframe(format_news_table(news), hide_index=True, use_container_width=True)


def _render_chat(db_path: Path) -> None:
    """Render the offline demo assistant or optional Anthropic agent.

    Conversation history is stored in st.session_state so it persists across
    Streamlit reruns. Charts emitted via render_chart are stored in session
    state and rendered via native Streamlit widgets (no LLM-generated code).
    """
    st.header("Portfolio Assistant")

    chat_mode = os.environ.get("SMA_CHAT_MODE", "mock").strip().lower()
    if chat_mode not in {"mock", "anthropic"}:
        st.warning("SMA_CHAT_MODE must be 'mock' or 'anthropic'.")
        return

    api_key: str | None = None
    if chat_mode == "anthropic":
        try:
            api_key = st.secrets["ANTHROPIC_API_KEY"]
        except (KeyError, FileNotFoundError):
            st.info(
                "Anthropic mode requires an API key. "
                "Add `ANTHROPIC_API_KEY = '...'` to `.streamlit/secrets.toml`."
            )
            return
        st.caption("Live Anthropic mode. Portfolio tools read the selected local database.")
    else:
        st.caption("Offline mock mode: deterministic answers from synthetic local data; no API key or network call.")

    # --- Session state init ---
    if "chat_display" not in st.session_state:
        st.session_state["chat_display"] = []  # list of display dicts
    if "api_messages" not in st.session_state:
        st.session_state["api_messages"] = []  # Anthropic API message history

    # --- Render existing chat history ---
    for entry in st.session_state["chat_display"]:
        role = entry["role"]
        with st.chat_message(role):
            if entry.get("content"):
                st.markdown(entry["content"])
            for status in entry.get("tool_statuses", []):
                st.caption(f"Tool: {status}")
            for chart in entry.get("charts", []):
                _render_chart_widget(chart)

    # --- User input ---
    user_input = st.chat_input("Ask about your portfolio…")
    if not user_input:
        return

    # Display user message immediately
    st.session_state["chat_display"].append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Run agent loop
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                from sma_dashboard.chatbot import run_agent_loop, run_mock_agent_loop
                if chat_mode == "mock":
                    response, updated_messages = run_mock_agent_loop(
                        user_message=user_input,
                        messages_history=st.session_state["api_messages"],
                        db_path=db_path,
                    )
                else:
                    response, updated_messages = run_agent_loop(
                        user_message=user_input,
                        messages_history=st.session_state["api_messages"],
                        db_path=db_path,
                        api_key=api_key,
                    )
            except Exception as exc:
                st.error(f"Agent error: {exc}")
                return

        st.session_state["api_messages"] = updated_messages

        # Display tool status feedback
        for status in response.tool_statuses:
            st.caption(f"Tool: {status}")

        # Display final text
        if response.text:
            st.markdown(response.text)

        # Render any charts emitted by render_chart tool
        for chart in response.charts:
            _render_chart_widget(chart)

        # Persist assistant turn in display state
        st.session_state["chat_display"].append({
            "role": "assistant",
            "content": response.text,
            "tool_statuses": response.tool_statuses,
            "charts": response.charts,
        })


def _render_chart_widget(chart: dict) -> None:
    """Render a render_chart payload using native Streamlit widgets.

    chart_type determines the widget used. No LLM-generated code is executed
    (SPEC.md §5 rendering rationale, Path A).
    """
    import pandas as pd

    chart_type = chart.get("chart_type")
    title = chart.get("title", "")
    data = chart.get("data", {})

    if title:
        st.caption(title)

    if chart_type == "table":
        columns = data.get("columns", [])
        rows = data.get("rows", [])
        if columns and rows is not None:
            st.dataframe(pd.DataFrame(rows, columns=columns), use_container_width=True)
        else:
            st.info("No table data available.")

    elif chart_type in ("line", "bar"):
        x_values = data.get("x", [])
        series_list = data.get("series", [])
        if not x_values or not series_list:
            st.info("No chart data available.")
            return
        chart_df = pd.DataFrame({"x": x_values})
        for s in series_list:
            chart_df[s["name"]] = s.get("values", [])
        chart_df = chart_df.set_index("x")
        if chart_type == "line":
            st.line_chart(chart_df)
        else:
            st.bar_chart(chart_df)

    else:
        st.info(f"Unsupported chart type: {chart_type}")


if __name__ == "__main__":
    run_app()
