"""M5: Anthropic tool-calling agent loop for the SMA Dashboard chatbot.

Pattern (SPEC.md §5):
  1. User question + tool defs → Claude via Anthropic API
  2. Claude returns tool_use block(s) or final text
  3. App executes custom tool(s) in Python
  4. Results returned as tool_result blocks
  5. Repeat until Claude returns plain text (cap: MAX_ITERATIONS)

web_search (type=web_search_20250305) is a server-side built-in tool — the
Anthropic API executes searches automatically; no local implementation needed.

render_chart is a rendering instruction: returns a structured dict that the
Streamlit dashboard renders via native widgets. No LLM-generated code is
executed.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sma_dashboard.dashboard_support import load_trade_log
from sma_dashboard.db import DEFAULT_DB_PATH, connect
from sma_dashboard.holdings import HoldingsDataError, get_latest_holdings_snapshot
from sma_dashboard.performance import (
    BENCHMARK_TICKER,
    PerformanceDataError,
    calculate_risk_metrics,
    get_benchmark_period_return,
    get_period_return,
)
from sma_dashboard.transcripts import search_transcripts
from sma_dashboard.valuation import ValuationDataError, get_holding_valuations, get_portfolio_valuation


MAX_ITERATIONS = 5


# ---------------------------------------------------------------------------
# Tool definitions — exactly as specified in SPEC.md §5
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_holdings",
        "description": (
            "Get portfolio holdings (positions, weights) as of the most recent "
            "model update, or as of a specific past date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of_date": {
                    "type": "string",
                    "description": "Optional. ISO date (YYYY-MM-DD). Omit for latest snapshot.",
                },
                "ticker": {
                    "type": "string",
                    "description": "Optional. Filter to a single ticker. Omit for full portfolio.",
                },
            },
        },
    },
    {
        "name": "get_performance",
        "description": "Get portfolio return and risk metrics over a given period, optionally vs benchmark.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["1D", "MTD", "QTD", "YTD", "since_inception", "custom"],
                    "description": "Default 'YTD' if omitted.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Required if period='custom'.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Required if period='custom'.",
                },
                "include_benchmark": {
                    "type": "boolean",
                    "description": "Include ^GSPTSE comparison. Default true.",
                },
                "include_risk_metrics": {
                    "type": "boolean",
                    "description": (
                        "Include vol, max drawdown, Sharpe, beta/alpha, tracking error, "
                        "information ratio. Default false."
                    ),
                },
            },
        },
    },
    {
        "name": "get_valuation",
        "description": (
            "Get valuation/fundamental multiples for one or more holdings, "
            "or portfolio-weighted averages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Optional. Single ticker. Omit for full portfolio.",
                },
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "pe_trailing",
                            "pe_forward",
                            "pb",
                            "ev_ebitda",
                            "dividend_yield",
                            "market_cap",
                        ],
                    },
                    "description": "Optional. Omit to return all available metrics.",
                },
                "portfolio_weighted_average": {
                    "type": "boolean",
                    "description": (
                        "If true and ticker omitted, return portfolio-weighted average "
                        "instead of per-holding breakdown."
                    ),
                },
            },
        },
    },
    {
        "name": "get_trades",
        "description": "Get the trade/model-update history (buys, sells, trims, adds).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Optional. Filter to one ticker.",
                },
                "action": {
                    "type": "string",
                    "enum": ["buy", "sell", "trim", "add"],
                    "description": "Optional. Filter by action type.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Optional. Filter range start.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Optional. Filter range end.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional. Max trades returned, most recent first. Default 10.",
                },
            },
        },
    },
    # Anthropic built-in server-side tool — no description/input_schema needed.
    # The API handles web_search execution; include only the type + name.
    {
        "type": "web_search_20250305",
        "name": "web_search",
    },
    {
        "name": "search_transcripts",
        "description": (
            "Retrieve text from past quarterly investment manager call transcripts. "
            "Defaults to the most recent call. Use when the question asks what the "
            "manager said, thought, or explained about a holding, sector, or decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "quarter_label": {
                    "type": "string",
                    "description": "Optional. Specific quarter, e.g. '2025_Q2'. Omit for most recent.",
                },
                "date_range_start": {
                    "type": "string",
                    "description": "Optional. ISO date. Pairs with date_range_end for a multi-quarter range.",
                },
                "date_range_end": {
                    "type": "string",
                    "description": "Optional. ISO date.",
                },
                "n_most_recent": {
                    "type": "integer",
                    "description": "Optional. Number of most recent transcripts if no specific quarter/date given. Default 1.",
                },
            },
        },
    },
    {
        "name": "render_chart",
        "description": (
            "Render a chart or table inline in the chat. Call after gathering data, "
            "when a visual would help answer the question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["line", "bar", "table"],
                },
                "title": {"type": "string"},
                "data": {
                    "type": "object",
                    "description": (
                        "Shape depends on chart_type. "
                        "line/bar: {x: [...], series: [{name: str, values: [...]}]}. "
                        "table: {columns: [...], rows: [[...], ...]}."
                    ),
                },
            },
            "required": ["chart_type", "title", "data"],
        },
    },
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class AgentResponse:
    """Result of one complete agent loop turn."""
    text: str
    charts: list[dict[str, Any]] = field(default_factory=list)
    tool_statuses: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent_loop(
    user_message: str,
    messages_history: list[dict[str, Any]],
    db_path: Path | str = DEFAULT_DB_PATH,
    api_key: str | None = None,
) -> tuple[AgentResponse, list[dict[str, Any]]]:
    """Run the Anthropic tool-calling agent loop for one user turn.

    Args:
        user_message: The user's latest question.
        messages_history: Prior Anthropic API messages (user+assistant turns).
            This list is not mutated; a new list is returned with the turn appended.
        db_path: SQLite database path for tool execution.
        api_key: Anthropic API key. Pass from st.secrets in Streamlit callers.

    Returns:
        (AgentResponse, updated_messages) — the response plus the full updated
        messages list for persisting in session state.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    messages = list(messages_history) + [{"role": "user", "content": user_message}]

    charts: list[dict[str, Any]] = []
    tool_statuses: list[str] = []

    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        # Append assistant turn to message history
        messages.append({"role": "assistant", "content": response.content})

        # No tool calls — final answer
        if response.stop_reason == "end_turn":
            text = _extract_text(response.content)
            return AgentResponse(text=text, charts=charts, tool_statuses=tool_statuses), messages

        # Collect tool_use blocks (skip web_search — handled server-side)
        tool_use_blocks = [
            block for block in response.content
            if hasattr(block, "type") and block.type == "tool_use"
            and block.name != "web_search"
        ]

        if not tool_use_blocks:
            # Only web_search or nothing — extract text and return
            text = _extract_text(response.content)
            return AgentResponse(text=text, charts=charts, tool_statuses=tool_statuses), messages

        # Execute each custom tool
        tool_results: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            status, result, chart = _execute_tool(block.name, block.input, db_path)
            tool_statuses.append(status)
            if chart:
                charts.append(chart)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    # Iteration cap hit — return whatever text we have
    text = _extract_text(response.content)
    tool_statuses.append("Agent loop capped at maximum iterations.")
    return AgentResponse(text=text or "(No response — iteration limit reached.)", charts=charts, tool_statuses=tool_statuses), messages


