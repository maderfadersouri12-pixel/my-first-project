import json
import urllib.request
from urllib.parse import urlencode

BASE = "https://sapi.xt.com"

def get_data(path):
    url = BASE + path
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "XT-Paper-Bot/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())

def scan_market():
    symbols_data = get_data(
        "/v4/public/symbol?tags=spot"
    )

    tickers_data = get_data(
        "/v4/public/ticker/24h?tags=spot"
    )

    symbols = symbols_data["result"]["symbols"]
    tickers = tickers_data["result"]

    allowed = {
        s["symbol"]
        for s in symbols
        if s.get("quoteCurrency") == "usdt"
        and s.get("state") == "ONLINE"
        and s.get("tradingEnabled") is True
        and s.get("openapiEnabled") is True
    }

    results = []

    for ticker in tickers:
        symbol = ticker.get("s", "").lower()

        if symbol not in allowed:
            continue

        try:
            price = float(ticker["c"])
            change = float(ticker["cr"])
            volume = float(ticker["v"])

            results.append({
                "symbol": symbol,
                "price": price,
                "change": change,
                "volume": volume
            })
        except (ValueError, TypeError, KeyError):
            continue

    results.sort(
        key=lambda x: x["volume"],
        reverse=True
    )

    print("\nXT USDT MARKET SCANNER")
    print("-" * 45)

    for coin in results[:20]:
        print(
            coin["symbol"],
            "Price:", coin["price"],
            "Change:", coin["change"],
            "Volume:", coin["volume"]
        )

    print("\nTotal eligible pairs:", len(results))

if __name__ == "__main__":
    scan_market()
