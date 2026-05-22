import os
import time
import requests
from supabase import create_client

TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Lista dei titoli da scaricare
TICKERS = [
    # USA - Tech (formato: simbolo Twelve Data)
    "AAPL", "MSFT", "GOOGL", "TSLA", "NVDA", "META", "AMZN",
    # USA - Altri
    "JPM", "V", "BRK.B",
    # Italia (Borsa Italiana = .MI)
    "ENI.MI", "ENEL.MI", "ISP.MI", "UCG.MI", "STLAM.MI",
    "FCA.MI", "G.MI", "TIT.MI", "MB.MI", "RACE.MI",
    # ETF principali
    "SPY", "QQQ", "VWCE.DE",
    # Crypto
    "BTC/USD", "ETH/USD",
]


def fetch_quote(ticker):
    """Scarica i dati di quotazione di un singolo titolo."""
    url = f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVE_DATA_KEY}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
    except Exception as e:
        print(f"  ✗ {ticker}: errore di rete ({e})")
        return None
    
    if "close" not in data:
        print(f"  ✗ {ticker}: {data.get('message', data)}")
        return None
    
    return {
        "ticker": ticker,
        "price": _to_float(data.get("close")),
        "change_percent": _to_float(data.get("percent_change")),
        "volume": _to_int(data.get("volume")),
        "market_cap": _to_float(data.get("market_cap")) if data.get("market_cap") else None,
        "pe_ratio": None,  # da quote endpoint non sempre arriva, lo riempiremo dopo
        "currency": data.get("currency"),
    }


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


def save_to_supabase(prices):
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return supabase.table("prices").insert(prices).execute()


def main():
    print(f"Scaricamento {len(TICKERS)} titoli da Twelve Data...")
    prices = []
    
    for i, ticker in enumerate(TICKERS, 1):
        price_data = fetch_quote(ticker)
        if price_data:
            prices.append(price_data)
            price = price_data["price"]
            change = price_data["change_percent"] or 0
            sign = "+" if change >= 0 else ""
            print(f"  ✓ [{i}/{len(TICKERS)}] {ticker}: {price} ({sign}{change:.2f}%)")
        # Piccola pausa tra le chiamate per non saturare il rate limit di Twelve Data
        time.sleep(0.2)
    
    if prices:
        print(f"\nSalvataggio di {len(prices)} prezzi nel database...")
        save_to_supabase(prices)
        print(f"✓ Fatto! Salvati {len(prices)}/{len(TICKERS)} titoli.")
    else:
        print("Nessun prezzo da salvare.")


if __name__ == "__main__":
    main()