# Public compatibility name used by installation smoke tests and external callers.
call_claude = run_agent_loop


def run_mock_agent_loop(
    user_message: str,
    messages_history: list[dict[str, Any]],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> tuple[AgentResponse, list[dict[str, Any]]]:
    """Answer common demo questions locally without an API key or network call."""
    question = user_message.strip().lower()
    charts: list[dict[str, Any]] = []
    statuses: list[str] = []

    if any(term in question for term in ("transcript", "manager", "commentary", "quarter")):
        result = execute_search_transcripts(db_path=db_path, n_most_recent=1)
        statuses.append(f"Retrieved {result.get('count', 0)} synthetic transcript(s).")
        if "error" in result:
            text = result["error"]
        else:
            transcript = result["transcripts"][0]
            excerpt = " ".join(transcript["full_text"].split())[:600]
            text = (
                f"The latest synthetic commentary is **{transcript['quarter_label']}** "
                f"({transcript['date']}): {excerpt}"
            )
    elif any(term in question for term in ("trade", "rebalance", "buy", "sell", "trim", "add")):
        result = execute_get_trades(db_path=db_path, limit=10)
        statuses.append(f"Fetched {result.get('count', 0)} synthetic trades.")
        if "error" in result:
            text = result["error"]
        else:
            rows = result["trades"]
            text = f"I found **{len(rows)}** recent synthetic trades."
            charts.append(
                execute_render_chart(
                    "table",
                    "Recent synthetic trades",
                    {
                        "columns": ["Date", "Ticker", "Action", "Weight change"],
                        "rows": [
                            [row["date"], row["ticker"], row["action"], row["weight_change"]]
                            for row in rows
                        ],
                    },
                )
            )
    elif any(term in question for term in ("holding", "allocation", "weight", "position")):
        result = execute_get_holdings(db_path=db_path)
        statuses.append(f"Fetched {result.get('count', 0)} synthetic holdings.")
        if "error" in result:
            text = result["error"]
        else:
            rows = result["holdings"]
            text = f"The latest synthetic snapshot contains **{len(rows)}** holdings."
            charts.append(
                execute_render_chart(
                    "bar",
                    "Latest synthetic allocation (%)",
                    {
                        "x": [row["ticker"] for row in rows],
                        "series": [{"name": "Weight", "values": [row["weight"] for row in rows]}],
                    },
                )
            )
    else:
        period = "since_inception" if "since inception" in question else "YTD"
        result = execute_get_performance(db_path=db_path, period=period, include_benchmark=True)
        statuses.append(f"Calculated {period} performance from the synthetic database.")
        if "error" in result:
            text = result["error"]
        else:
            text = f"Synthetic portfolio {period} TWR is **{result['portfolio_twr_pct']}**."
            if "benchmark_twr_pct" in result:
                text += f" The benchmark is **{result['benchmark_twr_pct']}**."

    updated_messages = list(messages_history)
    updated_messages.append({"role": "user", "content": user_message})
    updated_messages.append({"role": "assistant", "content": text})
    return AgentResponse(text=text, charts=charts, tool_statuses=statuses), updated_messages


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

def execute_get_holdings(
    db_path: Path | str = DEFAULT_DB_PATH,
    as_of_date: str | None = None,
    ticker: str | None = None,
) -> dict[str, Any]:
    """Return portfolio holdings as a serializable dict."""
    try:
        holdings = get_latest_holdings_snapshot(db_path, as_of_date=as_of_date)
    except HoldingsDataError as exc:
        return {"error": str(exc)}

    if ticker:
        holdings = holdings[holdings["ticker"].str.upper() == ticker.upper()]
        if holdings.empty:
            return {"error": f"Ticker {ticker} not found in holdings snapshot."}

    rows = holdings[["date", "ticker", "weight", "weight_decimal", "shares", "cost_basis"]].to_dict(orient="records")
    return {
        "as_of_date": str(holdings["date"].iloc[0]) if not holdings.empty else as_of_date,
        "holdings": rows,
        "count": len(rows),
    }


def execute_get_performance(
    db_path: Path | str = DEFAULT_DB_PATH,
    period: str = "YTD",
    start_date: str | None = None,
    end_date: str | None = None,
    include_benchmark: bool = True,
    include_risk_metrics: bool = False,
) -> dict[str, Any]:
    """Return TWR and optionally benchmark + risk metrics."""
    result: dict[str, Any] = {"period": period}

    try:
        portfolio_twr = get_period_return(
            period=period,
            db_path=db_path,
            start_date=start_date,
            end_date=end_date,
        )
        result["portfolio_twr"] = portfolio_twr
        result["portfolio_twr_pct"] = f"{portfolio_twr * 100:.2f}%"
    except PerformanceDataError as exc:
        return {"error": str(exc)}

    if include_benchmark:
        try:
            benchmark_twr = get_benchmark_period_return(
                period=period,
                db_path=db_path,
                start_date=start_date,
                end_date=end_date,
            )
            result["benchmark_twr"] = benchmark_twr
            result["benchmark_twr_pct"] = f"{benchmark_twr * 100:.2f}%"
            result["excess_return_pct"] = f"{(portfolio_twr - benchmark_twr) * 100:.2f}%"
        except PerformanceDataError as exc:
            result["benchmark_error"] = str(exc)

    if include_risk_metrics:
        try:
            from sma_dashboard.performance import (
                calculate_daily_portfolio_returns,
                get_benchmark_returns,
            )
            port_returns = calculate_daily_portfolio_returns(db_path)
            bench_returns = get_benchmark_returns(
                db_path,
                benchmark_ticker=BENCHMARK_TICKER,
                start_date=port_returns["date"].min(),
                end_date=port_returns["date"].max(),
            )
            metrics = calculate_risk_metrics(port_returns, bench_returns)
            result["risk_metrics"] = {
                "annualized_volatility": metrics.annualized_volatility,
                "maximum_drawdown": metrics.maximum_drawdown,
                "sharpe_ratio": metrics.sharpe_ratio,
                "beta": metrics.beta,
                "alpha": metrics.alpha,
                "tracking_error": metrics.tracking_error,
                "information_ratio": metrics.information_ratio,
            }
        except PerformanceDataError as exc:
            result["risk_metrics_error"] = str(exc)

    return result


def execute_get_valuation(
    db_path: Path | str = DEFAULT_DB_PATH,
    ticker: str | None = None,
    metrics: list[str] | None = None,
    portfolio_weighted_average: bool = False,
) -> dict[str, Any]:
    """Return per-holding or portfolio-weighted valuation multiples."""
    try:
        if portfolio_weighted_average and not ticker:
            valuation = get_portfolio_valuation(db_path)
            return {
                "type": "portfolio_weighted_average",
                "weighted_averages": valuation.weighted_averages,
            }

        holdings_df = get_holding_valuations(db_path)

        if ticker:
            holdings_df = holdings_df[holdings_df["ticker"].str.upper() == ticker.upper()]
            if holdings_df.empty:
                return {"error": f"Ticker {ticker} not found in current holdings."}

        # Select requested metrics
        metric_cols = metrics if metrics else [
            "pe_trailing", "pe_forward", "pb", "ev_ebitda", "dividend_yield", "market_cap"
        ]
        keep_cols = ["ticker", "weight"] + [c for c in metric_cols if c in holdings_df.columns]
        rows = holdings_df[keep_cols].to_dict(orient="records")
        return {"type": "per_holding", "holdings": rows}

    except (ValuationDataError, HoldingsDataError) as exc:
        return {"error": str(exc)}


def execute_get_trades(
    db_path: Path | str = DEFAULT_DB_PATH,
    ticker: str | None = None,
    action: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Return trade/model-update history with optional filters."""
    try:
        trades = load_trade_log(
            db_path=db_path,
            ticker=ticker,
            action=action,
            start_date=start_date,
            end_date=end_date,
        )
        trades = trades.head(limit)
        return {
            "trades": trades.to_dict(orient="records"),
            "count": len(trades),
        }
    except Exception as exc:
        return {"error": str(exc)}


def execute_search_transcripts(
    db_path: Path | str = DEFAULT_DB_PATH,
    quarter_label: str | None = None,
    date_range_start: str | None = None,
    date_range_end: str | None = None,
    n_most_recent: int = 1,
) -> dict[str, Any]:
    """Retrieve transcript text by quarter, date range, or recency."""
    transcripts = search_transcripts(
        db_path=db_path,
        quarter_label=quarter_label,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        n_most_recent=n_most_recent,
    )
    if not transcripts:
        return {"error": "No transcripts found matching the requested filters."}
    return {
        "transcripts": [
            {
                "quarter_label": t.quarter_label,
                "date": t.date,
                "notes": t.notes,
                "full_text": t.full_text,
            }
            for t in transcripts
        ],
        "count": len(transcripts),
    }


def execute_render_chart(
    chart_type: str,
    title: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Return a rendering-instruction dict for the dashboard to draw.

    Not a data tool — renders nothing here. The Streamlit dashboard receives
    this dict and renders it via st.line_chart / st.bar_chart / st.dataframe.
    """
    return {
        "chart_type": chart_type,
        "title": title,
        "data": data,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _execute_tool(
    name: str,
    tool_input: dict[str, Any],
    db_path: Path | str,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Dispatch a tool call and return (status_message, result, chart_or_None)."""
    chart: dict[str, Any] | None = None

    if name == "get_holdings":
        result = execute_get_holdings(
            db_path=db_path,
            as_of_date=tool_input.get("as_of_date"),
            ticker=tool_input.get("ticker"),
        )
        date_label = result.get("as_of_date", "latest")
        status = f"Fetched holdings as of {date_label}."

    elif name == "get_performance":
        result = execute_get_performance(
            db_path=db_path,
            period=tool_input.get("period", "YTD"),
            start_date=tool_input.get("start_date"),
            end_date=tool_input.get("end_date"),
            include_benchmark=tool_input.get("include_benchmark", True),
            include_risk_metrics=tool_input.get("include_risk_metrics", False),
        )
        status = f"Fetched {tool_input.get('period', 'YTD')} performance."

    elif name == "get_valuation":
        result = execute_get_valuation(
            db_path=db_path,
            ticker=tool_input.get("ticker"),
            metrics=tool_input.get("metrics"),
            portfolio_weighted_average=tool_input.get("portfolio_weighted_average", False),
        )
        label = tool_input.get("ticker") or "portfolio"
        status = f"Fetched valuation for {label}."

    elif name == "get_trades":
        result = execute_get_trades(
            db_path=db_path,
            ticker=tool_input.get("ticker"),
            action=tool_input.get("action"),
            start_date=tool_input.get("start_date"),
            end_date=tool_input.get("end_date"),
            limit=tool_input.get("limit", 10),
        )
        status = f"Fetched {result.get('count', 0)} trades."

    elif name == "search_transcripts":
        result = execute_search_transcripts(
            db_path=db_path,
            quarter_label=tool_input.get("quarter_label"),
            date_range_start=tool_input.get("date_range_start"),
            date_range_end=tool_input.get("date_range_end"),
            n_most_recent=tool_input.get("n_most_recent", 1),
        )
        label = tool_input.get("quarter_label") or "most recent"
        status = f"Retrieved {result.get('count', 0)} transcript(s) ({label})."

    elif name == "render_chart":
        result = execute_render_chart(
            chart_type=tool_input["chart_type"],
            title=tool_input["title"],
            data=tool_input["data"],
        )
        chart = result
        status = f"Chart ready: {tool_input.get('title', '')} ({tool_input.get('chart_type', '')})."

    else:
        result = {"error": f"Unknown tool: {name}"}
        status = f"Unknown tool '{name}' called."

    return status, result, chart


def _extract_text(content: list[Any]) -> str:
    """Extract concatenated text from a list of Anthropic content blocks."""
    parts = []
    for block in content:
        if hasattr(block, "type") and block.type == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()
