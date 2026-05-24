import os
import yfinance as yf
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")  # service_role key (bypassa RLS)


def fetch_price_yf(ticker):
    """Recupera prezzo corrente da yfinance. Ritorna None se ticker non valido."""
    try:
        # Adatta ticker al formato Yahoo: italiani .MI, tedeschi .DE, francesi .PA, ecc.
        # Sono già nel formato giusto, yfinance accetta direttamente questi suffissi.
        t = yf.Ticker(ticker)
        # Tentativo 1: fast_info (più veloce)
        try:
            price = t.fast_info.get("last_price")
            if price and price > 0:
                return float(price)
        except Exception:
            pass
        # Tentativo 2: history degli ultimi giorni
        hist = t.history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
        # Tentativo 3: info
        info = t.info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        if price and price > 0:
            return float(price)
        return None
    except Exception as e:
        print(f"  [yf error] {ticker}: {e}")
        return None


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("[update_prices] Recupero tutti gli holdings dal DB...")
    
    # Carica tutti gli holdings
    res = supabase.table("holdings").select("id, ticker, quantity").execute()
    holdings = res.data or []
    print(f"[update_prices] {len(holdings)} holdings totali")
    
    if not holdings:
        print("[update_prices] Nessun holding da aggiornare.")
        return
    
    # Trova ticker unici
    unique_tickers = sorted(set(h["ticker"] for h in holdings if h.get("ticker")))
    print(f"[update_prices] {len(unique_tickers)} ticker unici: {unique_tickers}")
    
    # Recupera prezzo per ogni ticker
    prices = {}
    for ticker in unique_tickers:
        print(f"  Recupero {ticker}...", end=" ")
        price = fetch_price_yf(ticker)
        if price is not None:
            prices[ticker] = price
            print(f"€{price:.2f}")
        else:
            print("FAIL")
    
    print(f"\n[update_prices] {len(prices)}/{len(unique_tickers)} prezzi recuperati")
    
    # Aggiorna gli holdings
    updated = 0
    for h in holdings:
        ticker = h.get("ticker")
        if ticker not in prices:
            continue
        new_value = float(h.get("quantity", 0)) * prices[ticker]
        try:
            supabase.table("holdings").update({
                "current_value": new_value,
            }).eq("id", h["id"]).execute()
            updated += 1
        except Exception as e:
            print(f"  [save error] {h['id']}: {e}")
    
    print(f"[update_prices] Aggiornati {updated} holdings.")


if __name__ == "__main__":
    main()
