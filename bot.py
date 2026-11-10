import os
import json
import time
import math
import logging
from datetime import datetime, timezone
from typing import Optional

import requests


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://sapi.xt.com"

STATE_FILE = "paper_state.json"

INITIAL_BALANCE = 1000.0

MAX_OPEN_POSITIONS = 4
MAX_POSITION_USDT = 100.0

RISK_PER_TRADE = 0.01       # 1% of equity
MAX_DAILY_LOSS = 0.03       # 3% of starting daily equity

FEE_RATE = 0.001            # simulated 0.1%
SLIPPAGE = 0.0005           # simulated 0.05%

SCAN_INTERVAL = 300         # scan new signals every 5 minutes
MANAGE_INTERVAL = 15         # check open positions every 15 seconds

MAX_SYMBOLS = 35
MIN_24H_QUOTE_VOLUME = 500_000

TIMEFRAME_ENTRY = "15m"
TIMEFRAME_TREND = "1h"

CANDLE_LIMIT = 150

MIN_RR = 2.0
ATR_STOP_BUFFER = 0.25

# SMC filters
PIVOT_LEFT = 2
PIVOT_RIGHT = 2

MIN_DISPLACEMENT_ATR = 1.0
MAX_ENTRY_DISTANCE_FROM_ZONE = 0.003

# RSI filter
RSI_MIN = 48
RSI_MAX = 70

# EMA trend filter
EMA_FAST = 20
EMA_SLOW = 50

# Safety
REQUEST_TIMEOUT = 15
HTTP_RETRIES = 3

# Only USDT spot pairs
QUOTE_ASSET = "usdt"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

log = logging.getLogger("XT-SMC-PAPER")


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "XT-SMC-PaperBot/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json"
})


def api_get(path, params=None):
    url = BASE_URL + path

    last_error = None

    for attempt in range(HTTP_RETRIES):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            response.raise_for_status()

            data = response.json()

            if not isinstance(data, dict):
                raise ValueError("Unexpected API response format")

            rc = data.get("rc")

            if rc not in (None, 0, "0"):
                raise RuntimeError(
                    f"XT API error: rc={rc}, mc={data.get('mc')}"
                )

            return data

        except Exception as exc:
            last_error = exc

            wait_time = 1 + attempt * 2

            log.warning(
                "API request failed (%s). Retry in %s sec",
                exc,
                wait_time
            )

            time.sleep(wait_time)

    raise RuntimeError(f"API request failed: {last_error}")


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def today_utc():
    return now_utc().strftime("%Y-%m-%d")


def safe_float(value, default=0.0):
    try:
        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    return sum(true_ranges[-period:]) / period


def candle_body(candle):
    return abs(candle["close"] - candle["open"])


def is_bullish(candle):
    return candle["close"] > candle["open"]


def is_bearish(candle):
    return candle["close"] < candle["open"]


# ============================================================
# XT MARKET DATA
# ============================================================

def unwrap_result(data):
    result = data.get("result")

    if result is None:
        return []

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        for key in ("items", "list", "data", "rows"):
            value = result.get(key)

            if isinstance(value, list):
                return value

        return [result]

    return []


def get_all_tickers():
    """
    Public market-data endpoint only.
    No API key and no private trading endpoint.
    """

    data = api_get("/v4/public/ticker/24h")

    return unwrap_result(data)


def parse_ticker(item):
    if not isinstance(item, dict):
        return None

    symbol = str(
        item.get("s")
        or item.get("symbol")
        or ""
    ).lower()

    if not symbol.endswith("_usdt"):
        return None

    price = safe_float(
        item.get("c")
        or item.get("close")
        or item.get("last")
        or item.get("lastPrice")
    )

    if price <= 0:
        return None

    quote_volume = safe_float(
        item.get("q")
        or item.get("quoteVolume")
        or item.get("turnover")
        or item.get("amount")
    )

    base_volume = safe_float(
        item.get("v")
        or item.get("volume")
        or item.get("baseVolume")
    )

    # Different XT responses may use different volume names.
    # If quote turnover is absent, estimate it from base volume.
    if quote_volume <= 0 and base_volume > 0:
        quote_volume = base_volume * price

    return {
        "symbol": symbol,
        "price": price,
        "quote_volume": quote_volume
    }


