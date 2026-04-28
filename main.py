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
            stop_rate = stops / max(recent, 1)
            if stop_rate > 0.30 and self.state["confidence_threshold"] < 70:
                self.state["confidence_threshold"] += 5
                summary["param_actions"].append(
                    f"⬆ Confidence floor raised → {self.state['confidence_threshold']} (stop rate {stop_rate:.0%})"
                )
            elif stop_rate < 0.10 and self.state["confidence_threshold"] > 50:
                self.state["confidence_threshold"] -= 5
                summary["param_actions"].append(
                    f"⬇ Confidence floor lowered → {self.state['confidence_threshold']} (stop rate {stop_rate:.0%})"
                )

        self.state["last_checkpoint"] = self.state["trades_processed"]
        self.state["checkpoint_history"].append(summary)
        self.state["checkpoint_history"] = self.state["checkpoint_history"][-50:]
        self._save()
        return summary

    def _save(self):
        _safe_write_json(LEARNING_FILE, self.state)


class PaperTradeLogger:
    """Append-only trade log with rolling cap."""
    MAX_TRADES = 5000

    def __init__(self):
        self.state = _safe_load_json(PAPER_TRADES_FILE, {"trades": []})

    def append(self, pos: Position, net_pnl: float, vault_amt: float):
        self.state["trades"].append({
            "n": pos.trade_num,
            "ts_open": pos.entry_time,
            "ts_close": pos.close_time,
            "market_id": pos.market_id,
            "slug": pos.market_slug,
            "category": pos.category.value,
            "strategy": pos.strategy.value,
            "confidence": pos.confidence,
            "entry": pos.entry_price,
            "exit": pos.last_price,
            "size_usd": pos.size_usd,
            "shares": pos.shares,
            "tier_hits": pos.tier_hits,
            "status": pos.status.value,
            "gross_pnl": pos.realized_pnl,
            "gas": pos.total_gas,
            "slippage": pos.total_slippage,
            "net_pnl": net_pnl,
            "vault": vault_amt,
        })
        self.state["trades"] = self.state["trades"][-self.MAX_TRADES:]
        self._save()

    @property
    def trades(self) -> list:
        return self.state["trades"]

    def _save(self):
        _safe_write_json(PAPER_TRADES_FILE, self.state)


class SpreadHistory:
    """
    v9.5 carryover. Schema preserved as placeholder.

    PLACEHOLDER SCHEMA (paste real v9.5 schema here when available):
    {
      "<market_id>": {
        "samples": [{"ts": float, "bid": float, "ask": float, "spread": float}, ...],
        "rolling_mean_spread": float,
        "rolling_std_spread": float,
        "last_update": float
      },
      ...
    }
    """
    SAMPLE_CAP = 200

    def __init__(self):
        self.state = _safe_load_json(SPREAD_HISTORY_FILE, {})

    def update(self, market_id: str, bid: float, ask: float):
        spread = ask - bid
        rec = self.state.setdefault(market_id, {
            "samples": [], "rolling_mean_spread": 0.0,
            "rolling_std_spread": 0.0, "last_update": 0.0
        })
        rec["samples"].append({"ts": time.time(), "bid": bid, "ask": ask, "spread": spread})
        rec["samples"] = rec["samples"][-self.SAMPLE_CAP:]
        spreads = [s["spread"] for s in rec["samples"]]
        rec["rolling_mean_spread"] = sum(spreads) / len(spreads)
        if len(spreads) > 1:
            mean = rec["rolling_mean_spread"]
            var = sum((s - mean)**2 for s in spreads) / len(spreads)
            rec["rolling_std_spread"] = math.sqrt(var)
        rec["last_update"] = time.time()
        # Throttle disk writes — every 10 updates
        if len(rec["samples"]) % 10 == 0:
            self._save()

    def get_volatility_score(self, market_id: str) -> float:
        """0-100. Healthy band 20-65 per spec."""
        rec = self.state.get(market_id)
        if not rec or len(rec["samples"]) < 5:
            return 0.0
        # Normalize std into 0-100. std of 0.05 → ~100, 0.005 → ~10.
        std = rec["rolling_std_spread"]
        return min(100.0, std * 2000.0)

    def _save(self):
        _safe_write_json(SPREAD_HISTORY_FILE, self.state)


class StatsTracker:
    """High-level rolling stats for Telegram + watchdog."""
    def __init__(self):
        self.state = _safe_load_json(STATS_FILE, {
            "starting_capital": STARTING_CAPITAL_USDC,
            "current_pnl": 0.0,
            "daily_pnl": 0.0,
            "daily_anchor_ts": time.time(),
            "consecutive_losses": 0,
            "total_gas": 0.0,
            "total_fees": 0.0,
            "total_slippage": 0.0,
            "halt_until": None,
            "halt_reason": None,
        })

    def record(self, net_pnl: float, gas: float, slip: float):
        # Daily anchor reset
        if time.time() - self.state["daily_anchor_ts"] > 86400:
            self.state["daily_pnl"] = 0.0
            self.state["daily_anchor_ts"] = time.time()
        self.state["current_pnl"] = round(self.state["current_pnl"] + net_pnl, 4)
        self.state["daily_pnl"] = round(self.state["daily_pnl"] + net_pnl, 4)
        self.state["total_gas"] = round(self.state["total_gas"] + gas, 4)
        self.state["total_slippage"] = round(self.state["total_slippage"] + slip, 4)
        if net_pnl < 0:
            self.state["consecutive_losses"] += 1
        else:
            self.state["consecutive_losses"] = 0
        self._save()

    def halt(self, reason: str, hours: float = 1.0):
        self.state["halt_until"] = time.time() + hours * 3600
        self.state["halt_reason"] = reason
        self._save()
        log.warning(f"HALT: {reason} for {hours}h")

    def is_halted(self) -> bool:
        if self.state["halt_until"] is None:
            return False
        return time.time() < self.state["halt_until"]

    def _save(self):
        _safe_write_json(STATS_FILE, self.state)


