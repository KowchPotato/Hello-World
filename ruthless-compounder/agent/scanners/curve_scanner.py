"""
Curve Scanner — Fetches Curve Finance pool yields + Convex boosted APYs.

Uses the web3-ethereum-defi (eth_defi) library and Curve's public API.
"""

import asyncio
from typing import Any

import aiohttp
import structlog

log = structlog.get_logger("ruthless.scanners.curve")

# Curve public API
CURVE_API_BASE = "https://api.curve.fi/api"


class CurveScanner:
    """Scan Curve pools and Convex-boosted vaults for yields."""

    def __init__(self, config: dict[str, Any], chain: str) -> None:
        self.config = config
        self.chain = chain
        self.chain_cfg = config.get("evm", {}).get("chains", {}).get(chain, {})
        self.pools_cfg = self.chain_cfg.get("curve_pools", [])
        self.convex_enabled = self.chain_cfg.get("convex_enabled", False)

    async def scan(self) -> dict:
        """
        Fetch current Curve pool APYs.

        Returns dict of pool_name -> {base_apy, crv_apy, convex_apy, tvl, ...}
        """
        results = {}

        # Map chain names to Curve API chain identifiers
        chain_map = {
            "base": "base",
            "arbitrum": "arbitrum",
            "ethereum": "ethereum",
        }
        api_chain = chain_map.get(self.chain, self.chain)

        async with aiohttp.ClientSession() as session:
            try:
                # Curve pools API
                async with session.get(
                    f"{CURVE_API_BASE}/getPools/{api_chain}/main",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pool_list = data.get("data", {}).get("poolData", [])

                        for pool_cfg in self.pools_cfg:
                            target_name = pool_cfg["name"]
                            min_apy = pool_cfg.get("min_apy", 0)

                            for pool in pool_list:
                                if target_name.lower() in pool.get("name", "").lower():
                                    base_apy = pool.get("apy", 0)
                                    # Curve APYs can be in different formats
                                    if isinstance(base_apy, list):
                                        base_apy = base_apy[0] if base_apy else 0

                                    results[target_name] = {
                                        "protocol": "curve",
                                        "chain": self.chain,
                                        "base_apy": base_apy,
                                        "tvl": pool.get("usdTotal", 0),
                                        "volume_24h": pool.get("volumeUSD", 0),
                                        "pool_address": pool.get("address", ""),
                                        "coins": [c.get("symbol", "") for c in pool.get("coins", [])],
                                        "min_apy_threshold": min_apy,
                                    }
                                    break
                    else:
                        log.warning("curve_api_error", chain=self.chain, status=resp.status)

            except asyncio.TimeoutError:
                log.warning("curve_scan_timeout", chain=self.chain)
            except Exception as e:
                log.error("curve_scan_failed", chain=self.chain, error=str(e))

        # If Convex is enabled, fetch boosted APYs
        if self.convex_enabled:
            await self._enrich_convex_apys(results)

        # Fallback for missing pools
        for pool_cfg in self.pools_cfg:
            if pool_cfg["name"] not in results:
                results[pool_cfg["name"]] = {
                    "protocol": "curve",
                    "chain": self.chain,
                    "base_apy": 0,
                    "tvl": 0,
                    "note": "Pool not found in API response",
                }

        log.info("curve_scan_complete", chain=self.chain, pools=len(results))
        return results

    async def _enrich_convex_apys(self, results: dict) -> None:
        """Add Convex-boosted APY data to Curve pool results."""
        async with aiohttp.ClientSession() as session:
            try:
                # Convex APY API
                async with session.get(
                    "https://www.convexfinance.com/api/curve-apys",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        convex_data = await resp.json()
                        apys = convex_data.get("apys", {})

                        for pool_name, pool_info in results.items():
                            pool_addr = pool_info.get("pool_address", "").lower()
                            if pool_addr in apys:
                                apy_data = apys[pool_addr]
                                pool_info["convex_base_apy"] = apy_data.get("baseApy", 0)
                                pool_info["convex_crv_apy"] = apy_data.get("crvApy", 0)
                                pool_info["convex_cvx_apy"] = apy_data.get("cvxApy", 0)
                                pool_info["convex_total_apy"] = (
                                    apy_data.get("baseApy", 0)
                                    + apy_data.get("crvApy", 0)
                                    + apy_data.get("cvxApy", 0)
                                )
                                pool_info["convex_boosted"] = True
                    else:
                        log.warning("convex_api_error", status=resp.status)

            except Exception as e:
                log.error("convex_apy_fetch_failed", error=str(e))