def get_market_universe():
    raw_tickers = get_all_tickers()

    parsed = []

    for item in raw_tickers:
        ticker = parse_ticker(item)

        if ticker is None:
            continue

        if ticker["quote_volume"] < MIN_24H_QUOTE_VOLUME:
            continue

        parsed.append(ticker)

    parsed.sort(
        key=lambda x: x["quote_volume"],
        reverse=True
    )

    return parsed[:MAX_SYMBOLS]


def normalize_candle(item):
    """
    Supports the documented object-style kline response
    and common array-style responses.
    """

    if isinstance(item, dict):
        timestamp = (
            item.get("t")
            or item.get("time")
            or item.get("timestamp")
        )

        open_price = (
            item.get("o")
            or item.get("open")
        )

        high_price = (
            item.get("h")
            or item.get("high")
        )

        low_price = (
            item.get("l")
            or item.get("low")
        )

        close_price = (
            item.get("c")
            or item.get("close")
        )

        volume = (
            item.get("q")
            or item.get("volume")
            or item.get("v")
            or 0
        )

    elif isinstance(item, (list, tuple)) and len(item) >= 6:
        timestamp = item[0]
        open_price = item[1]
        high_price = item[2]
        low_price = item[3]
        close_price = item[4]
        volume = item[5]

    else:
        return None

    timestamp = safe_float(timestamp)

    if timestamp <= 0:
        return None

    # XT timestamps are expected in milliseconds.
    if timestamp < 10_000_000_000:
        timestamp *= 1000

    candle = {
        "time": int(timestamp),
        "open": safe_float(open_price),
        "high": safe_float(high_price),
        "low": safe_float(low_price),
        "close": safe_float(close_price),
        "volume": safe_float(volume)
    }

    if min(
        candle["open"],
        candle["high"],
        candle["low"],
        candle["close"]
    ) <= 0:
        return None

    if candle["high"] < candle["low"]:
        return None

    return candle


def interval_milliseconds(interval):
    mapping = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000
    }

    if interval not in mapping:
        raise ValueError(f"Unsupported interval: {interval}")

    return mapping[interval]