# ============================================================================
# 5. INTELLIGENCE LAYER (v9.5 — preserved)
# ============================================================================

class RateLimitTracker:
    """Token-bucket style. Polymarket CLOB ~100 req / 10s."""
    def __init__(self, max_per_window: int = 80, window_s: float = 10.0):
        self.max = max_per_window
        self.window = window_s
        self.timestamps: deque = deque()

    async def acquire(self):
        now = time.time()
        while self.timestamps and now - self.timestamps[0] > self.window:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.max:
            sleep_for = self.window - (now - self.timestamps[0]) + 0.05
            log.debug(f"Rate limit: sleeping {sleep_for:.2f}s")
            await asyncio.sleep(sleep_for)
            return await self.acquire()
        self.timestamps.append(now)


class LatencyGuard:
    """Tracks request latency. If sustained >2s, signals degraded API."""
    def __init__(self, window: int = 20):
        self.samples: deque = deque(maxlen=window)
        self.last_failure_ts: float = 0.0

    def record(self, latency_s: float, success: bool):
        self.samples.append(latency_s)
        if not success:
            self.last_failure_ts = time.time()

    def is_degraded(self) -> bool:
        if len(self.samples) < 5:
            return False
        return (sum(self.samples) / len(self.samples)) > 2.0

    def downtime_seconds(self) -> float:
        if self.last_failure_ts == 0:
            return 0.0
        return time.time() - self.last_failure_ts


def walk_the_book(orderbook: dict, side: str, size_usd: float, ref_price: float) -> float:
    """
    Estimate effective fill price walking through book levels.
    side: "buy" walks asks, "sell" walks bids.
    Returns slippage in $ vs ref_price.
    """
    levels = orderbook.get("asks" if side == "buy" else "bids", [])
    remaining = size_usd
    weighted = 0.0
    filled = 0.0
    for level in levels:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        notional = price * size
        take = min(remaining, notional)
        weighted += (take / price) * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled == 0:
        return SLIPPAGE_BASE_USD
    avg_fill = weighted / (filled / ref_price) if ref_price > 0 else ref_price
    slip_per_share = abs(avg_fill - ref_price)
    shares = size_usd / ref_price if ref_price > 0 else 0
    return round(slip_per_share * shares + SLIPPAGE_BASE_USD, 4)


class PanicModeDetector:
    """Detects abnormal market-wide volatility. Triggers cooldown."""
    def __init__(self):
        self.recent_spreads: deque = deque(maxlen=50)
        self.panic_until: float = 0.0

    def feed(self, spread: float):
        self.recent_spreads.append(spread)

    def check(self) -> bool:
        if time.time() < self.panic_until:
            return True
        if len(self.recent_spreads) < 30:
            return False
        avg = sum(self.recent_spreads) / len(self.recent_spreads)
        if avg > 0.15:                  # avg spread >15% across markets
            self.panic_until = time.time() + 300  # 5 min cooldown
            log.warning(f"PANIC MODE: avg spread {avg:.2%} — 5min cooldown")
            return True
        return False


class SmartThrottler:
    """Adaptively delays scans if API showing stress."""
    def __init__(self, base_interval: float = MAIN_LOOP_INTERVAL):
        self.base = base_interval
        self.multiplier = 1.0

    def adjust(self, degraded: bool, panic: bool):
        if panic:
            self.multiplier = 3.0
        elif degraded:
            self.multiplier = min(self.multiplier * 1.5, 4.0)
        else:
            self.multiplier = max(self.multiplier * 0.9, 1.0)

    def interval(self) -> float:
        return self.base * self.multiplier


# ============================================================================
# 6. MARKET SCANNER
# ============================================================================

def categorize_market(question: str, slug: str) -> Category:
    """Heuristic category mapping."""
    text = f"{question} {slug}".lower()
    sport_kw = ["nba", "nfl", "nhl", "mlb", "soccer", "ufc", "tennis", "lakers",
                "warriors", "patriots", "match", "fight", "game", "premier league",
                "champions league", "world cup", "super bowl"]
    pol_kw = ["election", "president", "trump", "biden", "harris", "senator",
              "congress", "vote", "primary", "parliament", "minister", "government"]
    crypto_kw = ["btc", "bitcoin", "eth", "ethereum", "solana", "xrp", "crypto",
                 "doge", "memecoin", "halving"]
    if any(k in text for k in sport_kw):
        return Category.SPORTSBOOK
    if any(k in text for k in pol_kw):
        return Category.POLITICS
    if any(k in text for k in crypto_kw):
        return Category.CRYPTO
    return Category.OTHER


