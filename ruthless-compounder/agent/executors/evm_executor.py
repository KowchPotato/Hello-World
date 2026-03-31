"""
EVM Executor — Builds and sends transactions on EVM L2 chains.

Handles Curve pool deposits/withdrawals, Convex staking/unstaking,
and basic ERC-20 operations. Uses web3.py and eth_defi.
"""

import asyncio
from typing import Any

import structlog
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from agent.utils import get_evm_private_key

log = structlog.get_logger("ruthless.executors.evm")


class EvmExecutor:
    """Execute transactions on EVM L2 chains (Base, Arbitrum)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.evm_cfg = config.get("evm", {})
        self.chains_cfg = self.evm_cfg.get("chains", {})
        self.slippage_bps = self.evm_cfg.get("slippage_bps", 50)
        self.gas_limit_gwei = self.evm_cfg.get("gas_limit_gwei", 0.1)
        self._private_key: str | None = None
        self._web3_clients: dict[str, Web3] = {}

    @property
    def private_key(self) -> str:
        if self._private_key is None:
            self._private_key = get_evm_private_key()
        return self._private_key

    def _get_web3(self, chain: str) -> Web3:
        """Get or create a Web3 client for a chain."""
        if chain not in self._web3_clients:
            chain_cfg = self.chains_cfg.get(chain, {})
            rpc = chain_cfg.get("rpc", "")
            if not rpc:
                raise ValueError(f"No RPC configured for chain: {chain}")
            w3 = Web3(Web3.HTTPProvider(rpc))
            # Add PoA middleware for L2 chains
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            self._web3_clients[chain] = w3
        return self._web3_clients[chain]

    @property
    def wallet_address(self) -> str:
        """Derive wallet address from private key."""
        from eth_account import Account
        return Account.from_key(self.private_key).address

    async def execute(self, action: dict) -> str | None:
        """Dispatch an action to the appropriate handler."""
        protocol = action.get("protocol", "").lower()
        action_type = action.get("type", "")
        chain = action.get("chain", "arbitrum")

        log.info("evm_execute", chain=chain, protocol=protocol, type=action_type)

        if protocol == "curve":
            return await self._execute_curve(action, chain)
        elif protocol == "convex":
            return await self._execute_convex(action, chain)
        elif action_type == "harvest":
            return await self._harvest_rewards(action, chain)
        elif action_type == "emergency_exit":
            return await self._emergency_exit(action, chain)
        else:
            log.warning("evm_unknown_protocol", protocol=protocol)
            return None

    # ------------------------------------------------------------------
    # Curve Finance
    # ------------------------------------------------------------------
    async def _execute_curve(self, action: dict, chain: str) -> str | None:
        """
        Execute a Curve pool operation (deposit/withdraw/swap).

        Uses eth_defi for Curve registry interaction.
        """
        action_type = action.get("type", "")
        params = action.get("params", {})
        pool_name = action.get("pool", "")
        amount_usd = action.get("amount_usd", 0)

        log.info("curve_action", type=action_type, pool=pool_name, amount=amount_usd, chain=chain)

        # TODO: Implement via eth_defi Curve integration
        # from eth_defi.curve import (
        #     get_curve_pool,
        #     deposit_to_curve,
        #     withdraw_from_curve,
        # )
        #
        # w3 = self._get_web3(chain)
        # pool = get_curve_pool(w3, pool_address)
        #
        # if action_type == "deposit":
        #     tx = deposit_to_curve(pool, amounts, self.wallet_address)
        # elif action_type == "withdraw":
        #     tx = withdraw_from_curve(pool, lp_amount, self.wallet_address)
        #
        # signed = w3.eth.account.sign_transaction(tx, self.private_key)
        # tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        log.info("curve_action_placeholder", msg="Would execute Curve transaction")
        return None

    # ------------------------------------------------------------------
    # Convex Finance
    # ------------------------------------------------------------------
    async def _execute_convex(self, action: dict, chain: str) -> str | None:
        """
        Execute a Convex staking operation.

        Convex wraps Curve LP tokens for boosted CRV + CVX rewards.
        """
        action_type = action.get("type", "")
        params = action.get("params", {})

        log.info("convex_action", type=action_type, params=params, chain=chain)

        # TODO: Implement via Convex contracts
        # Steps:
        # 1. Approve Curve LP tokens for Convex Booster contract
        # 2. Call Booster.deposit(pid, amount, true) to stake
        # 3. For withdrawal: BaseRewardPool.withdrawAndUnwrap(amount, true)
        # 4. Claim rewards: BaseRewardPool.getReward()

        log.info("convex_action_placeholder", msg="Would execute Convex transaction")
        return None

    # ------------------------------------------------------------------
    # Harvest Rewards
    # ------------------------------------------------------------------
    async def _harvest_rewards(self, action: dict, chain: str) -> str | None:
        """Harvest pending CRV/CVX/other rewards."""
        protocol = action.get("protocol", "")
        log.info("evm_harvest", protocol=protocol, chain=chain)
        # TODO: Call reward claim functions
        return None

    # ------------------------------------------------------------------
    # Emergency Exit
    # ------------------------------------------------------------------
    async def _emergency_exit(self, action: dict, chain: str) -> str | None:
        """Emergency: unstake, withdraw LP, swap to stables."""
        log.critical("evm_emergency_exit", chain=chain, action=action)
        # TODO: Batch unstake + withdraw + swap
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _send_transaction(self, chain: str, tx_data: dict) -> str | None:
        """Sign and send a transaction, with gas price guard."""
        w3 = self._get_web3(chain)

        try:
            # Gas price check
            gas_price = w3.eth.gas_price
            gas_gwei = gas_price / 1e9
            if gas_gwei > self.gas_limit_gwei * 1000:  # L2 gas is in different units
                log.warning("evm_gas_too_high", gas_gwei=gas_gwei, limit=self.gas_limit_gwei)
                return None

            # Build transaction
            tx_data["nonce"] = w3.eth.get_transaction_count(self.wallet_address)
            tx_data["gasPrice"] = gas_price
            if "gas" not in tx_data:
                tx_data["gas"] = w3.eth.estimate_gas(tx_data)

            # Sign and send
            signed = w3.eth.account.sign_transaction(tx_data, self.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            hex_hash = tx_hash.hex()

            log.info("evm_tx_sent", chain=chain, hash=hex_hash[:16] + "...")

            # Wait for receipt
            receipt = await asyncio.to_thread(
                w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120
            )

            if receipt["status"] == 1:
                log.info("evm_tx_confirmed", chain=chain, hash=hex_hash[:16] + "...")
                return hex_hash
            else:
                log.error("evm_tx_reverted", chain=chain, hash=hex_hash)
                return None

        except Exception as e:
            log.error("evm_tx_failed", chain=chain, error=str(e))
            return None

    async def get_positions(self) -> list[dict]:
        """Fetch EVM DeFi positions across all configured chains."""
        positions = []

        for chain_name, chain_cfg in self.chains_cfg.items():
            try:
                w3 = self._get_web3(chain_name)
                # Native balance
                balance_wei = await asyncio.to_thread(
                    w3.eth.get_balance, self.wallet_address
                )
                native_amount = balance_wei / 1e18
                # Rough price placeholders
                native_prices = {"base": 3000.0, "arbitrum": 3000.0}
                price = native_prices.get(chain_name, 1.0)

                positions.append({
                    "chain": chain_name,
                    "protocol": "wallet",
                    "asset": "ETH",
                    "amount": native_amount,
                    "value_usd": native_amount * price,
                })

            except Exception as e:
                log.error("evm_positions_failed", chain=chain_name, error=str(e))

        # TODO: Query Curve LP positions, Convex staked positions
        return positions