def get_candles(symbol, interval, limit=CANDLE_LIMIT):
    data = api_get(
        "/v4/public/kline",
        params={
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )

    raw = unwrap_result(data)

    candles = []

    for item in raw:
        candle = normalize_candle(item)

        if candle is not None:
            candles.append(candle)

    candles.sort(key=lambda x: x["time"])

    # Ignore the currently forming candle.
    close_time = int(time.time() * 1000)

    duration = interval_milliseconds(interval)

    candles = [
        candle
        for candle in candles
        if candle["time"] + duration <= close_time
    ]

    # Remove duplicate timestamps.
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = list(unique.values())

    candles.sort(key=lambda x: x["time"])

    return candles[-limit:]


# ============================================================
# PIVOT / MARKET STRUCTURE
# ============================================================

def find_pivots(candles, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    highs = []
    lows = []

    for i in range(left, len(candles) - right):
        current = candles[i]

        left_slice = candles[i - left:i]
        right_slice = candles[i + 1:i + right + 1]

        is_high = all(
            current["high"] > x["high"]
            for x in left_slice + right_slice
        )

        is_low = all(
            current["low"] < x["low"]
            for x in left_slice + right_slice
        )

        if is_high:
            highs.append({
                "index": i,
                "price": current["high"]
            })

        if is_low:
            lows.append({
                "index": i,
                "price": current["low"]
            })

    return highs, lows


def find_bullish_sweep(candles, pivot_lows):
    """
    A bullish liquidity sweep:
    - Price trades below a previously confirmed swing low.
    - Candle closes back above that swing low.
    - Sweep candle must be reasonably recent.
    """

    if len(candles) < 20 or not pivot_lows:
        return None

    start = max(5, len(candles) - 15)

    for i in range(len(candles) - 1, start - 1, -1):
        candle = candles[i]

        previous_lows = [
            p for p in pivot_lows
            if p["index"] < i - 1
        ]

        if not previous_lows:
            continue

        # Use recent confirmed liquidity levels.
        candidates = previous_lows[-8:]

        for pivot in reversed(candidates):
            level = pivot["price"]

            swept = (
                candle["low"] < level
                and candle["close"] > level
            )

            if swept:
                return {
                    "index": i,
                    "level": level,
                    "low": candle["low"]
                }

    return None


def find_bullish_bos(candles, pivot_highs, sweep):
    """
    Bullish BOS must occur after the sweep and close above
    a confirmed swing high that existed before the break.
    """

    if sweep is None:
        return None

    sweep_index = sweep["index"]

    candidates = [
        p for p in pivot_highs
        if p["index"] < sweep_index
    ]

    if not candidates:
        return None

    # The most recent pre-sweep swing high is the reference.
    reference = candidates[-1]

    for i in range(sweep_index + 1, len(candles)):
        candle = candles[i]

        if candle["close"] > reference["price"]:
            return {
                "index": i,
                "level": reference["price"],
                "close": candle["close"]
            }

    return None


def detect_bearish_structure_before(candles, pivot_highs, pivot_lows, index):
    """
    Approximate bearish structure:
    recent lower high + lower low.
    This is used only as a CHoCH context filter.
    """

    highs = [
        p for p in pivot_highs
        if p["index"] < index
    ]

    lows = [
        p for p in pivot_lows
        if p["index"] < index
    ]

    if len(highs) < 2 or len(lows) < 2:
        return False

    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]

    lower_high = h2["price"] < h1["price"]
    lower_low = l2["price"] < l1["price"]

    return lower_high and lower_low


def detect_choch(candles, pivot_highs, pivot_lows, bos):
    """
    Approximate bullish CHoCH:
    bearish structure existed before the bullish break.
    """

    if bos is None:
        return False

    return detect_bearish_structure_before(
        candles,
        pivot_highs,
        pivot_lows,
        bos["index"]
    )


# ============================================================
# FVG / ORDER BLOCK / DISPLACEMENT
# ============================================================

def find_bullish_fvg(candles, sweep, bos, atr):
    """
    Bullish FVG:
    low of candle 3 > high of candle 1.

    The middle candle must show meaningful bullish
    displacement. The formation must be after the sweep
    and no later than the BOS candle.
    """

    if sweep is None or bos is None or atr is None:
        return None

    start = max(2, sweep["index"])
    end = min(bos["index"], len(candles) - 1)

    candidates = []

    for i in range(start, end + 1):
        first = candles[i - 2]
        middle = candles[i - 1]
        third = candles[i]

        gap_low = first["high"]
        gap_high = third["low"]

        gap_size = gap_high - gap_low

        if gap_size <= 0:
            continue

        displacement = (
            is_bullish(middle)
            and candle_body(middle) >= atr * MIN_DISPLACEMENT_ATR
        )

        if not displacement:
            continue

        # Close near the top of the displacement candle.
        candle_range = max(
            middle["high"] - middle["low"],
            1e-12
        )

        close_position = (
            middle["close"] - middle["low"]
        ) / candle_range

        if close_position < 0.70:
            continue

        candidates.append({
            "index": i,
            "low": gap_low,
            "high": gap_high,
            "mid": (gap_low + gap_high) / 2,
            "displacement": candle_body(middle) / atr
        })

    if not candidates:
        return None

    return candidates[-1]


def find_bullish_order_block(candles, sweep, bos, atr):
    """
    Approximate bullish OB:
    last bearish candle before the BOS displacement.
    The BOS must happen after that candle.
    """

    if sweep is None or bos is None or atr is None:
        return None

    bos_index = bos["index"]

    start = max(sweep["index"], bos_index - 12)

    for i in range(bos_index - 1, start - 1, -1):
        candle = candles[i]

        if not is_bearish(candle):
            continue

        # Search for a bullish impulse after this candle.
        future = candles[i + 1:bos_index + 1]

        if not future:
            continue

        strongest = max(
            future,
            key=lambda x: candle_body(x)
        )

        if (
            is_bullish(strongest)
            and candle_body(strongest) >= atr * MIN_DISPLACEMENT_ATR
        ):
            return {
                "index": i,
                "low": candle["low"],
                "high": candle["open"],
                "mid": (
                    candle["low"] + candle["open"]
                ) / 2
            }

    return None


# ============================================================
# SIGNAL ENGINE
# ============================================================

def trend_filter(candles):
    if len(candles) < EMA_SLOW + 5:
        return False

    closes = [c["close"] for c in candles]

    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)

    if fast is None or slow is None:
        return False

    last_close = closes[-1]

    return (
        fast > slow
        and last_close > fast
    )


