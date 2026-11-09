
import json
import os
import time
import urllib.request
from urllib.parse import urlencode

BASE = "https://sapi.xt.com"
STATE_FILE = "paper_state.json"

INITIAL_CASH = 1000.0
MAX_POSITIONS = 4
ORDER_SIZE = 100.0

TAKE_PROFIT = 0.025
STOP_LOSS = 0.015

FEE = 0.001
SLIPPAGE = 0.0005

MAX_SCAN = 35
INTERVAL = "15m"
CANDLE_LIMIT = 100

STABLECOINS = {
    "usdt", "usdc", "usd1", "usde",
    "dai", "tusd", "fdusd", "usdd"
}


def get_data(path, params=None):
    url = BASE + path

    if params:
        url += "?" + urlencode(params)

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "XT-Paper-Bot/1.0"}
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.loads(response.read())

    if isinstance(data, dict) and data.get("rc", 0) != 0:
        raise RuntimeError("XT API error: " + str(data.get("mc")))

    return data


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)

    return {
        "cash": INITIAL_CASH,
        "positions": {},
        "closed_trades": [],
        "created": int(time.time())
    }


def save_state(state):
    temp = STATE_FILE + ".tmp"

    with open(temp, "w") as f:
        json.dump(state, f, indent=2)

    os.replace(temp, STATE_FILE)


def ema(values, period):
    result = values[0]
    multiplier = 2 / (period + 1)

    for value in values[1:]:
        result = value * multiplier + result * (1 - multiplier)

    return result


def calculate_rsi(values, period=14):
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    recent = changes[-period:]

    gains = [max(x, 0) for x in recent]
    losses = [max(-x, 0) for x in recent]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_market():
    symbols_data = get_data(
        "/v4/public/symbol",
        {"tags": "spot"}
    )

    tickers_data = get_data(
        "/v4/public/ticker/24h",
        {"tags": "spot"}
    )

    symbols = symbols_data["result"]["symbols"]
    tickers = tickers_data["result"]

    allowed = {
        s["symbol"].lower()
        for s in symbols
        if s.get("quoteCurrency", "").lower() == "usdt"
        and s.get("state") == "ONLINE"
        and s.get("tradingEnabled") is True
        and s.get("openapiEnabled") is True
        and s.get("baseCurrency", "").lower() not in STABLECOINS
    }

    market = {}

    for ticker in tickers:
        symbol = ticker.get("s", "").lower()

        if symbol not in allowed:
            continue

        try:
            price = float(ticker["c"])
            volume = float(ticker["v"])

            if price > 0 and volume > 0:
                market[symbol] = {
                    "price": price,
                    "volume": volume
                }

        except (ValueError, TypeError, KeyError):
            continue

    return market


def get_signal(symbol):
    data = get_data(
        "/v4/public/kline",
        {
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": CANDLE_LIMIT
        }
    )

    candles = data.get("result", [])

    now_ms = int(time.time() * 1000)

    # Ignore the candle that may still be forming.
    candles = [
        c for c in candles
        if int(c["t"]) + 15 * 60 * 1000 <= now_ms
    ]

    candles.sort(key=lambda x: int(x["t"]))

    if len(candles) < 55:
        return None

    closes = [float(c["c"]) for c in candles]
    opens = [float(c["o"]) for c in candles]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    previous_ema20 = ema(closes[:-1], 20)
    rsi = calculate_rsi(closes)

    last_close = closes[-1]
    previous_close = closes[-2]
    last_open = opens[-1]

    # Trend + pullback recovery confirmation.
    buy_signal = (
        ema20 > ema50
        and previous_close <= previous_ema20 * 1.005
        and last_close > ema20
        and last_close > last_open
        and 42 <= rsi <= 62
    )

    return {
        "buy": buy_signal,
        "rsi": rsi,
        "close": last_close
    }


def close_position(state, symbol, market_price, reason):
    position = state["positions"][symbol]

    sell_price = market_price * (1 - SLIPPAGE)

    proceeds = (
        position["quantity"]
        * sell_price
        * (1 - FEE)
    )

    cost = position["cost"]

    profit = proceeds - cost

    state["cash"] += proceeds

    state["closed_trades"].append({
        "symbol": symbol,
        "profit": round(profit, 6),
        "reason": reason,
        "time": int(time.time())
    })

    state["closed_trades"] = state["closed_trades"][-100:]

    del state["positions"][symbol]

    print(
        "PAPER SELL:",
        symbol,
        "Reason:", reason,
        "Net P/L:", round(profit, 4),
        "USDT"
    )


def main():
    print("XT PAPER TRADING BOT")
    print("Mode: SIMULATION ONLY")

    state = load_state()
    market = get_market()

    print("Eligible USDT pairs:", len(market))

    # Check existing positions first.
    for symbol in list(state["positions"].keys()):
        if symbol not in market:
            continue

        price = market[symbol]["price"]
        position = state["positions"][symbol]
        entry = position["entry"]

        if price >= entry * (1 + TAKE_PROFIT):
            close_position(
                state, symbol, price, "TAKE PROFIT"
            )

        elif price <= entry * (1 - STOP_LOSS):
            close_position(
                state, symbol, price, "STOP LOSS"
            )

    # Select liquid pairs for technical analysis.
    candidates = sorted(
        market.items(),
        key=lambda item: item[1]["volume"],
        reverse=True
    )[:MAX_SCAN]

    for symbol, ticker in candidates:
        if len(state["positions"]) >= MAX_POSITIONS:
            break

        if symbol in state["positions"]:
            continue

        if state["cash"] < ORDER_SIZE:
            break

        try:
            signal = get_signal(symbol)
            time.sleep(0.13)

            if not signal or not signal["buy"]:
                continue

            market_price = ticker["price"]
            buy_price = market_price * (1 + SLIPPAGE)

            quantity = ORDER_SIZE / (
                buy_price * (1 + FEE)
            )

            cost = quantity * buy_price * (1 + FEE)

            if cost > state["cash"]:
                continue

            state["cash"] -= cost

            state["positions"][symbol] = {
                "entry": buy_price,
                "quantity": quantity,
                "cost": cost,
                "time": int(time.time())
            }

            print(
                "PAPER BUY:",
                symbol,
                "Price:", round(buy_price, 8),
                "RSI:", round(signal["rsi"], 2),
                "Cost:", round(cost, 2)
            )

        except Exception as error:
            print("Skipped", symbol, ":", str(error))

    save_state(state)

    position_value = sum(
        p["quantity"] * market.get(
            symbol, {}
        ).get("price", p["entry"])
        for symbol, p in state["positions"].items()
    )

    equity = state["cash"] + position_value

    print("-----------------------------")
    print("Cash:", round(state["cash"], 2), "USDT")
    print("Open positions:", len(state["positions"]))
    print("Closed trades:", len(state["closed_trades"]))
    print("Estimated equity:", round(equity, 2), "USDT")
    print("State saved.")


if __name__ == "__main__":
    main()

