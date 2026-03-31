"""
Solana Scanner — Fetches yield data from Kamino and Meteora on Solana.

Uses solana-py + solders for RPC calls, plus protocol-specific APIs.
"""

import asyncio
from typing import Any

import aiohttp
import structlog
from solana.rpc.async_api import AsyncClient

log = structlog.get_logger("ruthless.scanners.solana")

# Kamino API endpoints (public, no key needed)
KAMINO_API_BASE = "https://api.kamino.finance"
METEORA_API_BASE = "https://dlmm-api.meteora.ag"


class SolanaScanner:
    """Scan Solana DeFi protocols for current yields."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        sol_cfg = config.get("solana", {})
        rpcs = sol_cfg.get("rpc_endpoints", ["https://api.devnet.solana.com"])
        self.rpc_url = rpcs[0]
        self.kamino_cfg = sol_cfg.get("kamino", {})
        self.meteora_cfg = sol_cfg.get("meteora", {})

    async def scan_kamino(self) -> dict:
        """
        Fetch Kamino Multiply strategy yields.

        Returns dict of strategy_name -> {apy, tvl, leverage, ...}
        """
        results = {}
        strategies = self.kamino_cfg.get("multiply_strategies", [])

        async with aiohttp.ClientSession() as session:
            for strat in strategies:
                name = strat["name"]
                try:
                    # Kamino public API for strategy metrics
                    # Note: exact endpoints may change; this is the v2 API pattern
                    async with session.get(
                        f"{KAMINO_API_BASE}/strategies",
                        params={"status": "LIVE"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Filter for relevant strategies
                            for s in data if isinstance(data, list) else []:
                                results[s.get("name", name)] = {
                                    "protocol": "kamino",
                                    "apy": s.get("apy", 0),
                                    "tvl": s.get("tvl", 0),
                                    "leverage": s.get("leverage", 1.0),
                                    "market": strat.get("market", "SOL"),
                                    "min_apy_threshold": strat.get("min_apy_threshold", 0),
                                }
                        else:
                            log.warning("kamino_api_error", status=resp.status, strategy=name)

                except asyncio.TimeoutError:
                    log.warning("kamino_scan_timeout", strategy=name)
                except Exception as e:
                    log.error("kamino_scan_failed", strategy=name, error=str(e))

        # If API returned nothing, provide a placeholder
        if not results:
            for strat in strategies:
                results[strat["name"]] = {
                    "protocol": "kamino",
                    "apy": 0,
                    "tvl": 0,
                    "note": "API unavailable — using cached or zero data",
                }

        log.info("kamino_scan_complete", strategies=len(results))
        return results

    async def scan_meteora(self) -> dict:
        """
        Fetch Meteora DLMM vault yields.

        Returns dict of vault_name -> {apy, tvl, bin_step, ...}
        """
        results = {}
        vaults = self.meteora_cfg.get("dlmm_vaults", [])

        async with aiohttp.ClientSession() as session:
            for vault in vaults:
                name = vault["name"]
                try:
                    # Meteora DLMM public API
                    async with session.get(
                        f"{METEORA_API_BASE}/pair/all",
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pair_str = vault.get("pair", "")
                            # Search for matching pair
                            for pool in data if isinstance(data, list) else []:
                                pool_name = pool.get("name", "")
                                if pair_str.replace("/", "-") in pool_name or pair_str in pool_name:
                                    results[name] = {
                                        "protocol": "meteora",
                                        "apy": pool.get("apr", pool.get("apy", 0)),
                                        "tvl": pool.get("liquidity", 0),
                                        "bin_step": pool.get("bin_step", vault.get("bin_step", 0)),
                                        "volume_24h": pool.get("trade_volume_24h", 0),
                                        "fees_24h": pool.get("fees_24h", 0),
                                        "pair": pair_str,
                                        "min_apy_threshold": vault.get("min_apy_threshold", 0),
                                    }
                                    break
                        else:
                            log.warning("meteora_api_error", status=resp.status, vault=name)

                except asyncio.TimeoutError:
                    log.warning("meteora_scan_timeout", vault=name)
                except Exception as e:
                    log.error("meteora_scan_failed", vault=name, error=str(e))

        if not results:
            for vault in vaults:
                results[vault["name"]] = {
                    "protocol": "meteora",
                    "apy": 0,
                    "tvl": 0,
                    "note": "API unavailable",
                }

        log.info("meteora_scan_complete", vaults=len(results))
        return results

    async def get_sol_balance(self) -> float:
        """Get SOL balance of the hot wallet."""
        try:
            from agent.utils import get_solana_keypair
            kp = get_solana_keypair()
            async with AsyncClient(self.rpc_url) as client:
                resp = await client.get_balance(kp.pubkey())
                lamports = resp.value
                return lamports / 1e9  # Convert lamports to SOL
        except Exception as e:
            log.error("sol_balance_fetch_failed", error=str(e))
            return 0.0
