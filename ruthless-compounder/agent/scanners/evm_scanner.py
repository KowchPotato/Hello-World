"""
EVM Scanner — Additional EVM protocol scanning (Convex staking, Aave, Morpho).

Extends beyond Curve to capture the full EVM yield landscape.
"""

import asyncio
from typing import Any

import aiohttp
import structlog

log = structlog.get_logger("ruthless.scanners.evm")


class EvmScanner:
    """Scan EVM-based protocols beyond Curve (Convex staking, lending, etc.)."""

    def __init__(self, config: dict[str, Any], chain: str) -> None:
        self.config = config
        self.chain = chain
        self.chain_cfg = config.get("evm", {}).get("chains", {}).get(chain, {})

    async def scan_convex(self) -> dict:
        """
        Scan Convex Finance staking pools for boosted yields.

        Returns dict of pool_name -> {apy, tvl, staked, ...}
        """
        results = {}

        async with aiohttp.ClientSession() as session:
            try:
                # Convex pools API
                async with session.get(
                    "https://www.convexfinance.com/api/curve-pools",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        pools = await resp.json()
                        for pool in pools if isinstance(pools, list) else []:
                            results[pool.get("name", "unknown")] = {
                                "protocol": "convex",
                                "chain": self.chain,
                                "apy": pool.get("apy", 0),
                                "tvl": pool.get("tvl", 0),
                                "crv_apy": pool.get("crvApy", 0),
                                "cvx_apy": pool.get("cvxApy", 0),
                                "pool_id": pool.get("id", ""),
                            }
                    else:
                        log.warning("convex_pools_api_error", status=resp.status)

            except asyncio.TimeoutError:
                log.warning("convex_scan_timeout", chain=self.chain)
            except Exception as e:
                log.error("convex_scan_failed", chain=self.chain, error=str(e))

        log.info("convex_scan_complete", chain=self.chain, pools=len(results))
        return results

    async def scan_lending(self) -> dict:
        """
        Scan lending protocols (Aave, Morpho) for supply/borrow rates.

        Placeholder for future expansion.
        """
        results = {}

        # TODO: Use eth_defi library for Aave v3 rate queries
        # from eth_defi.aave_v3 import fetch_reserve_data
        # ...

        log.info("lending_scan_complete", chain=self.chain, protocols=len(results))
        return results

    async def get_token_balances(self, wallet_address: str) -> dict:
        """Get ERC-20 token balances for a wallet on this chain."""
        # TODO: Use web3.py to query balances
        # from web3 import Web3
        # w3 = Web3(Web3.HTTPProvider(self.chain_cfg["rpc"]))
        # balance = w3.eth.get_balance(wallet_address)
        return {}