def build_signal(symbol, candles_15m, candles_1h, current_price):
    if len(candles_15m) < 80:
        return None

    if len(candles_1h) < 80:
        return None

    if not trend_filter(candles_1h):
        return None

    closes = [c["close"] for c in candles_15m]

    rsi = calculate_rsi(closes)

    if rsi is None:
        return None

    if not (RSI_MIN <= rsi <= RSI_MAX):
        return None

    atr = calculate_atr(candles_15m)

    if atr is None or atr <= 0:
        return None

    pivot_highs, pivot_lows = find_pivots(candles_15m)

    sweep = find_bullish_sweep(
        candles_15m,
        pivot_lows
    )

    if sweep is None:
        return None

    bos = find_bullish_bos(
        candles_15m,
        pivot_highs,
        sweep
    )

    if bos is None:
        return None

    # Require an approximate bullish CHoCH context.
    choch = detect_choch(
        candles_15m,
        pivot_highs,
        pivot_lows,
        bos
    )

    if not choch:
        return None

    fvg = find_bullish_fvg(
        candles_15m,
        sweep,
        bos,
        atr
    )

    if fvg is None:
        return None

    order_block = find_bullish_order_block(
        candles_15m,
        sweep,
        bos,
        atr
    )

    if order_block is None:
        return None

    # Entry zone is the overlap of FVG and OB.
    zone_low = max(
        fvg["low"],
        order_block["low"]
    )

    zone_high = min(
        fvg["high"],
        order_block["high"]
    )

    if zone_low >= zone_high:
        return None

    # Do not chase price far above the zone.
    max_allowed_price = zone_high * (
        1 + MAX_ENTRY_DISTANCE_FROM_ZONE
    )

    if current_price > max_allowed_price:
        return None

    if current_price < zone_low * 0.99:
        return None

    # Simulated buy fill includes adverse slippage.
    entry = current_price * (1 + SLIPPAGE)

    # Structural stop below the sweep low and OB low.
    structural_low = min(
        sweep["low"],
        order_block["low"]
    )

    stop_loss = structural_low - ATR_STOP_BUFFER * atr

    if stop_loss <= 0 or stop_loss >= entry:
        return None

    risk_per_unit = entry - stop_loss

    if risk_per_unit <= 0:
        return None

    # First choose a target that guarantees at least 2R.
    target_2r = entry + risk_per_unit * MIN_RR

    # A nearby confirmed liquidity high may be used
    # only if it is at least 2R away.
    future_highs = [
        p["price"]
        for p in pivot_highs
        if p["index"] > bos["index"]
        and p["price"] >= target_2r
    ]

    if future_highs:
        target = min(future_highs)
    else:
        target = target_2r

    rr = (target - entry) / risk_per_unit

    if rr < MIN_RR:
        return None

    return {
        "symbol": symbol,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": target,
        "rr": rr,
        "rsi": rsi,
        "atr": atr,
        "sweep_level": sweep["level"],
        "bos_level": bos["level"],
        "fvg_low": fvg["low"],
        "fvg_high": fvg["high"],
        "ob_low": order_block["low"],
        "ob_high": order_block["high"],
        "created_at": now_utc().isoformat()
    }


