"""
============================================================================
 SENTINEL ENGINE v10-LISTENER
 Polymarket Paper Trading + Self-Learning Bot
 Built on top of v9.5 Intelligence Layer
============================================================================

 MODE:        LIVE_MODE=False (paper only). SDK skeleton ready for flip.
 CAPITAL:     $120 USDC virtual ledger (real funds untouched)
 STRATEGIES:  A) Bid-Ask MM (RISKY)   B) Cross-Outcome Arb (RISKLESS)
 LIFECYCLE:   Hybrid price evolution (real CLOB midpoint + noise),
              max 3 parallel positions, 60s main loop.
 LEARNING:    Every 50 trades — Level 1 (category) + Level 2 (params).
 VAULT:       Virtual ledger only. Bot writes vault_balance.json,
              never touches MetaMask funds.

 DEPLOY:      Railway, Python 3.11+. ENV vars read via os.getenv only.
              NEVER logged.
============================================================================
"""

import asyncio
import aiohttp
import json
import logging
import os
import random
import time
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ============================================================================
# 1. CONFIG & CONSTANTS
# ============================================================================

# --- Hard switches ---------------------------------------------------------
LIVE_MODE = False                     # NEVER set True without manual review
SIMULATE_TEST = False                 # If True, runs 50-trade simulation then exits
LOG_LEVEL = logging.INFO

# --- Capital & sizing ------------------------------------------------------
STARTING_CAPITAL_USDC = 120.0
MAX_PARALLEL_POSITIONS = 3            # Per user spec
SIZE_HIGH = 20.0                      # confidence ≥75
SIZE_MID = 10.0                       # confidence 60-75
SIZE_LOW = 5.0                        # confidence 50-60

# --- Confidence scoring weights -------------------------------------------
SCORE_VOLUME_HIGH = 20                # vol > $50k
SCORE_SPREAD_WIDE = 25                # spread > 3%
SCORE_DEPTH_PRESENT = 15              # CLOB depth available
SCORE_HEALTHY_VOL = 15                # volatility 20-65
SCORE_CROSS_VALIDATED = 10            # price cross-validated
PENALTY_STALE = -30                   # stale data
PENALTY_ANOMALY = -40                 # detected anomaly

# --- Adaptive stop loss ----------------------------------------------------
STOP_HIGH = -0.12                     # confidence ≥75 → -12%
STOP_MID = -0.09                      # confidence 60-75 → -9%
STOP_LOW = -0.06                      # confidence 50-60 → -6%

# --- Adaptive take profit (4 tier ladder) ---------------------------------
# Each tier closes 25% of position
TP_HIGH = [0.08, 0.18, 0.30, 0.50]    # +8/+18/+30/+50%
TP_MID  = [0.06, 0.12, 0.20, 0.35]
TP_LOW  = [0.04, 0.08, 0.14, 0.22]
TIER_CLOSE_PCT = 0.25                 # 25% sold at each tier

# --- Trailing stop locks ---------------------------------------------------
# After tier hit, stop ratchets up to lock profit
TRAIL_AFTER_T2 = 0.05                 # tier 2 hit → stop +5%
TRAIL_AFTER_T3 = 0.15                 # tier 3 hit → stop +15%
TRAIL_AFTER_T4 = 0.30                 # tier 4 hit → stop +30%

# --- Vault routing ---------------------------------------------------------
VAULT_PCT_RISKY = 0.50                # Strategy A → 50% of net to vault
VAULT_PCT_RISKLESS = 0.20             # Strategy B → 20% of net to vault

# --- Filters (v9.5 carryover) ---------------------------------------------
GAMMA_MARKET_LIMIT = 500
MIN_VOLUME = 10_000.0
PRICE_CORRIDOR = (0.05, 0.95)         # dual-corridor
MIN_ABS_SPREAD = 0.02
TOP_N_FOR_DEPTH = 10                  # only fetch CLOB depth for top-10

# --- Costs (paper sim) -----------------------------------------------------
GAS_COST_USD = 0.03                   # per trade leg
PM_FEE_PCT = 0.0                      # Polymarket currently 0% taker fee
SLIPPAGE_BASE_USD = 0.02              # base slippage estimate

# --- Self-stop triggers ----------------------------------------------------
MAX_CONSECUTIVE_LOSSES = 3
DAILY_DRAWDOWN_PCT = -0.05            # -5% of capital
MIN_MATIC_USD = 1.0                   # halt if MATIC balance < $1
API_DOWNTIME_SECONDS = 600            # 10 minutes
MAX_SINGLE_SLIPPAGE_USD = 5.0

