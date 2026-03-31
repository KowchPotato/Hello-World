# RuthlessCompounder

Autonomous DeFi compounding agent that runs 24/7 on a local machine (64 GB RAM + RTX 5090). Uses a local LLM (Ollama) to make daily rebalancing decisions across Solana and EVM L2 chains.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  APScheduler                     │
│  ┌──────────────┐     ┌──────────────────────┐  │
│  │  Daily Cron   │     │  Risk Sentinel (5m)  │  │
│  │  (00:00 UTC)  │     │  (almost never txs)  │  │
│  └──────┬───────┘     └──────────┬───────────┘  │
│         │                        │               │
│  ┌──────▼────────────────────────▼──────────┐   │
│  │           Decision Engine                 │   │
│  │     LangGraph + Ollama (Qwen2.5-72B)     │   │
│  └──────┬───────────────────────┬───────────┘   │
│  ┌──────▼───────┐      ┌───────▼──────────┐    │
│  │ Solana Exec   │      │   EVM Executor   │    │
│  │ Kamino/Meteora│      │  Curve/Convex    │    │
│  └──────────────┘      └──────────────────┘    │
│  ┌──────────────────────────────────────────┐   │
│  │       Trading Sleeve (Drift/Jupiter)      │   │
│  │       10-15% | Max 5x | 2% risk/trade    │   │
│  └──────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────┐   │
│  │          TX Budget Enforcer               │   │
│  │       Hard cap: 295,000 tx/year           │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
         │
    ┌────▼─────┐
    │ Streamlit │  :8501
    │ Dashboard │
    └──────────┘
```

## Supported Protocols

| Chain    | Protocol       | Type                    |
|----------|---------------|-------------------------|
| Solana   | Kamino         | Multiply (leveraged)    |
| Solana   | Meteora        | DLMM vaults             |
| Solana   | Drift          | Perpetual futures       |
| Solana   | Jupiter        | Swaps + Perps           |
| Arbitrum | Curve          | Stable/volatile pools   |
| Arbitrum | Convex         | Boosted Curve staking   |
| Base     | Curve          | Stable pools            |

## Step-by-Step Setup

### Prerequisites

- Docker + Docker Compose (with NVIDIA Container Toolkit)
- NVIDIA GPU with ≥24 GB VRAM (RTX 5090 recommended)
- `nvidia-smi` working on host

### 1. Clone & Configure

```bash
git clone <repo-url>
cd ruthless-compounder
cp .env.example .env
```

### 2. Encrypt Your Private Keys

**Never store raw private keys in .env.** Use the encryption helper:

```bash
python3 -c "
from agent.utils import encrypt_private_key
import getpass

key = getpass.getpass('Private key: ')
pwd = getpass.getpass('Password: ')
print('Encrypted:', encrypt_private_key(key, pwd))
"
```

Paste the output into `.env` as `SOLANA_ENCRYPTED_KEY` / `EVM_ENCRYPTED_KEY`.
Set the passwords as `SOLANA_KEY_PASSWORD` / `EVM_KEY_PASSWORD`.

### 3. Pull the LLM Model

```bash
# Start Ollama first
docker compose up -d ollama

# Pull the model (this downloads ~40 GB)
docker exec ruthless-ollama ollama pull qwen2.5:72b-instruct-q4_K_M

# Alternative smaller model:
# docker exec ruthless-ollama ollama pull llama3.3:70b-instruct-q4_K_M
```

### 4. Start Everything

```bash
docker compose up -d
```

Services:
- **Agent**: runs the daily compounder + risk sentinel
- **Ollama**: local LLM server on GPU
- **Dashboard**: Streamlit UI at http://localhost:8501

### 5. Fund the Hot Wallet

**Testnet (recommended first):**

```bash
# Solana devnet
solana airdrop 2 <YOUR_SOLANA_PUBKEY> --url devnet

