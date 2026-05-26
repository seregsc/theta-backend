"""
Update Prices — Yahoo Finance only, dynamic holdings.

Strategia:
1. Legge tutti i ticker distinti dalla tabella `holdings` (clienti)
2. Aggiunge una lista di benchmark fissi (indici/ETF principali)
3. Fetcha prezzo + change_percent da yfinance
4. Salva tutto nella tabella `prices` (insert, mantiene storico)

Il workflow lo lancia ogni 30 minuti durante orario mercato lun-ven.
"""

import os
import time
import yfinance as yf
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Benchmark sempre presenti, anche se nessun cliente li detiene
BENCHMARK_TICKERS = [
    # USA indici / ETF tracker
    "SPY", "QQQ", "DIA", "IWM", "VTI",
    # ETF UCITS Europa (più affidabili come benchmark per consulenti italiani)
    "VWCE.DE", "SWDA.MI", "IWDA.AS", "CSPX.MI", "EUNL.DE",
    # Indici italiani / Europa
    "FTSEMIB.MI",
    # Crypto
    "BTC-USD", "ETH-USD",
    # Commodities (futures)
    "GC=F",   # Gold
    "CL=F",   # WTI Crude
    "SI=F",   # Silver
    # Bond
    "TLT", "IEF", "SHY",
    # Tassi (proxy)
    "^TNX",   # US 10-year yield
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


def get_holdings_tickers(supabase):
    """Recupera tutti i ticker distinti dalle holdings dei clienti."""
    try:
        # Paginiamo perché alcune query Supabase tagliano a 1000 default
        all_tickers = set()
        offset = 0
        page_size = 1000
        while True:
            result = supabase.table("holdings").select("ticker").range(offset, offset + page_size - 1).execute()
            if not result.data:
                break
            for row in result.data:
                t = row.get("ticker")
                if t:
                    all_tickers.add(t.strip())
            if len(result.data) < page_size:
                break
            offset += page_size
        return sorted(all_tickers)
    except Exception as e:
        print(f"  ✗ errore lettura holdings: {e}")
        return []


def fetch_yfinance(ticker):
    """Fetch prezzo + change_percent da yfinance. Restituisce dict o None."""
    try:
        t = yf.Ticker(ticker)
        # period='5d' invece di '2d' per coprire weekend/festività
        # così abbiamo sempre 2 close validi anche di lunedì mattina
        hist = t.history(period="5d")
        if hist.empty:
            return None
        price = float(hist["Close"].iloc[-1])

        # change_percent = (close ultimo - close penultimo) / close penultimo * 100
        change_pct = None
        if len(hist) >= 2:
            prev = float(hist["Close"].iloc[-2])
            if prev > 0:
                change_pct = ((price - prev) / prev) * 100

        # info è opzionale (può fallire per alcuni ticker esotici)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            pass

        return {
            "ticker": ticker,
            "price": price,
            "change_percent": change_pct,
            "volume": _to_int(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None,
            "market_cap": _to_float(info.get("marketCap")),
            "pe_ratio": _to_float(info.get("trailingPE")),
            "currency": info.get("currency"),
        }
    except Exception as e:
        # Errore singolo ticker: log e continua
        msg = str(e)[:80]
        print(f"  ✗ {ticker}: {msg}")
        return None


def save_prices(supabase, prices):
    """Upsert dei prezzi nella tabella `prices` (sovrascrive l'ultimo per ticker)."""
    return supabase.table("prices").upsert(prices, on_conflict="ticker").execute()


def main():
    print("=" * 60)
    print("UPDATE PRICES — yfinance, dynamic holdings")
    print("=" * 60)

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠ SUPABASE_URL o SUPABASE_KEY mancanti")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 1. Ticker da holdings + benchmark
    holdings_tickers = get_holdings_tickers(supabase)
    print(f"\n📊 Ticker da holdings clienti: {len(holdings_tickers)}")
    if holdings_tickers:
        print(f"   Esempio: {holdings_tickers[:5]}")

    all_tickers = sorted(set(holdings_tickers + BENCHMARK_TICKERS))
    print(f"📊 Ticker totali (holdings + benchmark): {len(all_tickers)}\n")

    # 2. Fetch
    print("--- Fetch yfinance ---")
    prices = []
    failed = []
    for i, ticker in enumerate(all_tickers, 1):
        data = fetch_yfinance(ticker)
        if data:
            prices.append(data)
            change = data.get("change_percent") or 0
            sign = "+" if change >= 0 else ""
            print(f"  ✓ [{i}/{len(all_tickers)}] {ticker}: {data['price']:.2f} ({sign}{change:.2f}%)")
        else:
            failed.append(ticker)
        # yfinance: pausa breve per non farsi rate-limitare
        time.sleep(0.25)

    # 3. Save
    print()
    if prices:
        # Salviamo a batch per evitare timeout su tante righe
        batch_size = 50
        saved_total = 0
        for i in range(0, len(prices), batch_size):
            batch = prices[i:i + batch_size]
            try:
                save_prices(supabase, batch)
                saved_total += len(batch)
            except Exception as e:
                print(f"  ✗ batch {i}-{i+len(batch)}: {e}")
        print(f"✓ Salvati {saved_total}/{len(all_tickers)} prezzi nel database.")
    else:
        print("⚠ Nessun prezzo da salvare.")

    if failed:
        print(f"\n⚠ Ticker falliti ({len(failed)}): {', '.join(failed[:15])}{'...' if len(failed) > 15 else ''}")


if __name__ == "__main__":
    main()