# ============================================================
# STATE MANAGEMENT
# ============================================================

def new_state():
    return {
        "version": 2,
        "cash": INITIAL_BALANCE,
        "positions": {},
        "closed_trades": [],
        "daily": {
            "date": today_utc(),
            "starting_equity": INITIAL_BALANCE,
            "realized_pnl": 0.0
        },
        "last_signals": {},
        "created_at": now_utc().isoformat()
    }


def validate_state(state):
    required = (
        "cash",
        "positions",
        "closed_trades",
        "daily"
    )

    if not isinstance(state, dict):
        raise ValueError("State file is not a JSON object")

    for key in required:
        if key not in state:
            raise ValueError(f"State missing key: {key}")

    if not isinstance(state["positions"], dict):
        raise ValueError("Invalid positions data")

    if not isinstance(state["closed_trades"], list):
        raise ValueError("Invalid closed trades data")

    if not isinstance(state["daily"], dict):
        raise ValueError("Invalid daily data")

    cash = safe_float(state["cash"], -1)

    if cash < 0:
        raise ValueError("Invalid cash balance")

    for symbol, position in state["positions"].items():
        if not isinstance(position, dict):
            raise ValueError(f"Invalid position: {symbol}")

        for field in (
            "qty",
            "entry",
            "stop_loss",
            "take_profit",
            "entry_cost"
        ):
            if safe_float(position.get(field), -1) <= 0:
                raise ValueError(
                    f"Invalid {field} in position {symbol}"
                )

    return state


def save_state(state):
    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False
        )

        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_file, STATE_FILE)


def load_state():
    if not os.path.exists(STATE_FILE):
        state = new_state()
        save_state(state)
        return state

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        state = validate_state(state)

        # Fill optional fields for older compatible state files.
        state.setdefault("version", 2)
        state.setdefault("last_signals", {})

        state["daily"].setdefault(
            "date",
            today_utc()
        )

        state["daily"].setdefault(
            "starting_equity",
            INITIAL_BALANCE
        )

        state["daily"].setdefault(
            "realized_pnl",
            0.0
        )

        return state

    except Exception as exc:
        # Never overwrite a damaged state automatically.
        raise RuntimeError(
            f"Cannot safely load {STATE_FILE}: {exc}. "
            "Make a backup and inspect the file manually."
        )


def reset_daily_if_needed(state, current_equity):
    current_date = today_utc()

    if state["daily"].get("date") != current_date:
        state["daily"] = {
            "date": current_date,
            "starting_equity": current_equity,
            "realized_pnl": 0.0
        }

        log.info(
            "New UTC day. Daily risk counter reset. "
            "Starting equity: %.2f USDT",
            current_equity
        )


# ============================================================
# PORTFOLIO / RISK
# ============================================================

def calculate_equity(state, prices):
    equity = safe_float(state["cash"])

    for symbol, position in state["positions"].items():
        mark = prices.get(symbol)

        if mark is None or mark <= 0:
            mark = position["entry"]

        qty = position["qty"]

        estimated_exit_value = (
            qty * mark * (1 - FEE_RATE)
        )

        equity += estimated_exit_value

    return equity


def calculate_unrealized_pnl(state, prices):
    total = 0.0

    for symbol, position in state["positions"].items():
        mark = prices.get(
            symbol,
            position["entry"]
        )

        estimated_exit_value = (
            position["qty"]
            * mark
            * (1 - FEE_RATE)
            * (1 - SLIPPAGE)
        )

        total += (
            estimated_exit_value
            - position["entry_cost"]
        )

    return total


