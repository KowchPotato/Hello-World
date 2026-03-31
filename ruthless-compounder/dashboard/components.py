"""
Reusable Streamlit dashboard components.
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


def render_pnl_chart(df: pd.DataFrame) -> None:
    """Render a TVL history line chart."""
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=df["ts"], y=df["total_tvl"],
        name="Total TVL", line=dict(width=3, color="#00d4aa"),
    ))
    fig.add_trace(go.Scatter(
        x=df["ts"], y=df["solana_tvl"],
        name="Solana", line=dict(width=2, color="#9945FF"),
    ))
    fig.add_trace(go.Scatter(
        x=df["ts"], y=df["evm_tvl"],
        name="EVM", line=dict(width=2, color="#627EEA"),
    ))
    fig.add_trace(go.Scatter(
        x=df["ts"], y=df["sleeve_tvl"],
        name="Sleeve", line=dict(width=1, dash="dot", color="#FF6B6B"),
    ))

    fig.update_layout(
        template="plotly_dark",
        xaxis_title="Time",
        yaxis_title="USD",
        height=400,
        margin=dict(l=20, r=20, t=30, b=20),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_tx_budget_gauge(used: int, cap: int) -> None:
    """Render a gauge chart for transaction budget utilization."""
    pct = (used / cap * 100) if cap > 0 else 0

    col1, col2, col3 = st.columns(3)
    col1.metric("TX Used (YTD)", f"{used:,}")
    col2.metric("TX Cap", f"{cap:,}")
    col3.metric("Remaining", f"{cap - used:,}")

    # Color based on utilization
    if pct < 50:
        color = "#00d4aa"
    elif pct < 80:
        color = "#FFD700"
    else:
        color = "#FF4444"

    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=used,
        delta={"reference": cap, "relative": False},
        title={"text": "Annual TX Budget"},
        gauge={
            "axis": {"range": [0, cap]},
            "bar": {"color": color},
            "steps": [
                {"range": [0, cap * 0.5], "color": "#1a3a2a"},
                {"range": [cap * 0.5, cap * 0.8], "color": "#3a3a1a"},
                {"range": [cap * 0.8, cap], "color": "#3a1a1a"},
            ],
            "threshold": {
                "line": {"color": "red", "width": 4},
                "thickness": 0.75,
                "value": cap * 0.8,
            },
        },
    ))
    fig.update_layout(height=250, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)


def render_allocation_pie(solana: float, evm: float, sleeve: float) -> None:
    """Render a pie chart of current allocation."""
    values = [solana, evm, sleeve]
    labels = ["Solana (Kamino+Meteora)", "EVM (Curve+Convex)", "Trading Sleeve"]
    colors = ["#9945FF", "#627EEA", "#FF6B6B"]

    fig = px.pie(
        values=values,
        names=labels,
        color_discrete_sequence=colors,
        hole=0.4,
    )
    fig.update_layout(
        template="plotly_dark",
        height=300,
        margin=dict(l=20, r=20, t=30, b=20),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_risk_events_table(df: pd.DataFrame) -> None:
    """Render risk events with color-coded severity."""
    def severity_color(severity: str) -> str:
        colors = {
            "critical": "background-color: #ff4444; color: white;",
            "warning": "background-color: #ffaa00; color: black;",
            "info": "background-color: #4488ff; color: white;",
        }
        return colors.get(severity, "")

    st.dataframe(
        df.style.map(severity_color, subset=["severity"]),
        use_container_width=True,
        hide_index=True,
    )
