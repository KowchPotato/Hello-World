"""
Core Loop — Daily AI-driven compound + rebalance cycle.

Executed once per day at the configured UTC hour. Gathers yields from all
chains, asks the LLM for a rebalance plan, validates it, and dispatches
batched transactions through the appropriate executors.
"""

import asyncio
import json
from typing import Any

import structlog

from agent.decision_engine import make_decision
from agent.executors.evm_executor import EvmExecutor
from agent.executors.solana_executor import SolanaExecutor
from agent.scanners.curve_scanner import CurveScanner
from agent.scanners.evm_scanner import EvmScanner
from agent.scanners.solana_scanner import SolanaScanner
from agent.trading_sleeve import TradingSleeve
from agent.tx_budget import TxBudget
from agent.utils import get_db, is_paused, utc_now_iso

log = structlog.get_logger("ruthless.core_loop")


async def run_daily_compound(config: dict[str, Any], tx_budget: TxBudget) -> None:
    """
    Full daily compound cycle:
    1. Check pause state
    2. Scan yields across all chains
    3. Get current portfolio snapshot
    4. Ask LLM for rebalance plan
    5. Validate tx budget
    6. Execute batched transactions
    7. Record results
    """
    log.info("daily_compound_started")

    # ── 1. Pause check ──────────────────────────────────────────────
    if is_paused(config):
        log.warning("daily_compound_skipped_paused")
        return

    # ── 2. Scan yields ──────────────────────────────────────────────
    yields = await _scan_all_yields(config)
    log.info("yields_scanned", protocols=list(yields.keys()))

    # ── 3. Current portfolio state ──────────────────────────────────
    portfolio = await _get_portfolio_snapshot(config)
    log.info("portfolio_snapshot", total_tvl=portfolio.get("total_tvl", 0))

    # ── 4. Risk metrics for the LLM context ─────────────────────────
    risk_metrics = _gather_risk_metrics(config, portfolio)

    # ── 5. Ask the LLM ─────────────────────────────────────────────
    budget_status = tx_budget.status()
    decision = await make_decision(
        config=config,
        portfolio=portfolio,
        yields=yields,
        risk_metrics=risk_metrics,
        tx_budget_status=budget_status,
    )
    log.info(
        "llm_decision",
        actions=len(decision.get("actions", [])),
        confidence=decision.get("confidence", 0),
        reasoning=decision.get("reasoning", "")[:200],
    )

    actions = decision.get("actions", [])
    if not actions:
        log.info("no_actions_decided")
        _record_snapshot(portfolio)
        return

    # ── 6. TX budget gate ───────────────────────────────────────────
    est_tx = decision.get("estimated_tx_count", len(actions))
    if not tx_budget.authorize(planned_tx_count=est_tx):
        log.error("daily_compound_blocked_by_tx_budget", est_tx=est_tx)
        _record_snapshot(portfolio)
        return

    # ── 7. Execute actions ──────────────────────────────────────────
    await _execute_actions(config, actions, tx_budget)

    # ── 8. Record post-execution snapshot ───────────────────────────
    post_portfolio = await _get_portfolio_snapshot(config)
    _record_snapshot(post_portfolio)

    log.info("daily_compound_completed")


# ---------------------------------------------------------------------------
# Yield Scanning
# ---------------------------------------------------------------------------
async def _scan_all_yields(config: dict[str, Any]) -> dict:
    """Scan yields from all configured protocols in parallel."""
    results = {}

    tasks = []

    # Solana yields
    if config.get("solana", {}).get("kamino", {}).get("enabled"):
        sol_scanner = SolanaScanner(config)
        tasks.append(("solana_kamino", sol_scanner.scan_kamino()))
    if config.get("solana", {}).get("meteora", {}).get("enabled"):
        sol_scanner = SolanaScanner(config)
        tasks.append(("solana_meteora", sol_scanner.scan_meteora()))

    # EVM yields
    for chain_name, chain_cfg in config.get("evm", {}).get("chains", {}).items():
        if chain_cfg.get("curve_pools"):
            curve_scanner = CurveScanner(config, chain_name)
            tasks.append((f"curve_{chain_name}", curve_scanner.scan()))
        if chain_cfg.get("convex_enabled"):
            evm_scanner = EvmScanner(config, chain_name)
            tasks.append((f"convex_{chain_name}", evm_scanner.scan_convex()))

    # Run all scans concurrently
    for name, coro in tasks:
        try:
            results[name] = await coro
        except Exception as e:
            log.error("yield_scan_failed", scanner=name, error=str(e))
            results[name] = {"error": str(e)}

    return results