def daily_total_pnl(state, prices):
    realized = safe_float(
        state["daily"].get("realized_pnl")
    )

    unrealized = calculate_unrealized_pnl(
        state,
        prices
    )

    return realized + unrealized


def daily_loss_limit_reached(state, prices):
    starting_equity = safe_float(
        state["daily"].get("starting_equity"),
        INITIAL_BALANCE
    )

    if starting_equity <= 0:
        return True

    pnl = daily_total_pnl(state, prices)

    return pnl <= -starting_equity * MAX_DAILY_LOSS


def calculate_position_size(state, signal, prices):
    equity = calculate_equity(state, prices)

    risk_budget = equity * RISK_PER_TRADE

    entry = signal["entry"]
    stop = signal["stop_loss"]

    risk_per_unit = entry - stop

    if risk_per_unit <= 0:
        return 0.0

    # Include estimated entry and exit fees in the
    # per-unit risk approximation.
    estimated_risk_per_unit = (
        risk_per_unit
        + entry * FEE_RATE
        + stop * FEE_RATE
    )

    qty_by_risk = risk_budget / estimated_risk_per_unit

    notional_by_risk = qty_by_risk * entry

    notional = min(
        MAX_POSITION_USDT,
        notional_by_risk,
        state["cash"] / (1 + FEE_RATE)
    )

    if notional <= 0:
        return 0.0

    return notional


# ============================================================
# PAPER EXECUTION
# ============================================================

def open_paper_position(state, signal, notional):
    symbol = signal["symbol"]

    if symbol in state["positions"]:
        return False

    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False

    entry = signal["entry"]

    if entry <= 0 or notional <= 0:
        return False

    qty = notional / entry

    entry_fee = notional * FEE_RATE

    total_cost = notional + entry_fee

    if total_cost > state["cash"]:
        return False

    state["cash"] -= total_cost

    state["positions"][symbol] = {
        "symbol": symbol,
        "qty": qty,
        "entry": entry,
        "entry_cost": total_cost,
        "entry_fee": entry_fee,
        "stop_loss": signal["stop_loss"],
        "take_profit": signal["take_profit"],
        "rr": signal["rr"],
        "opened_at": now_utc().isoformat(),
        "signal": signal
    }

    log.info(
        "PAPER BUY | %s | entry %.8f | qty %.8f | "
        "notional %.2f USDT | SL %.8f | TP %.8f | RR %.2f",
        symbol,
        entry,
        qty,
        notional,
        signal["stop_loss"],
        signal["take_profit"],
        signal["rr"]
    )

    return True


def close_paper_position(
    state,
    symbol,
    market_price,
    reason
):
    position = state["positions"].get(symbol)

    if position is None:
        return False

    if market_price <= 0:
        return False

    # A simulated market sell suffers adverse slippage.
    exit_price = market_price * (1 - SLIPPAGE)

    qty = position["qty"]

    gross_value = qty * exit_price

    exit_fee = gross_value * FEE_RATE

    net_proceeds = gross_value - exit_fee

    pnl = net_proceeds - position["entry_cost"]

    state["cash"] += net_proceeds

    trade = {
        "symbol": symbol,
        "entry": position["entry"],
        "exit": exit_price,
        "qty": qty,
        "entry_cost": position["entry_cost"],
        "exit_fee": exit_fee,
        "pnl": pnl,
        "reason": reason,
        "opened_at": position["opened_at"],
        "closed_at": now_utc().isoformat()
    }

    state["closed_trades"].append(trade)

    # Keep state file reasonably small.
    state["closed_trades"] = state["closed_trades"][-1000:]

    if state["daily"].get("date") == today_utc():
        state["daily"]["realized_pnl"] = (
            safe_float(
                state["daily"].get("realized_pnl")
            ) + pnl
        )

    del state["positions"][symbol]

    log.info(
        "PAPER SELL | %s | reason=%s | exit %.8f | "
        "PnL=%+.4f USDT | cash=%.2f",
        symbol,
        reason,
        exit_price,
        pnl,
        state["cash"]
    )

    return True


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def manage_positions(state, ticker_prices):
    if not state["positions"]:
        return

    for symbol in list(state["positions"].keys()):
        position = state["positions"].get(symbol)

        if position is None:
            continue

        current_price = ticker_prices.get(symbol)

        if current_price is None or current_price <= 0:
            continue

        stop = position["stop_loss"]
        target = position["take_profit"]

        # Long spot position.
        if current_price <= stop:
            close_paper_position(
                state,
                symbol,
                current_price,
                "STOP_LOSS"
            )

        elif current_price >= target:
            close_paper_position(
                state,
                symbol,
                target,
                "TAKE_PROFIT"
            )