# --- Self-learning ---------------------------------------------------------
CHECKPOINT_INTERVAL = 50              # trades per learning checkpoint
CATEGORY_SUSPEND_THRESHOLD = 0.45     # <45% winrate → 1 week suspension
CATEGORY_BOOST_THRESHOLD = 0.65       # >65% winrate → +20% budget
CATEGORY_SUSPEND_DAYS = 7

# --- Loop & API -----------------------------------------------------------
MAIN_LOOP_INTERVAL = 60               # seconds
POSITION_TICK_INTERVAL = 60           # seconds between price updates per pos
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# --- File paths ------------------------------------------------------------
DATA_DIR = Path(os.getenv("SENTINEL_DATA_DIR", "."))
LEARNING_FILE = DATA_DIR / "learning_state.json"
VAULT_FILE = DATA_DIR / "vault_balance.json"
PAPER_TRADES_FILE = DATA_DIR / "paper_trades.json"
SPREAD_HISTORY_FILE = DATA_DIR / "spread_history.json"
STATS_FILE = DATA_DIR / "sentinel_stats.json"

# --- ENV (never log) -------------------------------------------------------
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PRIV_KEY = os.getenv("POLYGON_PRIVATE_KEY")     # only read, never logged
WALLET_ADDR = os.getenv("POLYGON_WALLET_ADDRESS")  # only read, never logged

# ============================================================================
# 2. LOGGING
# ============================================================================

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentinel")


# ============================================================================
# 3. ENUMS & DATACLASSES
# ============================================================================

class Strategy(Enum):
    RISKY = "RISKY"           # bid-ask MM (Strategy A)
    RISKLESS = "RISKLESS"     # cross-outcome arb (Strategy B)


class Category(Enum):
    SPORTSBOOK = "sportsbook"
    POLITICS = "politics"
    CRYPTO = "crypto"
    OTHER = "other"


class PositionStatus(Enum):
    OPEN = "open"
    CLOSED_TP = "closed_tp"      # closed by take profit ladder
    CLOSED_SL = "closed_sl"      # closed by stop loss
    CLOSED_TRAIL = "closed_trail"
    CLOSED_MANUAL = "closed_manual"


@dataclass
class Market:
    """Snapshot of a Polymarket market for scanning."""
    market_id: str                # Gamma market id
    condition_id: str             # on-chain condition id
    yes_token_id: str             # CLOB token id for YES outcome
    no_token_id: str              # CLOB token id for NO outcome
    slug: str
    question: str
    category: Category
    volume_24h: float
    yes_price: float
    no_price: float
    bid: float
    ask: float
    spread: float
    has_depth: bool = False
    volatility: float = 0.0
    confidence: int = 0
    strategy: Optional[Strategy] = None


@dataclass
class Position:
    """Open paper position with full lifecycle state."""
    trade_num: int
    market_id: str
    yes_token_id: str                      # For CLOB orderbook polling
    market_slug: str
    question: str
    category: Category
    strategy: Strategy
    confidence: int
    entry_price: float
    size_usd: float
    shares: float                          # size_usd / entry_price
    entry_time: float
    tp_targets: list[float]                # 4 absolute prices
    sl_price: float                        # absolute
    tier_hits: list[bool] = field(default_factory=lambda: [False]*4)
    closed_pct: float = 0.0                # cumulative %
    realized_pnl: float = 0.0              # gross realized so far
    last_price: float = 0.0                # for tick simulation
    status: PositionStatus = PositionStatus.OPEN
    close_time: Optional[float] = None
    total_gas: float = 0.0
    total_slippage: float = 0.0
    notes: str = ""


# ============================================================================
# 4. STATE MANAGERS
# ============================================================================

def _safe_load_json(path: Path, default: Any) -> Any:
    """Load JSON file or return default if missing/corrupt."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Could not load {path.name}: {e} — using default")
        return default


def _safe_write_json(path: Path, data: Any) -> None:
    """Atomic write: write to .tmp then rename. Survives Railway restarts."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