# ---------------------------------------------------------------------------
# Portfolio Snapshot
# ---------------------------------------------------------------------------
async def _get_portfolio_snapshot(config: dict[str, Any]) -> dict:
    """Gather current balances and positions across all chains."""
    snapshot = {
        "total_tvl": 0.0,
        "solana_tvl": 0.0,
        "evm_tvl": 0.0,
        "sleeve_tvl": 0.0,
        "positions": [],
    }

    try:
        sol_executor = SolanaExecutor(config)
        sol_positions = await sol_executor.get_positions()
        sol_tvl = sum(p.get("value_usd", 0) for p in sol_positions)
        snapshot["solana_tvl"] = sol_tvl
        snapshot["positions"].extend(sol_positions)
    except Exception as e:
        log.error("solana_snapshot_failed", error=str(e))

    try:
        evm_executor = EvmExecutor(config)
        evm_positions = await evm_executor.get_positions()
        evm_tvl = sum(p.get("value_usd", 0) for p in evm_positions)
        snapshot["evm_tvl"] = evm_tvl
        snapshot["positions"].extend(evm_positions)
    except Exception as e:
        log.error("evm_snapshot_failed", error=str(e))

    snapshot["total_tvl"] = snapshot["solana_tvl"] + snapshot["evm_tvl"] + snapshot["sleeve_tvl"]
    return snapshot


# ---------------------------------------------------------------------------
# Risk Metrics Gathering
# ---------------------------------------------------------------------------
def _gather_risk_metrics(config: dict[str, Any], portfolio: dict) -> dict:
    """Compile risk metrics for the decision engine."""
    metrics = {
        "portfolio_tvl": portfolio.get("total_tvl", 0),
        "timestamp": utc_now_iso(),
    }

    # Pull recent risk events from DB
    try:
        conn = get_db()
        events = conn.execute(
            "SELECT * FROM risk_events WHERE ts >= datetime('now', '-24 hours') ORDER BY ts DESC LIMIT 10"
        ).fetchall()
        conn.close()
        metrics["recent_risk_events"] = [dict(e) for e in events]
    except Exception:
        metrics["recent_risk_events"] = []

    # Calculate 24h P&L from snapshots
    try:
        conn = get_db()
        snaps = conn.execute(
            "SELECT total_tvl FROM portfolio_snapshots ORDER BY ts DESC LIMIT 2"
        ).fetchall()
        conn.close()
        if len(snaps) == 2:
            metrics["pnl_24h"] = snaps[0]["total_tvl"] - snaps[1]["total_tvl"]
            metrics["pnl_24h_pct"] = (
                (metrics["pnl_24h"] / snaps[1]["total_tvl"] * 100)
                if snaps[1]["total_tvl"] > 0 else 0
            )
    except Exception:
        pass

    return metrics


# ---------------------------------------------------------------------------
# Action Execution
# ---------------------------------------------------------------------------
async def _execute_actions(
    config: dict[str, Any],
    actions: list[dict],
    tx_budget: TxBudget,
) -> None:
    """Dispatch each action to the appropriate executor."""
    sol_executor = SolanaExecutor(config)
    evm_executor = EvmExecutor(config)
    sleeve = TradingSleeve(config, tx_budget)

    for action in actions:
        chain = action.get("chain", "")
        action_type = action.get("type", "")

        log.info("executing_action", chain=chain, type=action_type, protocol=action.get("protocol"))

        try:
            if action_type == "no_action":
                continue

            if action_type in ("open_perp", "close_perp"):
                await sleeve.execute(action)
            elif chain == "solana":
                await sol_executor.execute(action)
            elif chain in ("base", "arbitrum"):
                await evm_executor.execute(action)
            else:
                log.warning("unknown_chain_in_action", chain=chain, action=action)

        except Exception as e:
            log.error("action_execution_failed", action=action, error=str(e))


# ---------------------------------------------------------------------------
# Snapshot Recording
# ---------------------------------------------------------------------------
def _record_snapshot(portfolio: dict) -> None:
    """Persist a portfolio snapshot to the database."""
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO portfolio_snapshots (total_tvl, solana_tvl, evm_tvl, sleeve_tvl, pnl_24h, data_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                portfolio.get("total_tvl", 0),
                portfolio.get("solana_tvl", 0),
                portfolio.get("evm_tvl", 0),
                portfolio.get("sleeve_tvl", 0),
                portfolio.get("pnl_24h", 0),
                json.dumps(portfolio),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("snapshot_record_failed", error=str(e))
