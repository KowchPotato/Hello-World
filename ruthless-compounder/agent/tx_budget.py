"""
Transaction Budget Enforcer

Tracks tx count per year and projects annual total.
Refuses to authorize transactions that would breach the hard cap.
"""

from datetime import datetime, timezone
from typing import Any

import structlog

from agent.utils import get_db, utc_now

log = structlog.get_logger("ruthless.tx_budget")


class TxBudget:
    """Enforce annual transaction budget across all chains."""

    def __init__(self, config: dict[str, Any]) -> None:
        budget_cfg = config.get("tx_budget", {})
        self.annual_cap: int = budget_cfg.get("max_annual_transactions", 295_000)
        self.warn_pct: int = budget_cfg.get("warn_threshold_pct", 80)
        self.year_start_month: int = budget_cfg.get("budget_year_start_month", 1)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def _fiscal_year_start(self) -> datetime:
        """Return the start of the current fiscal year."""
        now = utc_now()
        year = now.year if now.month >= self.year_start_month else now.year - 1
        return datetime(year, self.year_start_month, 1, tzinfo=timezone.utc)

    def used_this_year(self) -> int:
        """Count transactions logged since fiscal year start."""
        start = self._fiscal_year_start().isoformat()
        conn = get_db()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM transactions WHERE ts >= ?", (start,)
        ).fetchone()
        conn.close()
        return row["cnt"] if row else 0

    def remaining(self) -> int:
        """Transactions remaining before cap."""
        return max(0, self.annual_cap - self.used_this_year())

    def projected_annual(self, additional: int = 0) -> int:
        """
        Project total annual tx count from current usage rate.
        Includes `additional` planned transactions.
        """
        now = utc_now()
        fy_start = self._fiscal_year_start()
        days_elapsed = max((now - fy_start).days, 1)
        used = self.used_this_year() + additional
        projected = int(used * (365 / days_elapsed))
        return projected

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------
    def authorize(self, planned_tx_count: int = 1) -> bool:
        """
        Return True if `planned_tx_count` transactions can proceed
        without breaching the annual cap.
        """
        used = self.used_this_year()
        after = used + planned_tx_count

        # Hard cap check
        if after > self.annual_cap:
            log.error(
                "tx_budget_hard_cap_blocked",
                used=used,
                planned=planned_tx_count,
                cap=self.annual_cap,
            )
            return False

        # Projection check
        projected = self.projected_annual(additional=planned_tx_count)
        if projected > self.annual_cap:
            log.warning(
                "tx_budget_projection_blocked",
                projected=projected,
                cap=self.annual_cap,
                msg="Projected annual tx would exceed cap; refusing.",
            )
            return False

        # Warning check
        utilization = (after / self.annual_cap) * 100
        if utilization >= self.warn_pct:
            log.warning(
                "tx_budget_high_utilization",
                utilization_pct=round(utilization, 1),
                remaining=self.annual_cap - after,
            )

        log.info(
            "tx_budget_authorized",
            planned=planned_tx_count,
            used_after=after,
            remaining=self.annual_cap - after,
        )
        return True

    def record(self, chain: str, tx_hash: str, tx_type: str,
               protocol: str = "", amount_usd: float = 0.0,
               notes: str = "") -> None:
        """Record a completed transaction in the database."""
        conn = get_db()
        conn.execute(
            """INSERT INTO transactions (chain, tx_hash, tx_type, protocol, amount_usd, status, notes)
               VALUES (?, ?, ?, ?, ?, 'confirmed', ?)""",
            (chain, tx_hash, tx_type, protocol, amount_usd, notes),
        )
        conn.commit()
        conn.close()
        log.info("tx_recorded", chain=chain, tx_hash=tx_hash[:16] + "...", tx_type=tx_type)

    def status(self) -> dict:
        """Return a summary dict for the dashboard."""
        used = self.used_this_year()
        return {
            "used_this_year": used,
            "annual_cap": self.annual_cap,
            "remaining": self.annual_cap - used,
            "projected_annual": self.projected_annual(),
            "utilization_pct": round((used / self.annual_cap) * 100, 2) if self.annual_cap else 0,
        }
