"""
Solana Executor — Builds, signs, and sends Solana transactions.

Handles Kamino Multiply deposits/withdrawals, Meteora DLMM LP operations,
and Jupiter swaps. Uses batched transactions and priority fees.
"""

import asyncio
import base64
from typing import Any

import structlog
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_price  # type: ignore
from solders.keypair import Keypair  # type: ignore
from solders.message import Message  # type: ignore
from solders.transaction import Transaction  # type: ignore

from agent.utils import get_solana_keypair

log = structlog.get_logger("ruthless.executors.solana")


class SolanaExecutor:
    """Execute transactions on Solana (Kamino, Meteora, Jupiter)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        sol_cfg = config.get("solana", {})
        rpcs = sol_cfg.get("rpc_endpoints", ["https://api.devnet.solana.com"])
        self.rpc_url = rpcs[0]
        self.priority_fee = sol_cfg.get("priority_fee_microlamports", 50_000)
        self._keypair: Keypair | None = None

    @property
    def keypair(self) -> Keypair:
        """Lazy-load the keypair from encrypted env var."""
        if self._keypair is None:
            self._keypair = get_solana_keypair()
        return self._keypair

    async def execute(self, action: dict) -> str | None:
        """
        Dispatch a single action to the appropriate handler.

        Returns the transaction signature or None on failure.
        """
        protocol = action.get("protocol", "").lower()
        action_type = action.get("type", "")

        log.info("solana_execute", protocol=protocol, type=action_type)

        if protocol == "kamino":
            return await self._execute_kamino(action)
        elif protocol == "meteora":
            return await self._execute_meteora(action)
        elif protocol == "jupiter":
            return await self._execute_jupiter_swap(action)
        elif action_type == "harvest":
            return await self._harvest_rewards(action)
        elif action_type == "emergency_exit":
            return await self._emergency_exit(action)
        else:
            log.warning("solana_unknown_protocol", protocol=protocol)
            return None

    # ------------------------------------------------------------------
    # Kamino Multiply
    # ------------------------------------------------------------------
    async def _execute_kamino(self, action: dict) -> str | None:
        """
        Execute a Kamino Multiply strategy action.

        Kamino Multiply = leveraged yield via borrow-lend loops.
        """
        action_type = action.get("type", "")
        params = action.get("params", {})
        amount_usd = action.get("amount_usd", 0)

        log.info("kamino_action", type=action_type, amount=amount_usd, params=params)

        # TODO: Implement via Kamino SDK / CPI instructions
        # Steps for deposit into Kamino Multiply:
        # 1. Fetch the strategy's on-chain account
        # 2. Build deposit instruction via Kamino's program
        # 3. Add priority fee instruction
        # 4. Build + sign + send transaction
        #
        # Example (pseudocode):
        # from kamino_sdk import KaminoClient
        # client = KaminoClient(self.rpc_url)
        # strategy = await client.get_strategy(strategy_pubkey)
        # ix = client.build_deposit_ix(strategy, amount, self.keypair.pubkey())
        # tx = Transaction([priority_fee_ix, ix])
        # sig = await self._send_transaction(tx)

        log.info("kamino_action_placeholder", msg="Would execute Kamino transaction")
        return None

    # ------------------------------------------------------------------
    # Meteora DLMM
    # ------------------------------------------------------------------
    async def _execute_meteora(self, action: dict) -> str | None:
        """
        Execute a Meteora DLMM vault operation.

        DLMM = Dynamic Liquidity Market Maker with concentrated bins.
        """
        action_type = action.get("type", "")
        params = action.get("params", {})

        log.info("meteora_action", type=action_type, params=params)

        # TODO: Implement via Meteora SDK
        # Steps for adding liquidity to DLMM:
        # 1. Fetch pool info and current active bin
        # 2. Calculate optimal bin range based on strategy
        # 3. Build add_liquidity instruction
        # 4. Sign and send

        log.info("meteora_action_placeholder", msg="Would execute Meteora transaction")
        return None

    # ------------------------------------------------------------------
    # Jupiter Swap (for rebalancing)
    # ------------------------------------------------------------------
    async def _execute_jupiter_swap(self, action: dict) -> str | None:
        """Use Jupiter aggregator for token swaps during rebalancing."""
        params = action.get("params", {})
        input_mint = params.get("input_mint", "")
        output_mint = params.get("output_mint", "")
        amount = params.get("amount_lamports", 0)
        slippage_bps = params.get("slippage_bps", 50)

        log.info("jupiter_swap", input=input_mint[:8], output=output_mint[:8], amount=amount)

        # TODO: Use Jupiter V6 Swap API
        # 1. GET /quote?inputMint=...&outputMint=...&amount=...&slippageBps=...
        # 2. POST /swap with quote response
        # 3. Deserialize + sign + send transaction

        log.info("jupiter_swap_placeholder", msg="Would execute Jupiter swap")
        return None

    # ------------------------------------------------------------------
    # Harvest Rewards
    # ------------------------------------------------------------------
    async def _harvest_rewards(self, action: dict) -> str | None:
        """Harvest pending rewards from Solana protocols."""
        protocol = action.get("protocol", "")
        log.info("harvest_rewards", protocol=protocol)
        # TODO: Implement reward claiming
        return None

    # ------------------------------------------------------------------
    # Emergency Exit
    # ------------------------------------------------------------------
    async def _emergency_exit(self, action: dict) -> str | None:
        """Emergency: close all positions and convert to stables."""
        log.critical("solana_emergency_exit", action=action)
        # TODO: Close all Kamino/Meteora positions, swap to USDC
        return None

    # ------------------------------------------------------------------
    # Transaction Helpers
    # ------------------------------------------------------------------
    async def _send_transaction(self, tx: Transaction) -> str | None:
        """Sign and send a transaction with retry logic."""
        try:
            async with AsyncClient(self.rpc_url) as client:
                # Send with skip_preflight for speed, but confirm after
                result = await client.send_transaction(
                    tx,
                    self.keypair,
                    opts=TxOpts(
                        skip_preflight=False,
                        max_retries=3,
                    ),
                )
                sig = str(result.value)
                log.info("solana_tx_sent", signature=sig[:16] + "...")

                # Wait for confirmation
                await client.confirm_transaction(sig, commitment="confirmed")
                log.info("solana_tx_confirmed", signature=sig[:16] + "...")
                return sig

        except Exception as e:
            log.error("solana_tx_failed", error=str(e))
            return None

    async def get_positions(self) -> list[dict]:
        """
        Fetch current Solana DeFi positions for the hot wallet.

        Returns list of position dicts with value_usd estimates.
        """
        positions = []

        try:
            async with AsyncClient(self.rpc_url) as client:
                balance = await client.get_balance(self.keypair.pubkey())
                sol_lamports = balance.value
                # Rough SOL price estimate (in production, query oracle)
                sol_price = 150.0  # placeholder
                sol_usd = (sol_lamports / 1e9) * sol_price
                positions.append({
                    "chain": "solana",
                    "protocol": "wallet",
                    "asset": "SOL",
                    "amount": sol_lamports / 1e9,
                    "value_usd": sol_usd,
                })
        except Exception as e:
            log.error("solana_positions_failed", error=str(e))

        # TODO: Query Kamino and Meteora for active positions
        # TODO: Query SPL token balances

        return positions