# EVM testnets: use faucets for Base Sepolia / Arbitrum Sepolia
```

**Mainnet:**
- Transfer SOL to the Solana hot wallet pubkey
- Transfer ETH to the EVM hot wallet address on Base and/or Arbitrum
- Keep the hot wallet **small** — only what you need for active strategies
- Store the bulk of your funds in a cold wallet (hardware wallet)

## How to Toggle the Trading Sleeve

Edit `config.yaml`:

```yaml
trading_sleeve:
  enabled: false    # set to false to disable perps entirely
```

Or set `allocation.trading_sleeve_pct: 0`.

Restart the agent: `docker compose restart agent`

## Example LLM Prompt Template

The decision engine sends this to Ollama:

```
You are RuthlessCompounder, an autonomous DeFi yield optimization agent.

CURRENT PORTFOLIO:
{ "total_tvl": 50000, "solana_tvl": 22000, "evm_tvl": 20000, ... }

AVAILABLE YIELDS:
{ "solana_kamino": { "SOL-USDC Multiply": { "apy": 12.5, "tvl": 1000000 } }, ... }

RISK METRICS:
{ "portfolio_tvl": 50000, "recent_risk_events": [] }

TX BUDGET:
- Used this year: 150
- Remaining: 294850
- Projected annual: 450

RULES:
1. Minimize transactions — batch where possible.
2. Only move capital if new position's APY exceeds current by ≥2%.
3. Never exceed leverage limits.
...

Output ONLY valid JSON: { "reasoning": "...", "actions": [...], "estimated_tx_count": N, "confidence": 0-100 }
```

## Security Checklist

- [ ] Private keys encrypted with strong passwords (≥20 chars)
- [ ] `.env` file has `chmod 600` permissions
- [ ] Testnet mode verified before mainnet
- [ ] Hot wallet contains only actively-used funds
- [ ] Cold wallet holds the majority of assets
- [ ] `max_single_tx_value_usd` set appropriately in config
- [ ] Emergency pause tested (`touch /tmp/ruthless_pause`)
- [ ] Telegram alerts configured for critical events
- [ ] Docker container runs as non-root user (consider adding in Dockerfile)
- [ ] Firewall blocks all incoming ports except 8501 (dashboard)
- [ ] RPC endpoints are not leaking keys (use public RPCs)

## How to Monitor TX Count

### Dashboard
The Overview tab shows a gauge chart with YTD transaction count vs. annual cap.

### CLI
```bash
# Connect to the SQLite database
sqlite3 data/history.db "SELECT COUNT(*) FROM transactions WHERE ts >= datetime('now', 'start of year');"

# Projected annual rate
sqlite3 data/history.db "
  SELECT
    COUNT(*) as used,
    CAST(COUNT(*) * 365.0 / MAX(1, julianday('now') - julianday(datetime('now', 'start of year'))) AS INT) as projected
  FROM transactions
  WHERE ts >= datetime('now', 'start of year');
"
```

### Logs
Search for `tx_budget` entries:
```bash
grep "tx_budget" logs/*.log
```

## How to Add New Protocols

1. **Create a scanner** in `agent/scanners/` (e.g., `aave_scanner.py`)
   - Implement an async `scan()` method returning yield data
2. **Create or extend an executor** in `agent/executors/`
   - Add protocol-specific deposit/withdraw/harvest methods
3. **Register in core_loop.py**
   - Add the scanner to `_scan_all_yields()`
   - Add the executor dispatch in `_execute_actions()`
4. **Update config.yaml** with the new protocol's parameters
5. **Update the LLM prompt** in `decision_engine.py` if the new protocol has unique properties

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Ollama OOM | Use a smaller quantization: `qwen2.5:72b-instruct-q3_K_M` |
| "TX budget exceeded" | Wait for next fiscal year or increase `max_annual_transactions` |
| Dashboard blank | Check if `data/history.db` exists; run `python -c "from agent.utils import init_db; init_db()"` |
| Agent paused | `rm /tmp/ruthless_pause` |
| RPC rate limited | Add more RPC endpoints to `config.yaml` |

## License

Private / Internal Use Only.