class VaultManager:
    """
    Virtual vault ledger. Bot NEVER moves real funds.
    User manually mirrors vault_balance to a separate MetaMask address if desired.
    """
    def __init__(self):
        self.state = _safe_load_json(VAULT_FILE, {
            "balance": 0.0,
            "lifetime_deposits": 0.0,
            "history": []
        })

    def deposit(self, net_profit: float, strategy: Strategy, trade_num: int) -> float:
        """Routes a portion of NET profit to vault per strategy. Returns deposited amount."""
        if net_profit <= 0:
            return 0.0
        pct = VAULT_PCT_RISKY if strategy == Strategy.RISKY else VAULT_PCT_RISKLESS
        amount = round(net_profit * pct, 4)
        self.state["balance"] = round(self.state["balance"] + amount, 4)
        self.state["lifetime_deposits"] = round(self.state["lifetime_deposits"] + amount, 4)
        self.state["history"].append({
            "trade_num": trade_num,
            "ts": time.time(),
            "amount": amount,
            "strategy": strategy.value,
            "balance_after": self.state["balance"],
        })
        # Trim history to last 500
        self.state["history"] = self.state["history"][-500:]
        self._save()
        return amount

    @property
    def balance(self) -> float:
        return self.state["balance"]

    def _save(self):
        _safe_write_json(VAULT_FILE, self.state)


class LearningState:
    """
    Tracks per-category and per-tier performance. Adjusts behavior every 50 trades.

    Schema:
    {
      "trades_processed": int,
      "last_checkpoint": int,
      "categories": {
        "sportsbook": {"wins": int, "losses": int, "suspended_until": ts | null,
                       "budget_multiplier": float},
        ...
      },
      "tier_hits": {"t1": int, "t2": int, "t3": int, "t4": int},
      "stops_triggered": int,
      "confidence_threshold": int,   // dynamic floor (default 50)
      "checkpoint_history": [...]
    }
    """
    DEFAULT = {
        "trades_processed": 0,
        "last_checkpoint": 0,
        "categories": {c.value: {
            "wins": 0, "losses": 0,
            "suspended_until": None,
            "budget_multiplier": 1.0,
        } for c in Category},
        "tier_hits": {"t1": 0, "t2": 0, "t3": 0, "t4": 0},
        "stops_triggered": 0,
        "confidence_threshold": 50,
        "checkpoint_history": [],
    }

    def __init__(self):
        self.state = _safe_load_json(LEARNING_FILE, self.DEFAULT)
        # Backfill missing keys (forward-compat)
        for k, v in self.DEFAULT.items():
            self.state.setdefault(k, v)

    def record_trade(self, pos: Position, won: bool):
        cat = self.state["categories"][pos.category.value]
        if won:
            cat["wins"] += 1
        else:
            cat["losses"] += 1
        for i, hit in enumerate(pos.tier_hits, start=1):
            if hit:
                self.state["tier_hits"][f"t{i}"] += 1
        if pos.status == PositionStatus.CLOSED_SL:
            self.state["stops_triggered"] += 1
        self.state["trades_processed"] += 1
        self._save()

    def category_allowed(self, cat: Category) -> bool:
        c = self.state["categories"][cat.value]
        if c["suspended_until"] is None:
            return True
        return time.time() >= c["suspended_until"]

    def category_budget_mult(self, cat: Category) -> float:
        return self.state["categories"][cat.value]["budget_multiplier"]

    def confidence_floor(self) -> int:
        return self.state["confidence_threshold"]

    def should_run_checkpoint(self) -> bool:
        return (self.state["trades_processed"] - self.state["last_checkpoint"]) >= CHECKPOINT_INTERVAL

    def run_checkpoint(self) -> dict:
        """
        Level 1: category audit (suspend / boost).
        Level 2: parameter tuning (confidence threshold based on win rate trend).
        Returns checkpoint summary dict for Telegram report.
        """
        summary = {
            "ts": time.time(),
            "trades_processed": self.state["trades_processed"],
            "category_actions": [],
            "param_actions": [],
        }
        # --- Level 1 ---
        for cat_name, cat in self.state["categories"].items():
            total = cat["wins"] + cat["losses"]
            if total < 10:
                continue                # insufficient sample
            wr = cat["wins"] / total
            if wr < CATEGORY_SUSPEND_THRESHOLD:
                cat["suspended_until"] = time.time() + CATEGORY_SUSPEND_DAYS * 86400
                summary["category_actions"].append(f"⛔ {cat_name} suspended 7d (WR {wr:.0%})")
            elif wr > CATEGORY_BOOST_THRESHOLD:
                cat["budget_multiplier"] = min(cat["budget_multiplier"] * 1.20, 2.0)
                summary["category_actions"].append(
                    f"🚀 {cat_name} budget x{cat['budget_multiplier']:.2f} (WR {wr:.0%})"
                )

        # --- Level 2 ---
        # If too many stops triggered (>30% of trades), raise confidence floor.
        # If win rate is high but volume low (few trades), lower it.
        stops = self.state["stops_triggered"]
        recent = self.state["trades_processed"]
        if recent >= CHECKPOINT_INTERVAL:
            stop_rate = stops / max(
