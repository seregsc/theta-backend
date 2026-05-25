import os
import time
import requests
from supabase import create_client

TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# 8 ticker selezionati (rispettiamo il free tier Twelve Data)
TICKERS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "GOOGL",
    "META", "AMZN", "SPY",
]


def _to_float(v):
    try:
        return float(v) if v not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


def _to_int(v):
    try:
        return int(float(v)) if v not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


def fetch_quote(ticker):
    url = f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVE_DATA_KEY}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
    except Exception as e:
        print(f"  ✗ {ticker}: errore rete ({e})")
        return None
    
    if "close" not in data:
        print(f"  ✗ {ticker}: {data.get('message', data)}")
        return None
    
    return {
        "ticker": ticker,
        "price": _to_float(data.get("close")),
        "change_percent": _to_float(data.get("percent_change")),
        "volume": _to_int(data.get("volume")),
        "market_cap": _to_float(data.get("market_cap")),
        "pe_ratio": None,
        "currency": data.get("currency"),
    }


def save_to_supabase(prices):
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return supabase.table("prices").upsert(prices, on_conflict="ticker").execute()


def main():
    print(f"Scaricamento {len(TICKERS)} titoli da Twelve Data...")
    prices = []
    
    for i, ticker in enumerate(TICKERS, 1):
        data = fetch_quote(ticker)
        if data:
            prices.append(data)
            change = data["change_percent"] or 0
            sign = "+" if change >= 0 else ""
            print(f"  ✓ [{i}/{len(TICKERS)}] {ticker}: {data['price']} ({sign}{change:.2f}%)")
        time.sleep(0.2)
    
    if prices:
        print(f"\nSalvataggio di {len(prices)}/{len(TICKERS)} prezzi nel database...")
        save_to_supabase(prices)
        print("✓ Fatto!")
    else:
        print("\nNessun prezzo da salvare.")


if __name__ == "__main__":
    main()
