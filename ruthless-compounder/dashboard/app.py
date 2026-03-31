"""
RuthlessCompounder Dashboard — Streamlit UI

Live TVL, P&L, transaction logs, LLM reasoning, next run time, and tx budget.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml

from dashboard.components import (
    render_allocation_pie,
    render_pnl_chart,
    render_risk_events_table,
    render_tx_budget_gauge,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "history.db"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f.read())
    return {}


# ---------------------------------------------------------------------------
# Page Setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="RuthlessCompounder",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("RuthlessCompounder Dashboard")

config = load_config()
mode = config.get("mode", "unknown")
st.caption(f"Mode: **{mode.upper()}** | Refreshes every 30s")

# Auto-refresh
st.empty()

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Controls")

    # Emergency pause
    pause_file = Path(config.get("security", {}).get("emergency_pause_file", "/tmp/ruthless_pause"))
    is_paused = pause_file.exists()

    if is_paused:
        st.error("AGENT IS PAUSED")
        if st.button("Resume Agent"):
            pause_file.unlink(missing_ok=True)
            st.rerun()
    else:
        st.success("Agent Running")
        if st.button("EMERGENCY PAUSE", type="primary"):
            pause_file.touch()
            st.rerun()

    st.divider()

    # Schedule info
    compound_hour = config.get("schedule", {}).get("daily_compound_hour_utc", 0)
    sentinel_min = config.get("schedule", {}).get("risk_sentinel_interval_min", 5)
    st.metric("Next Compound (UTC)", f"{compound_hour:02d}:00")
    st.metric("Sentinel Interval", f"{sentinel_min} min")

    st.divider()

    # Allocation targets
    st.subheader("Allocation Targets")
    alloc = config.get("allocation", {})
    st.write(f"Solana Yield: **{alloc.get('solana_yield_pct', 0)}%**")
    st.write(f"EVM Yield: **{alloc.get('evm_yield_pct', 0)}%**")
    st.write(f"Trading Sleeve: **{alloc.get('trading_sleeve_pct', 0)}%**")

    st.divider()
    st.caption(f"Now: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

# ---------------------------------------------------------------------------
# Main Content — Tabs
# ---------------------------------------------------------------------------
tab_overview, tab_txs, tab_llm, tab_risk, tab_config = st.tabs(
    ["Overview", "Transactions", "LLM Decisions", "Risk Events", "Config"]
)

# ── Tab: Overview ───────────────────────────────────────────────────────
with tab_overview:
    try:
        conn = get_db()

        # Latest snapshot
        snap = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()

        if snap:
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total TVL", f"${snap['total_tvl']:,.2f}")
            col2.metric("Solana TVL", f"${snap['solana_tvl']:,.2f}")
            col3.metric("EVM TVL", f"${snap['evm_tvl']:,.2f}")
            col4.metric("Sleeve TVL", f"${snap['sleeve_tvl']:,.2f}")

            if snap["pnl_24h"]:
                st.metric("24h P&L", f"${snap['pnl_24h']:,.2f}",
                          delta=f"${snap['pnl_24h']:,.2f}")
        else:
            st.info("No portfolio snapshots yet. Waiting for first compound cycle.")

        # TVL history chart
        snaps_df = pd.read_sql(
            "SELECT ts, total_tvl, solana_tvl, evm_tvl, sleeve_tvl FROM portfolio_snapshots ORDER BY ts",
            conn,
        )
        if not snaps_df.empty:
            st.subheader("TVL History")
            render_pnl_chart(snaps_df)

        # TX Budget gauge
        st.subheader("Transaction Budget")
        tx_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM transactions WHERE ts >= datetime('now', 'start of year')"
        ).fetchone()
        tx_used = tx_count["cnt"] if tx_count else 0
        tx_cap = config.get("tx_budget", {}).get("max_annual_transactions", 295000)
        render_tx_budget_gauge(tx_used, tx_cap)

        # Allocation pie
        if snap:
            st.subheader("Current Allocation")
            render_allocation_pie(
                snap["solana_tvl"] or 0,
                snap["evm_tvl"] or 0,
                snap["sleeve_tvl"] or 0,
            )

        conn.close()
    except Exception as e:
        st.error(f"Database error: {e}")

# ── Tab: Transactions ──────────────────────────────────────────────────
with tab_txs:
    try:
        conn = get_db()
        txs = pd.read_sql(
            "SELECT * FROM transactions ORDER BY ts DESC LIMIT 200", conn
        )
        conn.close()

        if not txs.empty:
            st.dataframe(txs, use_container_width=True, hide_index=True)
            st.caption(f"Showing last {len(txs)} transactions")
        else:
            st.info("No transactions recorded yet.")
    except Exception as e:
        st.error(f"Error loading transactions: {e}")

# ── Tab: LLM Decisions ─────────────────────────────────────────────────
with tab_llm:
    try:
        conn = get_db()
        decisions = conn.execute(
            "SELECT * FROM llm_decisions ORDER BY ts DESC LIMIT 20"
        ).fetchall()
        conn.close()

        if decisions:
            for d in decisions:
                with st.expander(f"Decision @ {d['ts']}", expanded=(d == decisions[0])):
                    st.subheader("Response")
                    st.text(d["response"][:2000] if d["response"] else "N/A")
                    st.subheader("Actions")
                    if d["actions"]:
                        try:
                            actions = json.loads(d["actions"])
                            st.json(actions)
                        except json.JSONDecodeError:
                            st.text(d["actions"])
        else:
            st.info("No LLM decisions yet. Waiting for first compound cycle.")
    except Exception as e:
        st.error(f"Error loading decisions: {e}")

# ── Tab: Risk Events ───────────────────────────────────────────────────
with tab_risk:
    try:
        conn = get_db()
        events = pd.read_sql(
            "SELECT * FROM risk_events ORDER BY ts DESC LIMIT 100", conn
        )
        conn.close()

        if not events.empty:
            render_risk_events_table(events)
        else:
            st.success("No risk events recorded. All clear.")
    except Exception as e:
        st.error(f"Error loading risk events: {e}")

# ── Tab: Config ─────────────────────────────────────────────────────────
with tab_config:
    st.subheader("Current Configuration")
    st.json(config)

# ---------------------------------------------------------------------------
# Auto-refresh (every 30 seconds)
# ---------------------------------------------------------------------------
import time

time.sleep(30)
st.rerun()
