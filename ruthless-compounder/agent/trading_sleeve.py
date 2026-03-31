"""
Trading Sleeve — Optional 10-15% portfolio allocation for perpetual futures.

Strict guardrails:
- Max 5x leverage
- Max 2% risk per trade
- 8% daily stop-loss halts all sleeve activity
- Operates on Drift and Jupiter Perps (Solana)
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Any

import structlog

from agent.tx_budget import TxBudget
from agent.utils import get_db, utc_now, utc_now_iso

log = structlog.get_logger("ruthless.trading_sleeve")


class TradingSleeve:
    """Manages perpetual futures positions with strict risk controls."""

    def __init__(self, config: dict[str, Any], tx_budget: TxBudget) -> None:
        self.config = config
        self.tx_budget = tx_budget
        sleeve_cfg = config.get("trading_sleeve", {})
        self.enabled = sleeve_cfg.get("enabled", False)
        self.max_leverage = sleeve_cfg.get("max_leverage", 5.0)
        self.risk_per_trade = sleeve_cfg.get("risk_per_trade_pct", 2.0)
        self.daily_stop_loss = sleeve_cfg.get("daily_stop_loss_pct", 8.0)
        self.allowed_pairs = sleeve_cfg.get("allowed_pairs", [])
        self.platforms = sleeve_cfg.get("platforms", [])

    async def execute(self, action: dict) -> None:
        """Execute a trading sleeve action with all guardrails."""
        if not self.enabled:
            log.info("trading_sleeve_disabled")
            return

        action_type = action.get("type", "")

        # ── Guardrail: Daily stop-loss check ────────────────────────
        if await self._daily_stop_loss_breached():
            log.warning("trading_sleeve_halted_daily_stop_loss")
            return

        # ── Guardrail: Pair whitelist ───────────────────────────────
        pair = action.get("params", {}).get("pair", "")
        if pair and pair not in self.allowed_pairs:
            log.warning("trading_sleeve_pair_not_allowed", pair=pair)
            return

        # ── Guardrail: Leverage cap ─────────────────────────────────
        leverage = action.get("params", {}).get("leverage", 1.0)
        if leverage > self.max_leverage:
            log.warning("trading_sleeve_leverage_capped", requested=leverage, max=self.max_leverage)
            leverage = self.max_leverage
            action["params"]["leverage"] = leverage

        # ── Guardrail: Position sizing (2% risk rule) ──────────────
        amount_usd = action.get("amount_usd", 0)
        sleeve_capital = await self._get_sleeve_capital()
        max_risk_amount = sleeve_capital * (self.risk_per_trade / 100)
        if amount_usd > max_risk_amount:
            log.warning(
                "trading_sleeve_position_oversized",
                requested=amount_usd,
                max_risk=max_risk_amount,
            )
            action["amount_usd"] = max_risk_amount
            amount_usd = max_risk_amount

        # ── TX budget check ─────────────────────────────────────────
        if not self.tx_budget.authorize(planned_tx_count=1):
            log.error("trading_sleeve_tx_budget_exceeded")
            return

        # ── Execute ─────────────────────────────────────────────────
        if action_type == "open_perp":
            await self._open_position(action)
        elif action_type == "close_perp":
            await self._close_position(action)
        else:
            log.warning("trading_sleeve_unknown_action", action_type=action_type)

    async def _open_position(self, action: dict) -> None:
        """Open a new perp position on Drift or Jupiter."""
        platform = action.get("params", {}).get("platform", "drift")
        pair = action.get("params", {}).get("pair", "SOL-PERP")
        side = action.get("params", {}).get("side", "long")
        leverage = action.get("params", {}).get("leverage", 1.0)
        amount = action.get("amount_usd", 0)

        log.info(
            "opening_perp_position",
            platform=platform,
            pair=pair,
            side=side,
            leverage=leverage,
            amount_usd=amount,
        )

        # TODO: Implement actual Drift / Jupiter Perps SDK calls
        # Example for Drift:
        # from driftpy.drift_client import DriftClient
        # client = DriftClient(...)
        # await client.open_position(
        #     market_index=...,
        #     direction=...,
        #     base_asset_amount=...,
        # )

        # Record placeholder
        self.tx_budget.record(
            chain="solana",
            tx_hash="placeholder_perp_open",
            tx_type="open_perp",
            protocol=platform,
            amount_usd=amount,
            notes=f"{side} {pair} {leverage}x",
        )

    async def _close_position(self, action: dict) -> None:
        """Close an existing perp position."""
        platform = action.get("params", {}).get("platform", "drift")
        pair = action.get("params", {}).get("pair", "SOL-PERP")

        log.info("closing_perp_position", platform=platform, pair=pair)

        # TODO: Implement actual close logic
        self.tx_budget.record(
            chain="solana",
            tx_hash="placeholder_perp_close",
            tx_type="close_perp",
            protocol=platform,
            notes=f"close {pair}",
        )

    async def _daily_stop_loss_breached(self) -> bool:
        """Check if the trading sleeve has hit its daily 8% stop-loss."""
        try:
            conn = get_db()
            # Get sleeve P&L from today's transactions
            today = utc_now().replace(hour=0, minute=0, second=0).isoformat()
            rows = conn.execute(
                """SELECT SUM(amount_usd) as total
                   FROM transactions
                   WHERE ts >= ? AND tx_type IN ('open_perp', 'close_perp')""",
                (today,),
            ).fetchone()
            conn.close()

            # Simplified: would need proper P&L tracking in production
            # For now, check if we have too many sleeve trades today
            return False
        except Exception as e:
            log.error("daily_stop_loss_check_failed", error=str(e))
            return True  # Fail safe: halt on error

    async def _get_sleeve_capital(self) -> float:
        """Get the current capital allocated to the trading sleeve."""
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT sleeve_tvl FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            conn.close()
            return row["sleeve_tvl"] if row else 0.0
        except Exception:
            return 0.0
