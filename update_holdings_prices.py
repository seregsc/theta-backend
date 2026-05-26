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


def get_all_tickers_from_db(supabase):
    """Recupera tutti i ticker distinti da tutte le tabelle rilevanti."""
    all_tickers = set()

    # Helper per fetchare con paginazione
    def fetch_table(table, column):
        offset = 0
        page_size = 1000
        while True:
            try:
                result = supabase.table(table).select(column).range(offset, offset + page_size - 1).execute()
            except Exception as e:
                print(f"  ⚠ {table}: {str(e)[:80]}")
                return
            if not result.data:
                return
            for row in result.data:
                t = row.get(column)
                if t:
                    all_tickers.add(t.strip())
            if len(result.data) < page_size:
                return
            offset += page_size

    # 1. holdings (ticker detenuti dai clienti)
    fetch_table("holdings", "ticker")
    after_holdings = len(all_tickers)
    print(f"   • Da holdings: {after_holdings}")

    # 2. etf_catalog (catalogo ETF Fineco)
    fetch_table("etf_catalog", "ticker")
    after_etf = len(all_tickers)
    print(f"   • +Da etf_catalog: {after_etf - after_holdings} (totale {after_etf})")

    # 3. opportunities (asset segnalati come occasioni)
    fetch_table("opportunities", "ticker")
    after_opp = len(all_tickers)
    print(f"   • +Da opportunities: {after_opp - after_etf} (totale {after_opp})")

    return sorted(all_tickers)


# Quando yfinance fallisce su un ticker, prova questi suffissi alternativi
# (stesso ETF/asset quotato su altre borse).
ALTERNATIVE_SUFFIXES = [".L", ".AS", ".DE", ".MI", ".PA", ""]


def fetch_yfinance(ticker):
    """Fetch prezzo + change_percent da yfinance, con fallback su suffissi alternativi."""
    # Prova prima il ticker originale
    result = _try_fetch(ticker)
    if result:
        return result

    # Fallback: estrai il "base" del ticker e prova altri suffissi
    base = ticker.split(".")[0] if "." in ticker else ticker
    for suffix in ALTERNATIVE_SUFFIXES:
        alt = base + suffix
        if alt == ticker:
            continue
        result = _try_fetch(alt)
        if result:
            # Salva con il ticker originale (così matcha le holdings)
            result["ticker"] = ticker
            print(f"     └─ {ticker} risolto via {alt}")
            return result
    return None


def _try_fetch(ticker):
    """Tentativo singolo di fetch yfinance (senza fallback). Restituisce None su errore."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if hist.empty:
            return None
        price = float(hist["Close"].iloc[-1])

        change_pct = None
        if len(hist) >= 2:
            prev = float(hist["Close"].iloc[-2])
            if prev > 0:
                change_pct = ((price - prev) / prev) * 100

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
    except Exception:
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

    # 1. Ticker da tutte le tabelle DB + benchmark
    print("\n📊 Recupero ticker dal database:")
    db_tickers = get_all_tickers_from_db(supabase)
    print(f"\n📊 Ticker totali da DB: {len(db_tickers)}")

    all_tickers = sorted(set(db_tickers + BENCHMARK_TICKERS))
    print(f"📊 Con benchmark aggiunti: {len(all_tickers)}\n")

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