# ============================================================
# SIGNAL SCANNING
# ============================================================

def scan_symbol(symbol, current_price):
    candles_15m = get_candles(
        symbol,
        TIMEFRAME_ENTRY,
        CANDLE_LIMIT
    )

    candles_1h = get_candles(
        symbol,
        TIMEFRAME_TREND,
        CANDLE_LIMIT
    )

    if not candles_15m or not candles_1h:
        return None

    return build_signal(
        symbol,
        candles_15m,
        candles_1h,
        current_price
    )


def scan_for_entries(state, market):
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return

    prices = {
        item["symbol"]: item["price"]
        for item in market
    }

    equity = calculate_equity(state, prices)

    reset_daily_if_needed(
        state,
        equity
    )

    if daily_loss_limit_reached(state, prices):
        log.warning(
            "Daily loss limit reached. No new positions today."
        )
        return

    for item in market:
        if len(state["positions"]) >= MAX_OPEN_POSITIONS:
            break

        symbol = item["symbol"]
        price = item["price"]

        if symbol in state["positions"]:
            continue

        # Avoid repeated processing of the same recent signal.
        last_signal = state["last_signals"].get(symbol)

        try:
            candles = get_candles(
                symbol,
                TIMEFRAME_ENTRY,
                CANDLE_LIMIT
            )

            if not candles:
                continue

            latest_candle_time = candles[-1]["time"]

            if last_signal == latest_candle_time:
                continue

            # Fetch the higher timeframe.
            candles_1h = get_candles(
                symbol,
                TIMEFRAME_TREND,
                CANDLE_LIMIT
            )

            signal = build_signal(
                symbol,
                candles,
                candles_1h,
                price
            )

            # Record the candle as processed even when no
            # signal is produced. A new closed candle is needed.
            state["last_signals"][symbol] = latest_candle_time

            if signal is None:
                continue

            if daily_loss_limit_reached(state, prices):
                log.warning(
                    "Daily loss limit reached during scan."
                )
                break

            notional = calculate_position_size(
                state,
                signal,
                prices
            )

            if notional <= 0:
                continue

            opened = open_paper_position(
                state,
                signal,
                notional
            )

            if opened:
                save_state(state)

        except Exception as exc:
            log.warning(
                "Scan failed for %s: %s",
                symbol,
                exc
            )

    save_state(state)


# ============================================================
# REPORTING
# ============================================================

