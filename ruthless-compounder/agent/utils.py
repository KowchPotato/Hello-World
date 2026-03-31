"""
Shared utilities: config loader, DB init, wallet encryption, pause check.
"""

import base64
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = structlog.get_logger("ruthless.utils")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "history.db"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load and return the YAML config, substituting env vars."""
    with open(path) as f:
        raw = f.read()
    # Simple env-var substitution for ${VAR} patterns
    for key, val in os.environ.items():
        raw = raw.replace(f"${{{key}}}", val)
    return yaml.safe_load(raw)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    """Return a connection to the history database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL DEFAULT (datetime('now')),
            chain       TEXT NOT NULL,
            tx_hash     TEXT,
            tx_type     TEXT,
            protocol    TEXT,
            amount_usd  REAL,
            status      TEXT DEFAULT 'pending',
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL DEFAULT (datetime('now')),
            total_tvl   REAL,
            solana_tvl  REAL,
            evm_tvl     REAL,
            sleeve_tvl  REAL,
            pnl_24h     REAL,
            data_json   TEXT
        );

        CREATE TABLE IF NOT EXISTS llm_decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL DEFAULT (datetime('now')),
            prompt      TEXT,
            response    TEXT,
            actions     TEXT
        );

        CREATE TABLE IF NOT EXISTS risk_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL DEFAULT (datetime('now')),
            event_type  TEXT,
            severity    TEXT,
            details     TEXT,
            action_taken TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tx_ts ON transactions(ts);
        CREATE INDEX IF NOT EXISTS idx_snap_ts ON portfolio_snapshots(ts);
    """)
    conn.close()
    log.info("database_initialized", path=str(DB_PATH))


# ---------------------------------------------------------------------------
# Wallet Encryption / Decryption
# ---------------------------------------------------------------------------
def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet key from a password + salt via PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_private_key(private_key: str, password: str) -> str:
    """
    Encrypt a private key string. Returns base64-encoded salt+ciphertext.
    Use this offline to prepare your .env values.
    """
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    ct = Fernet(key).encrypt(private_key.encode())
    payload = base64.b64encode(salt + ct).decode()
    return payload


def decrypt_private_key(encrypted: str, password: str) -> str:
    """Decrypt a private key from base64-encoded salt+ciphertext."""
    raw = base64.b64decode(encrypted)
    salt, ct = raw[:16], raw[16:]
    key = _derive_key(password, salt)
    return Fernet(key).decrypt(ct).decode()


def get_solana_keypair():
    """Load and decrypt the Solana private key, return a Keypair."""
    from solders.keypair import Keypair  # type: ignore

    enc = os.environ.get("SOLANA_ENCRYPTED_KEY", "")
    pwd = os.environ.get("SOLANA_KEY_PASSWORD", "")
    if not enc or not pwd:
        raise RuntimeError("SOLANA_ENCRYPTED_KEY and SOLANA_KEY_PASSWORD must be set")
    raw = decrypt_private_key(enc, pwd)
    return Keypair.from_base58_string(raw)


def get_evm_private_key() -> str:
    """Load and decrypt the EVM private key."""
    enc = os.environ.get("EVM_ENCRYPTED_KEY", "")
    pwd = os.environ.get("EVM_KEY_PASSWORD", "")
    if not enc or not pwd:
        raise RuntimeError("EVM_ENCRYPTED_KEY and EVM_KEY_PASSWORD must be set")
    return decrypt_private_key(enc, pwd)


# ---------------------------------------------------------------------------
# Pause Check
# ---------------------------------------------------------------------------
def is_paused(config: dict[str, Any] | None = None) -> bool:
    """Return True if emergency pause file exists."""
    pause_file = "/tmp/ruthless_pause"
    if config:
        pause_file = config.get("security", {}).get("emergency_pause_file", pause_file)
    return Path(pause_file).exists()


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()
