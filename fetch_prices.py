import os
import requests
from supabase import create_client

# Leggiamo le chiavi dalle variabili d'ambiente
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Lista dei titoli da scaricare (per iniziare ne mettiamo pochi)
TICKERS = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]


def fetch_price(ticker):
    """Scarica il prezzo corrente di un singolo titolo da Twelve Data."""
    url = f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={TWELVE_DATA_KEY}"
    response = requests.get(url)
    data = response.json()
    
    if "close" not in data:
        print(f"Errore su {ticker}: {data}")
        return None
    
    return {
        "ticker": ticker,
        "price": float(data["close"]),
        "change_percent": float(data.get("percent_change", 0)),
    }


def save_to_supabase(prices):
    """Salva i prezzi nel database Supabase."""
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    result = supabase.table("prices").insert(prices).execute()
    return result


def main():
    print("Inizio scaricamento prezzi...")
    prices = []
    for ticker in TICKERS:
        price_data = fetch_price(ticker)
        if price_data:
            prices.append(price_data)
            print(f"  {ticker}: ${price_data['price']:.2f} ({price_data['change_percent']:+.2f}%)")
    
    if prices:
        print(f"\nSalvataggio di {len(prices)} prezzi nel database...")
        save_to_supabase(prices)
        print("✓ Fatto!")
    else:
        print("Nessun prezzo da salvare.")


if __name__ == "__main__":
    main()