def print_status(state, prices):
    equity = calculate_equity(
        state,
        prices
    )

    unrealized = calculate_unrealized_pnl(
        state,
        prices
    )

    realized = sum(
        safe_float(t.get("pnl"))
        for t in state["closed_trades"]
    )

    today_realized = safe_float(
        state["daily"].get("realized_pnl")
    )

    daily_pnl = daily_total_pnl(
        state,
        prices
    )

    wins = sum(
        1 for t in state["closed_trades"]
        if safe_float(t.get("pnl")) > 0
    )

    losses = sum(
        1 for t in state["closed_trades"]
        if safe_float(t.get("pnl")) <= 0
    )

    total_closed = wins + losses

    win_rate = (
        wins / total_closed * 100
        if total_closed
        else 0
    )

    log.info(
        "ACCOUNT | cash=%.2f | equity=%.2f | "
        "unrealized=%+.2f | all-time realized=%+.2f",
        state["cash"],
        equity,
        unrealized,
        realized
    )

    log.info(
        "TODAY | realized=%+.2f | total PnL=%+.2f | "
        "daily limit=%.2f%%",
        today_realized,
        daily_pnl,
        MAX_DAILY_LOSS * 100
    )

    log.info(
        "POSITIONS=%s/%s | closed=%s | wins=%s | "
        "losses=%s | win rate=%.1f%%",
        len(state["positions"]),
        MAX_OPEN_POSITIONS,
        total_closed,
        wins,
        losses,
        win_rate
    )

    for symbol, position in state["positions"].items():
        mark = prices.get(
            symbol,
            position["entry"]
        )

        log.info(
            "OPEN | %s | entry=%.8f | mark=%.8f | "
            "SL=%.8f | TP=%.8f",
            symbol,
            position["entry"],
            mark,
            position["stop_loss"],
            position["take_profit"]
        )


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    log.info("=" * 65)
    log.info("XT SMC / ICT PAPER TRADING BOT")
    log.info("MODE: PAPER ONLY - NO REAL ORDERS")
    log.info("=" * 65)

    state = load_state()

    log.info(
        "Loaded state | cash=%.2f | open positions=%s",
        state["cash"],
        len(state["positions"])
    )

    last_scan_time = 0
    last_status_time = 0

    while True:
        try:
            raw_tickers = get_all_tickers()

            market = []

            for raw in raw_tickers:
                item = parse_ticker(raw)

                if item is not None:
                    market.append(item)

            market.sort(
                key=lambda x: x["quote_volume"],
                reverse=True
            )

            market = [
                item for item in market
                if item["quote_volume"] >= MIN_24H_QUOTE_VOLUME
            ][:MAX_SYMBOLS]

            prices = {
                item["symbol"]: item["price"]
                for item in market
            }

            # Include prices for held positions even if they
            # fall outside the top-volume universe.
            missing_positions = [
                symbol
                for symbol in state["positions"]
                if symbol not in prices
            ]

            for symbol in missing_positions:
                try:
                    single = api_get(
                        "/v4/public/ticker/24h",
                        params={"symbol": symbol}
                    )

                    for raw in unwrap_result(single):
                        parsed = parse_ticker(raw)

                        if parsed:
                            prices[symbol] = parsed["price"]

                except Exception as exc:
                    log.warning(
                        "Could not refresh held symbol %s: %s",
                        symbol,
                        exc
                    )

            # Manage open positions more frequently than signals.
            manage_positions(
                state,
                prices
            )

            equity = calculate_equity(
                state,
                prices
            )

            reset_daily_if_needed(
                state,
                equity
            )

            if daily_loss_limit_reached(state, prices):
                log.warning(
                    "Daily loss limit active. "
                    "Existing positions will still be managed."
                )

            current_time = time.time()

            if current_time - last_scan_time >= SCAN_INTERVAL:
                log.info(
                    "Starting market scan. Symbols=%s",
                    len(market)
                )

                if market:
                    scan_for_entries(
                        state,
                        market
                    )

                last_scan_time = current_time

            if current_time - last_status_time >= 60:
                print_status(
                    state,
                    prices
                )

                last_status_time = current_time

            save_state(state)

            time.sleep(MANAGE_INTERVAL)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")

            save_state(state)

            break

        except Exception as exc:
            log.exception(
                "Main loop error: %s",
                exc
            )

            save_state(state)

            time.sleep(10)


if __name__ == "__main__":
    main()