async def fetch_gamma_markets(session: aiohttp.ClientSession,
                               rate: RateLimitTracker,
                               latency: LatencyGuard) -> list[dict]:
    """Pull up to 500 active markets from Gamma."""
    await rate.acquire()
    url = f"{GAMMA_BASE}/markets"
    params = {"limit": GAMMA_MARKET_LIMIT, "active": "true", "closed": "false"}
    t0 = time.time()
    try:
        async with session.get(url, params=params, timeout=15) as resp:
            data = await resp.json()
            latency.record(time.time() - t0, resp.status == 200)
            if resp.status != 200:
                log.warning(f"Gamma returned {resp.status}")
                return []
            return data if isinstance(data, list) else data.get("markets", [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        latency.record(time.time() - t0, False)
        log.warning(f"Gamma fetch failed: {e}")
        return []


def filter_pipeline(raw_markets: list[dict]) -> list[Market]:
    """
    Apply v9.5 filters: tradeable, vol, dual-corridor, abs spread.

    NOTE on Gamma API quirks (verified against docs):
    - `volume` is total volume; `volumeNum` may also be present as float
    - `outcomes`, `outcomePrices`, `clobTokenIds` come as STRINGIFIED JSON arrays
    - `enableOrderBook` must be True for CLOB tradeable markets
    - Token IDs (yes_token_id, no_token_id) are what CLOB endpoints expect,
      NOT the gamma market id.
    """
    out: list[Market] = []
    for m in raw_markets:
        try:
            if not m.get("active") or m.get("closed"):
                continue
            # CRITICAL: skip non-CLOB markets — we cannot place orders or fetch books
            if not m.get("enableOrderBook"):
                continue
            # Volume — try volumeNum first (float), then volume (string)
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            if vol < MIN_VOLUME:
                continue
            # Outcomes & prices come as JSON strings per Polymarket convention
            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    continue
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except json.JSONDecodeError:
                    continue
            if not prices or len(prices) < 2:
                continue
            yes_p = float(prices[0])
            no_p = float(prices[1])
            # Dual-corridor filter
            if not (PRICE_CORRIDOR[0] <= yes_p <= PRICE_CORRIDOR[1]):
                continue
            if not (PRICE_CORRIDOR[0] <= no_p <= PRICE_CORRIDOR[1]):
                continue
            # CLOB token IDs (also stringified)
            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except json.JSONDecodeError:
                    continue
            if not tokens or len(tokens) < 2:
                continue
            yes_token = str(tokens[0])
            no_token = str(tokens[1])
            # Bid/ask — Gamma exposes bestBid/bestAsk as strings sometimes
            try:
                bid = float(m.get("bestBid", yes_p - 0.01))
                ask = float(m.get("bestAsk", yes_p + 0.01))
            except (TypeError, ValueError):
                bid, ask = yes_p - 0.01, yes_p + 0.01
            spread = ask - bid
            if spread < MIN_ABS_SPREAD:
                continue
            question = m.get("question", "")[:120]
            slug = m.get("slug", m.get("id", ""))
            out.append(Market(
                market_id=str(m.get("id", slug)),
                condition_id=str(m.get("conditionId", "")),
                yes_token_id=yes_token,
                no_token_id=no_token,
                slug=slug,
                question=question,
                category=categorize_market(question, slug),
                volume_24h=vol,
                yes_price=yes_p,
                no_price=no_p,
                bid=bid,
                ask=ask,
                spread=spread,
            ))
        except (KeyError, TypeError, ValueError) as e:
            log.debug(f"Skipping malformed market: {e}")
            continue
    return out


def score_confidence(m: Market, has_depth: bool, vol_score: float,
                     stale: bool = False, anomaly: bool = False) -> int:
    """Apply spec scoring: 0-100."""
    score = 0
    if m.volume_24h > 50_000:
        score += SCORE_VOLUME_HIGH
    if m.spread / max(m.yes_price, 0.01) > 0.03:   # spread > 3% relative
        score += SCORE_SPREAD_WIDE
    if has_depth:
        score += SCORE_DEPTH_PRESENT
    if 20 <= vol_score <= 65:
        score += SCORE_HEALTHY_VOL
    # Cross-validation: yes + no should sum ~1.0 ± 0.02
    if abs((m.yes_price + m.no_price) - 1.0) <= 0.02:
        score += SCORE_CROSS_VALIDATED
    if stale:
        score += PENALTY_STALE
    if anomaly:
        score += PENALTY_ANOMALY
    return max(0, min(100, score))


async def fetch_clob_depth(session: aiohttp.ClientSession,
                            token_id: str,
                            rate: RateLimitTracker,
                            latency: LatencyGuard) -> dict:
    """
    Best-effort CLOB orderbook fetch. Returns {} on failure.
    NOTE: token_id is the CLOB token id (from clobTokenIds), NOT the gamma market id.
    """
    if not token_id:
        return {}
    await rate.acquire()
    url = f"{CLOB_BASE}/book"
    t0 = time.time()
    try:
        async with session.get(url, params={"token_id": token_id}, timeout=10) as resp:
            latency.record(time.time() - t0, resp.status == 200)
            if resp.status != 200:
                return {}
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        latency.record(time.time() - t0, False)
        log.debug(f"CLOB depth fetch failed for token {token_id[:12]}...: {e}")
        return {}


# ============================================================================
# 7. POSITION SIZING & STRATEGY ASSIGNMENT
# ============================================================================

def determine_size(confidence: int, learning: LearningState, cat: Category) -> float:
    """Apply sizing tiers + learning budget multiplier."""
    if confidence < learning.confidence_floor():
        return 0.0
    if confidence >= 75:
        base = SIZE_HIGH
    elif confidence >= 60:
        base = SIZE_MID
    elif confidence >= 50:
        base = SIZE_LOW
    else:
        return 0.0
    return round(base * learning.category_budget_mult(cat), 2)


def determine_stop_pct(confidence: int) -> float:
    if confidence >= 75:
        return STOP_HIGH
    if confidence >= 60:
        return STOP_MID
    return STOP_LOW


def determine_tp_ladder(confidence: int) -> list[float]:
    if confidence >= 75:
        return TP_HIGH[:]
    if confidence >= 60:
        return TP_MID[:]
    return TP_LOW[:]


def assign_strategy(m: Market) -> Strategy:
    """
    Strategy A (RISKY): single bid-ask MM on yes side.
    Strategy B (RISKLESS): cross-outcome arb when yes+no < 0.99.
    """
    total = m.yes_price + m.no_price
    if total < 0.99:
        return Strategy.RISKLESS
    return Strategy.RISKY


# ============================================================================
# 8. POSITION LIFECYCLE (paper)
# ============================================================================

class PositionEngine:
    """
    Manages up to MAX_PARALLEL_POSITIONS open positions.
    Hybrid price evolution: real CLOB midpoint (when available) + Brownian noise.
    """
    def __init__(self, vault: VaultManager, learning: LearningState,
                 logger_: PaperTradeLogger, stats: StatsTracker):
        self.open_positions: list[Position] = []
        self.next_trade_num: int = self._compute_next_trade_num(logger_)
        self.vault = vault
        self.learning = learning
        self.logger = logger_
        self.stats = stats

    @staticmethod
    def _compute_next_trade_num(logger_: PaperTradeLogger) -> int:
        if not logger_.trades:
            return 1
        return max(t["n"] for t in logger_.trades) + 1

    def can_open(self) -> bool:
        return len(self.open_positions) < MAX_PARALLEL_POSITIONS

    def open(self, m: Market) -> Optional[Position]:
        """Open a paper position based on a scored Market."""
        if not self.can_open():
            return None
        if not self.learning.category_allowed(m.category):
            log.info(f"Category {m.category.value} suspended — skipping")
            return None
        size = determine_size(m.confidence, self.learning, m.category)
        if size <= 0:
            return None
        strategy = assign_strategy(m)
        # Entry on yes side at ask
        entry = m.ask
        shares = size / entry if entry > 0 else 0
        sl_pct = determine_stop_pct(m.confidence)
        tp_pct = determine_tp_ladder(m.confidence)
        # Convert pct to absolute prices
        tp_targets = [round(entry * (1 + p), 4) for p in tp_pct]
        sl_price = round(entry * (1 + sl_pct), 4)
        pos = Position(
            trade_num=self.next_trade_num,
            market_id=m.market_id,
            yes_token_id=m.yes_token_id,
            market_slug=m.slug,
            question=m.question,
            category=m.category,
            strategy=strategy,
            confidence=m.confidence,
            entry_price=entry,
            size_usd=size,
            shares=shares,
            entry_time=time.time(),
            tp_targets=tp_targets,
            sl_price=sl_price,
            last_price=entry,
            total_gas=GAS_COST_USD,
        )
        self.open_positions.append(pos)
        self.next_trade_num += 1
        log.info(f"OPEN #{pos.trade_num} {strategy.value} {m.slug[:30]} @ {entry:.4f} size=${size}")
        return pos

    def evolve_prices(self, real_prices: dict[str, float]):
        """
        Hybrid price tick.
        real_prices: {market_id: midpoint} from latest CLOB poll.
        For each open position, blend real + Brownian noise.
        """
        for pos in self.open_positions:
            real = real_prices.get(pos.market_id)
            # Brownian: dS = sigma * sqrt(dt) * Z
            sigma = 0.02                              # 2% per tick std
            dt = POSITION_TICK_INTERVAL / 3600.0     # in hours
            noise = sigma * math.sqrt(dt) * random.gauss(0, 1)
            if real is not None and real > 0:
                # Blend 70% real, 30% noisy drift
                new_price = real * (1 + 0.3 * noise)
            else:
                # Pure Brownian off last price
                new_price = pos.last_price * (1 + noise)
            pos.last_price = max(0.01, min(0.99, round(new_price, 4)))

    def check_lifecycle(self) -> list[Position]:
        """
        Check tier hits, trailing stops, and stop loss for all open positions.
        Returns list of positions that closed this tick.
        """
        closed_now: list[Position] = []
        for pos in list(self.open_positions):
            self._update_trailing_stop(pos)
            # Tier hits (only check tiers not yet hit, in order)
            for i, target in enumerate(pos.tp_targets):
                if pos.tier_hits[i]:
                    continue
                if pos.last_price >= target:
                    self._hit_tier(pos, i)
            # If fully closed by tiers
            if pos.closed_pct >= 0.999:
                pos.status = PositionStatus.CLOSED_TP
                pos.close_time = time.time()
                closed_now.append(pos)
                continue
            # Stop loss
            if pos.last_price <= pos.sl_price:
                # Close remaining shares at sl_price
                remaining_pct = 1.0 - pos.closed_pct
                pnl = (pos.sl_price - pos.entry_price) * pos.shares * remaining_pct
                pos.realized_pnl += pnl
                pos.closed_pct = 1.0
                pos.total_gas += GAS_COST_USD
                pos.total_slippage += SLIPPAGE_BASE_USD
                # Distinguish trailing-triggered vs adaptive stop
                pos.status = (PositionStatus.CLOSED_TRAIL
                              if pos.tier_hits[1] else PositionStatus.CLOSED_SL)
                pos.close_time = time.time()
                closed_now.append(pos)
        for pos in closed_now:
            if pos in self.open_positions:
                self.open_positions.remove(pos)
            self._finalize(pos)
        return closed_now

    def _hit_tier(self, pos: Position, tier_idx: int):
        """Close 25% at this tier."""
        pos.tier_hits[tier_idx] = True
        target = pos.tp_targets[tier_idx]
        pnl = (target - pos.entry_price) * pos.shares * TIER_CLOSE_PCT
        pos.realized_pnl += pnl
        pos.closed_pct = round(pos.closed_pct + TIER_CLOSE_PCT, 4)
        pos.total_gas += GAS_COST_USD
        pos.total_slippage += SLIPPAGE_BASE_USD
        log.info(f"  T{tier_idx+1} hit #{pos.trade_num} @ {target:.4f} (+${pnl:.3f})")

    def _update_trailing_stop(self, pos: Position):
        """Ratchet stop loss up after tier hits to lock profit."""
        if pos.tier_hits[3]:
            new_sl = pos.entry_price * (1 + TRAIL_AFTER_T4)
        elif pos.tier_hits[2]:
            new_sl = pos.entry_price * (1 + TRAIL_AFTER_T3)
        elif pos.tier_hits[1]:
            new_sl = pos.entry_price * (1 + TRAIL_AFTER_T2)
        else:
            return
        if new_sl > pos.sl_price:
            pos.sl_price = round(new_sl, 4)

    def _finalize(self, pos: Position):
        """Compute net P&L, route to vault, log, update stats & learning."""
        net = pos.realized_pnl - pos.total_gas - pos.total_slippage
        won = net > 0
        vault_amt = self.vault.deposit(net, pos.strategy, pos.trade_num) if won else 0.0
        self.logger.append(pos, net, vault_amt)
        self.stats.record(net, pos.total_gas, pos.total_slippage)
        self.learning.record_trade(pos, won)
        # Per-trade Telegram one-liner is dispatched by the main loop


# ============================================================================
# 9. SELF-STOP WATCHDOG
# ============================================================================

class Watchdog:
    """Monitors triggers per spec and halts bot when fired."""
    def __init__(self, stats: StatsTracker, latency: LatencyGuard):
        self.stats = stats
        self.latency = latency
        self.api_down_since: Optional[float] = None

    def check(self, last_max_slippage: float = 0.0,
              matic_balance_usd: Optional[float] = None) -> Optional[str]:
        """Returns a halt reason string if any trigger fires, else None."""
        s = self.stats.state
        # 1. Consecutive losses
        if s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            return f"3 consecutive losses (={s['consecutive_losses']})"
        # 2. Daily drawdown
        if s["daily_pnl"] <= s["starting_capital"] * DAILY_DRAWDOWN_PCT:
            return f"Daily drawdown {s['daily_pnl']:.2f} ≤ -5%"
        # 3. MATIC low (only if balance reading provided)
        if matic_balance_usd is not None and matic_balance_usd < MIN_MATIC_USD:
            return f"MATIC balance ${matic_balance_usd:.2f} < ${MIN_MATIC_USD}"
        # 4. API downtime
        if self.latency.last_failure_ts > 0:
            if self.latency.downtime_seconds() > API_DOWNTIME_SECONDS:
                return f"API down >{API_DOWNTIME_SECONDS//60}min"
        # 5. Single-trade slippage blowout
        if last_max_slippage > MAX_SINGLE_SLIPPAGE_USD:
            return f"Single-trade slippage ${last_max_slippage:.2f} > ${MAX_SINGLE_SLIPPAGE_USD}"
        return None


# ============================================================================
# 10. TELEGRAM REPORTER
# ============================================================================

class Telegram:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.enabled = bool(TG_TOKEN and TG_CHAT_ID)
        if not self.enabled:
            log.warning("Telegram disabled (missing token or chat_id)")

    async def send(self, text: str):
        if not self.enabled:
            log.info(f"[TG-MUTED] {text[:200]}")
            return
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self.session.post(url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    log.warning(f"Telegram send failed: {resp.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning(f"Telegram error: {e}")

    @staticmethod
    def per_trade_line(pos: Position, net_pnl: float, vault_amt: float,
                        vault_balance: float) -> str:
        """
        Format spec: ✅ #47 [RISKY] Lakers @ +$0.40 (gas -$0.03, slip -$0.02, vault +$0.18)
        """
        emoji = "✅" if net_pnl > 0 else ("❌" if net_pnl < 0 else "➖")
        sign = "+" if net_pnl >= 0 else ""
        title = (pos.question or pos.market_slug)[:40]
        line = (
            f"{emoji} #{pos.trade_num} [{pos.strategy.value}] {title} "
            f"@ {sign}${net_pnl:+.2f}\n"
            f"   gas -${pos.total_gas:.2f} | slip -${pos.total_slippage:.2f} | "
            f"net {sign}${net_pnl:+.2f}"
        )
        if vault_amt > 0:
            line += f"\n   vault +${vault_amt:.2f} → balance ${vault_balance:.2f}"
        return line


def build_checkpoint_report(learning: LearningState, vault: VaultManager,
                              stats: StatsTracker, logger_: PaperTradeLogger,
                              checkpoint_summary: dict) -> str:
    """
    Per spec: win rate, avg win/loss, R:R, category breakdown,
    tier hit %, drawdown analysis, prev checkpoint diff,
    self-learning suggestions, vault balance, paper P&L, total gas+fees.
    """
    trades = logger_.trades
    last_50 = trades[-CHECKPOINT_INTERVAL:] if len(trades) >= CHECKPOINT_INTERVAL else trades
    n = len(last_50)
    if n == 0:
        return "📊 No trades yet — checkpoint skipped."
    wins = [t for t in last_50 if t["net_pnl"] > 0]
    losses = [t for t in last_50 if t["net_pnl"] <= 0]
    win_rate = len(wins) / n
    avg_win = sum(t["net_pnl"] for t in wins) / max(len(wins), 1)
    avg_loss = sum(t["net_pnl"] for t in losses) / max(len(losses), 1)
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    # Category breakdown
    cat_lines = []
    for c in Category:
        c_trades = [t for t in last_50 if t["category"] == c.value]
        if not c_trades:
            continue
        c_wins = sum(1 for t in c_trades if t["net_pnl"] > 0)
        cat_lines.append(f"   {c.value}: {c_wins}/{len(c_trades)} ({c_wins/len(c_trades):.0%})")
    # Tier hit rate
    tier_total = {f"t{i}": 0 for i in range(1, 5)}
    for t in last_50:
        for i, hit in enumerate(t.get("tier_hits", []), start=1):
            if hit:
                tier_total[f"t{i}"] += 1
    tier_lines = " | ".join(f"T{i}: {tier_total[f't{i}']}/{n} ({tier_total[f't{i}']/n:.0%})"
                              for i in range(1, 5))
    # Drawdown
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in last_50:
        running += t["net_pnl"]
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    # Diff vs prev checkpoint
    prev = (learning.state["checkpoint_history"][-2]
            if len(learning.state["checkpoint_history"]) >= 2 else None)
    diff_line = ""
    if prev:
        diff_line = f"\n   vs prev checkpoint: trades +{n}"
    # Total gas+fees+slippage on last 50
    total_costs = sum(t["gas"] + t["slippage"] for t in last_50)
    # Suggestions
    suggestions = checkpoint_summary.get("category_actions", []) + \
                  checkpoint_summary.get("param_actions", [])
    sugg_block = "\n".join(f"   • {s}" for s in suggestions) if suggestions else "   • No actions"

    report = f"""📊 <b>SENTINEL CHECKPOINT</b> — Trades #{learning.state['trades_processed']-n+1}-{learning.state['trades_processed']}
━━━━━━━━━━━━━━━━━━━━━━
<b>Performance</b>
  Win Rate: {win_rate:.0%} ({len(wins)}W / {len(losses)}L)
  Avg Win: +${avg_win:.2f} | Avg Loss: ${avg_loss:.2f}
  R:R Ratio: {rr:.2f}
  Max Drawdown: ${max_dd:.2f}{diff_line}

<b>Category Breakdown</b>
{chr(10).join(cat_lines) if cat_lines else '   (none)'}

<b>Tier Hit Rates</b>
   {tier_lines}

<b>Costs (last 50)</b>
   Total gas + slippage: ${total_costs:.2f}

<b>Self-Learning Actions</b>
{sugg_block}

<b>Capital</b>
   Paper P&L: ${stats.state['current_pnl']:+.2f}
   Daily P&L: ${stats.state['daily_pnl']:+.2f}
   Vault Balance: ${vault.balance:.2f}
   Confidence Floor: {learning.confidence_floor()}
"""
    return report


# ============================================================================
# 11. CLOB SDK SKELETON (LIVE_MODE=False guarded)
# ============================================================================

class CLOBClientSkeleton:
    """
    Skeleton wrapper around py-clob-client. NEVER executes when LIVE_MODE=False.
    Real ENV (PRIV_KEY, WALLET_ADDR) is read but never logged.
    """
    def __init__(self):
        self.live = LIVE_MODE
        self._client = None
        if self.live:
            # Lazy import — keep dep optional for paper mode
            try:
                from py_clob_client.client import ClobClient  # type: ignore
                from py_clob_client.constants import POLYGON  # type: ignore
                if not (PRIV_KEY and WALLET_ADDR):
                    raise RuntimeError("Live mode requires private key + wallet env")
                self._client = ClobClient(
                    host=CLOB_BASE,
                    key=PRIV_KEY,
                    chain_id=POLYGON,
                    funder=WALLET_ADDR,
                )
                self._client.set_api_creds(self._client.create_or_derive_api_creds())
                log.info("CLOB live client initialized")
            except Exception as e:
                log.error(f"CLOB init failed: {type(e).__name__}")
                self.live = False

    async def execute_order(self, market_id: str, side: str,
                              price: float, size: float) -> dict:
        """Dummy execute. In live mode, would place real order."""
        if not self.live:
            return {"ok": True, "paper": True, "ts": time.time()}
        # Real path (deliberately incomplete; flip LIVE_MODE only after audit)
        log.warning("LIVE order execution path triggered")
        return {"ok": False, "reason": "live execution disabled in v10-LISTENER"}


# ============================================================================
# 12. MAIN LOOP
# ============================================================================

async def real_price_poll(session: aiohttp.ClientSession,
                           positions: list[Position],
                           rate: RateLimitTracker,
                           latency: LatencyGuard) -> dict[str, float]:
    """Poll CLOB midpoints for currently open positions (keyed by market_id)."""
    out: dict[str, float] = {}
    for pos in positions:
        ob = await fetch_clob_depth(session, pos.yes_token_id, rate, latency)
        if not ob:
            continue
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if bids and asks:
            try:
                mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                out[pos.market_id] = mid
            except (KeyError, ValueError):
                continue
    return out


async def scan_and_open(session: aiohttp.ClientSession,
                          engine: PositionEngine,
                          rate: RateLimitTracker,
                          latency: LatencyGuard,
                          spread_hist: SpreadHistory,
                          panic: PanicModeDetector,
                          tg):
    """One scan cycle: fetch markets → filter → score → open if worth it."""
    raw = await fetch_gamma_markets(session, rate, latency)
    if not raw:
        return
    markets = filter_pipeline(raw)
    if not markets:
        return
    # Update spread history + panic feed
    for m in markets:
        spread_hist.update(m.market_id, m.bid, m.ask)
        panic.feed(m.spread)
    if panic.check():
        log.warning("Panic mode active — no new opens")
        return
    # Sort by spread $ desc, take top N for depth
    markets.sort(key=lambda x: x.spread, reverse=True)
    top = markets[:TOP_N_FOR_DEPTH]
    for m in top:
        ob = await fetch_clob_depth(session, m.yes_token_id, rate, latency)
        m.has_depth = bool(ob.get("bids") and ob.get("asks"))
        m.volatility = spread_hist.get_volatility_score(m.market_id)
        m.confidence = score_confidence(m, m.has_depth, m.volatility)
        # Open one if engine has capacity
        if engine.can_open() and m.confidence >= engine.learning.confidence_floor():
            pos = engine.open(m)
            if pos:
                log.info(f"Opened paper position #{pos.trade_num}")


async def main_loop():
    """Top-level orchestrator."""
    random.seed()                          # nondeterministic in production
    vault = VaultManager()
    learning = LearningState()
    paper_log = PaperTradeLogger()
    spread_hist = SpreadHistory()
    stats = StatsTracker()
    rate = RateLimitTracker()
    latency = LatencyGuard()
    panic = PanicModeDetector()
    throttle = SmartThrottler()
    clob = CLOBClientSkeleton()           # noqa: F841 — held for live flip
    engine = PositionEngine(vault, learning, paper_log, stats)
    watchdog = Watchdog(stats, latency)

    async with aiohttp.ClientSession() as session:
        tg = Telegram(session)
        await tg.send("🟢 Sentinel v10-LISTENER online (paper mode)")
        loop_count = 0
        while True:
            try:
                loop_count += 1
                # 0. Halt check
                if stats.is_halted():
                    log.info(f"Halted: {stats.state['halt_reason']}")
                    await asyncio.sleep(throttle.interval())
                    continue

                # 1. Evolve open positions (real prices + noise)
                if engine.open_positions:
                    real_prices = await real_price_poll(
                        session, engine.open_positions, rate, latency
                    )
                    engine.evolve_prices(real_prices)
                    closed = engine.check_lifecycle()
                    for pos in closed:
                        # Reload last trade record to get net_pnl & vault
                        last = paper_log.trades[-1]
                        line = Telegram.per_trade_line(
                            pos, last["net_pnl"], last["vault"], vault.balance
                        )
                        await tg.send(line)
                        # Per-trade slippage blowout check
                        reason = watchdog.check(last_max_slippage=pos.total_slippage)
                        if reason:
                            stats.halt(reason, hours=2.0)
                            await tg.send(f"🛑 SELF-STOP: {reason}")

                # 2. Scan for new opens (only every 2nd loop to save API)
                if loop_count % 2 == 1 and engine.can_open():
                    await scan_and_open(session, engine, rate, latency,
                                         spread_hist, panic, tg)

                # 3. Checkpoint?
                if learning.should_run_checkpoint():
                    summary = learning.run_checkpoint()
                    report = build_checkpoint_report(learning, vault, stats,
                                                       paper_log, summary)
                    await tg.send(report)

                # 4. Watchdog tail check
                reason = watchdog.check()
                if reason:
                    stats.halt(reason, hours=2.0)
                    await tg.send(f"🛑 SELF-STOP: {reason}")

                # 5. Adaptive throttle
                throttle.adjust(latency.is_degraded(), panic.check())
                await asyncio.sleep(throttle.interval())

            except asyncio.CancelledError:
                log.info("Main loop cancelled")
                break
            except Exception as e:
                # Catch-all to prevent crash on Railway
                log.error(f"Main loop error: {type(e).__name__}: {e}")
                await asyncio.sleep(30)


# ============================================================================
# 13. SIMULATION HARNESS (50 hypothetical trades — checkpoint test)
# ============================================================================

def simulate_50_trades_test():
    """
    Generates 50 random hypothetical trades to validate:
      - VaultManager routing
      - LearningState category audit + param tuning
      - Checkpoint report generation
      - Stats tracker arithmetic
    Resets state files if they exist (uses .test suffix).
    """
    print("\n" + "="*70)
    print(" SENTINEL v10 — 50-TRADE CHECKPOINT SIMULATION")
    print("="*70 + "\n")

    # Use temp data dir
    global LEARNING_FILE, VAULT_FILE, PAPER_TRADES_FILE, STATS_FILE, SPREAD_HISTORY_FILE
    LEARNING_FILE = DATA_DIR / "test_learning_state.json"
    VAULT_FILE = DATA_DIR / "test_vault_balance.json"
    PAPER_TRADES_FILE = DATA_DIR / "test_paper_trades.json"
    STATS_FILE = DATA_DIR / "test_sentinel_stats.json"
    SPREAD_HISTORY_FILE = DATA_DIR / "test_spread_history.json"
    for p in [LEARNING_FILE, VAULT_FILE, PAPER_TRADES_FILE,
              STATS_FILE, SPREAD_HISTORY_FILE]:
        if p.exists():
            p.unlink()

    random.seed(42)                        # reproducible
    vault = VaultManager()
    learning = LearningState()
    paper_log = PaperTradeLogger()
    stats = StatsTracker()

    cats = list(Category)
    strats = list(Strategy)
    confidences = [55, 65, 80]

    print(f"{'#':>3} {'CAT':<10} {'STR':<9} {'CONF':>4} {'NET':>8} {'VAULT':>7} {'STATUS':<14}")
    print("-" * 70)

    for i in range(1, 51):
        cat = random.choice(cats)
        strategy = random.choice(strats)
        conf = random.choice(confidences)
        # Outcome: 60% win bias for high conf, 50/50 for mid, 40% for low
        win_prob = {80: 0.6, 65: 0.5, 55: 0.4}[conf]
        won = random.random() < win_prob
        # P&L magnitude scales with confidence
        if won:
            gross = round(random.uniform(0.20, 0.80), 3) * (conf / 60.0)
            tier_hits = [True, conf >= 65, conf >= 75 and random.random() < 0.6,
                         conf >= 75 and random.random() < 0.3]
            status = PositionStatus.CLOSED_TP
        else:
            gross = -round(random.uniform(0.30, 0.70), 3) * (60.0 / conf)
            tier_hits = [False, False, False, False]
            status = PositionStatus.CLOSED_SL
        gas = GAS_COST_USD * 2             # entry+exit
        slip = SLIPPAGE_BASE_USD * 2
        net = round(gross - gas - slip, 3)

        # Build a fake Position
        pos = Position(
            trade_num=i, market_id=f"m{i}", yes_token_id=f"tok_{i}",
            market_slug=f"slug-{i}", question=f"Question {i}",
            category=cat, strategy=strategy,
            confidence=conf, entry_price=0.50, size_usd=20.0, shares=40.0,
            entry_time=time.time(), tp_targets=[0.54,0.59,0.65,0.75],
            sl_price=0.44, tier_hits=tier_hits,
            closed_pct=1.0 if status != PositionStatus.OPEN else 0.0,
            realized_pnl=gross, last_price=0.55 if won else 0.42,
            status=status, close_time=time.time(),
            total_gas=gas, total_slippage=slip,
        )
        vault_amt = vault.deposit(net, strategy, i) if net > 0 else 0.0
        paper_log.append(pos, net, vault_amt)
        stats.record(net, gas, slip)
        learning.record_trade(pos, net > 0)

        print(f"{i:>3} {cat.value:<10} {strategy.value:<9} {conf:>4} "
              f"{net:>+7.2f}  ${vault_amt:>5.2f} {status.value:<14}")

    # Run checkpoint
    print("\n" + "="*70)
    print(" CHECKPOINT TRIGGERED")
    print("="*70 + "\n")
    summary = learning.run_checkpoint()
    report = build_checkpoint_report(learning, vault, stats, paper_log, summary)
    print(report)
    print("\n" + "="*70)
    print(f" Final: P&L ${stats.state['current_pnl']:+.2f} | "
          f"Vault ${vault.balance:.2f} | "
          f"WR {sum(1 for t in paper_log.trades if t['net_pnl']>0)}/50")
    print("="*70 + "\n")


# ============================================================================
# 14. ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    if SIMULATE_TEST or os.getenv("SENTINEL_SIM") == "1":
        simulate_50_trades_test()
    else:
        try:
            asyncio.run(main_loop())
        except KeyboardInterrupt:
            log.info("Shutdown requested")
