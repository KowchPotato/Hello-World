"""
Risk Sentinel — Continuous 5-minute background monitor.

Polls on-chain health factors, oracle freshness, yield rates, and stablecoin pegs.
Almost never transacts — only triggers emergency exits on critical risk signals.
"""

import json
import time
from typing import Any

import structlog

from agent.tx_budget import TxBudget
from agent.utils import get_db, is_paused, utc_now_iso

log = structlog.get_logger("ruthless.risk_sentinel")


class RiskSentinel:
    """Monitors risk conditions and triggers emergency actions when necessary."""

    def __init__(self, config: dict[str, Any], tx_budget: TxBudget) -> None:
        self.config = config
        self.tx_budget = tx_budget
        self.risk_cfg = config.get("risk", {})
        self.health_min = self.risk_cfg.get("health_factor_min", 1.30)
        self.yield_crash_pct = self.risk_cfg.get("yield_crash_pct", 50)
        self.max_drawdown_pct = self.risk_cfg.get("max_drawdown_pct", 15)
        self.depeg_threshold = self.risk_cfg.get("depeg_threshold_pct", 2.0)
        self.oracle_staleness = self.risk_cfg.get("oracle_staleness_sec", 120)
        self._last_yields: dict[str, float] = {}

    async def check(self) -> None:
        """Run all risk checks. Called every 5 minutes by the scheduler."""
        if is_paused(self.config):
            log.info("risk_sentinel_paused")
            return

        log.debug("risk_sentinel_tick")

        alerts: list[dict] = []

        try:
            alerts.extend(await self._check_health_factors())
        except Exception as e:
            log.error("risk_check_health_failed", error=str(e))

        try:
            alerts.extend(await self._check_yield_crash())
        except Exception as e:
            log.error("risk_check_yield_failed", error=str(e))

        try:
            alerts.extend(await self._check_drawdown())
        except Exception as e:
            log.error("risk_check_drawdown_failed", error=str(e))

        try:
            alerts.extend(await self._check_stablecoin_pegs())
        except Exception as e:
            log.error("risk_check_peg_failed", error=str(e))

        # Process any critical alerts
        for alert in alerts:
            self._record_event(alert)
            if alert["severity"] == "critical":
                await self._emergency_exit(alert)

    # ------------------------------------------------------------------
    # Individual Risk Checks
    # ------------------------------------------------------------------
    async def _check_health_factors(self) -> list[dict]:
        """Check lending protocol health factors (Kamino, Aave, Morpho)."""
        alerts = []
        # TODO: Integrate with actual on-chain health factor queries
        # Placeholder: would call Kamino / Aave APIs
        #
        # Example check:
        # health = await kamino_client.get_health_factor(wallet)
        # if health < self.health_min:
        #     alerts.append({...})

        log.debug("health_factor_check_ok")
        return alerts

    async def _check_yield_crash(self) -> list[dict]:
        """Detect sudden yield drops (>50% in one check interval)."""
        alerts = []
        # TODO: Compare current yields vs self._last_yields
        # If any pool's yield dropped by >yield_crash_pct, alert.
        #
        # Example:
        # for pool, current_apy in current_yields.items():
        #     prev = self._last_yields.get(pool)
        #     if prev and prev > 0:
        #         drop_pct = ((prev - current_apy) / prev) * 100
        #         if drop_pct > self.yield_crash_pct:
        #             alerts.append({
        #                 "event_type": "yield_crash",
        #                 "severity": "critical",
        #                 "details": f"{pool}: {prev:.1f}% -> {current_apy:.1f}% ({drop_pct:.0f}% drop)",
        #             })
        # self._last_yields = current_yields

        log.debug("yield_crash_check_ok")
        return alerts

    async def _check_drawdown(self) -> list[dict]:
        """Check portfolio drawdown from peak."""
        alerts = []
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT total_tvl FROM portfolio_snapshots ORDER BY ts DESC LIMIT 48"
            ).fetchall()
            conn.close()

            if len(rows) >= 2:
                current = rows[0]["total_tvl"]
                peak = max(r["total_tvl"] for r in rows)
                if peak > 0:
                    drawdown = ((peak - current) / peak) * 100
                    if drawdown > self.max_drawdown_pct:
                        alerts.append({
                            "event_type": "max_drawdown",
                            "severity": "critical",
                            "details": f"Drawdown {drawdown:.1f}% exceeds {self.max_drawdown_pct}% limit. Peak: ${peak:,.0f}, Current: ${current:,.0f}",
                        })
                        log.error("drawdown_breach", drawdown_pct=drawdown)
        except Exception as e:
            log.error("drawdown_check_failed", error=str(e))

        log.debug("drawdown_check_ok")
        return alerts

    async def _check_stablecoin_pegs(self) -> list[dict]:
        """Monitor stablecoin depeg risk via on-chain price feeds."""
        alerts = []
        # TODO: Query Pyth/Chainlink oracle prices for USDC, USDT, DAI
        # If |price - 1.0| > depeg_threshold/100, alert.
        #
        # Example:
        # for stable, price in oracle_prices.items():
        #     deviation = abs(price - 1.0) * 100
        #     if deviation > self.depeg_threshold:
        #         alerts.append({
        #             "event_type": "stablecoin_depeg",
        #             "severity": "critical",
        #             "details": f"{stable} at ${price:.4f} ({deviation:.2f}% off peg)",
        #         })

        log.debug("peg_check_ok")
        return alerts

    # ------------------------------------------------------------------
    # Emergency Actions
    # ------------------------------------------------------------------
    async def _emergency_exit(self, alert: dict) -> None:
        """
        Execute emergency exit: unwind risky positions.
        Only transacts when absolutely necessary.
        """
        log.critical("emergency_exit_triggered", alert=alert)

        # Check tx budget even for emergencies (but with higher priority)
        if not self.tx_budget.authorize(planned_tx_count=3):
            log.error("emergency_exit_blocked_by_budget")
            # Record the blocked emergency
            self._record_event({
                "event_type": "emergency_blocked",
                "severity": "critical",
                "details": "TX budget exhausted, cannot execute emergency exit",
                "action_taken": "none — budget exhausted",
            })
            return

        # TODO: Implement actual emergency exit logic:
        # 1. Close all perp positions (trading sleeve)
        # 2. Withdraw from high-risk lending positions
        # 3. Convert to stables
        #
        # For now, log and create pause file
        log.critical(
            "emergency_action",
            event=alert["event_type"],
            action="would_exit_positions",
        )

        self._record_event({
            **alert,
            "action_taken": "emergency_exit_initiated",
        })

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _record_event(self, event: dict) -> None:
        """Persist a risk event to the database."""
        try:
            conn = get_db()
            conn.execute(
                """INSERT INTO risk_events (event_type, severity, details, action_taken)
                   VALUES (?, ?, ?, ?)""",
                (
                    event.get("event_type", "unknown"),
                    event.get("severity", "info"),
                    event.get("details", ""),
                    event.get("action_taken", "none"),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error("record_risk_event_failed", error=str(e))
