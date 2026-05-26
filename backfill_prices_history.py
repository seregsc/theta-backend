
"""
Backfill Prices History — Scarica storico daily di ogni ticker e salva in Supabase.

Strategia smart:
- Al primo run per un ticker: scarica 5 anni di storico
- Run successivi: scarica solo ultimi 30 giorni (incremental update)

Tickers letti dinamicamente da holdings/etf_catalog/opportunities + benchmark fissi.
Tabella: prices_history (id, ticker, date, price, currency, created_at)
UNIQUE constraint su (ticker, date) → upsert sicuro, niente duplicati.
"""

import os
import time
import yfinance as yf
from datetime import datetime, timedelta
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

BENCHMARK_TICKERS = [
    "SPY", "QQQ", "DIA", "IWM", "VTI",
    "VWCE.DE", "SWDA.MI", "IWDA.AS", "CSPX.MI", "EUNL.DE",
    "FTSEMIB.MI",
    "BTC-USD", "ETH-USD",
    "GC=F", "CL=F", "SI=F",
    "TLT", "IEF", "SHY",
    "^TNX",
]


# ─────────────────────────────────────────────────────────────────────
# CURRENCY MAPPING — dedotta dal suffisso del ticker
# ─────────────────────────────────────────────────────────────────────
SUFFIX_TO_CURRENCY = {
    ".MI": "EUR",   # Borsa Italiana
    ".DE": "EUR",   # Xetra
    ".F":  "EUR",   # Frankfurt
    ".PA": "EUR",   # Euronext Paris
    ".AS": "EUR",   # Euronext Amsterdam
    ".BR": "EUR",   # Euronext Brussels
    ".LS": "EUR",   # Euronext Lisbon
    ".MC": "EUR",   # Madrid
    ".VI": "EUR",   # Vienna
    ".HE": "EUR",   # Helsinki
    ".IR": "EUR",   # Ireland
    ".L":  "GBP",   # London
    ".SW": "CHF",   # SIX Swiss
    ".TO": "CAD",   # Toronto
    ".V":  "CAD",   # TSX Venture
    ".HK": "HKD",   # Hong Kong
    ".T":  "JPY",   # Tokyo
    ".KS": "KRW",   # Korea
    ".AX": "AUD",   # Australia
    ".NS": "INR",   # India NSE
    ".BO": "INR",   # India BSE
    ".SA": "BRL",   # São Paulo
}


def infer_currency(ticker: str) -> str:
    """Ritorna la valuta probabile per un ticker yfinance basandosi sul suffisso."""
    t = ticker.upper().strip()

    # Crypto pairs (BTC-USD, ETH-USD, ...)
    if "-USD" in t:
        return "USD"
    if "-EUR" in t:
        return "EUR"

    # Indici Yahoo (^TNX, ^GSPC, ^STOXX50E, ...)
    if t.startswith("^"):
        # ^STOXX50E è EUR, ^FTSE è GBP, ^N225 è JPY — ma il valore degli indici
        # è un numero puro (non un prezzo), quindi USD è solo un placeholder.
        if "STOXX" in t or t == "^FTSEMIB":
            return "EUR"
        if t == "^FTSE":
            return "GBP"
        if t == "^N225":
            return "JPY"
        return "USD"

    # Futures (=F)
    if t.endswith("=F"):
        return "USD"

    # Forex (=X)
    if t.endswith("=X"):
        return "USD"

    # Match per suffisso .XX
    if "." in t:
        suffix = "." + t.split(".")[-1]
        if suffix in SUFFIX_TO_CURRENCY:
            return SUFFIX_TO_CURRENCY[suffix]

    # Default: nessun suffisso → US stock/ETF → USD
    return "USD"


def get_all_tickers_from_db(supabase):
    """Ticker distinti da holdings + etf_catalog + opportunities + benchmark."""
    all_tickers = set()

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

    fetch_table("holdings", "ticker")
    fetch_table("etf_catalog", "ticker")
    fetch_table("opportunities", "ticker")
    return sorted(all_tickers)


def get_last_date_for_ticker(supabase, ticker):
    """Restituisce la data dell'ultimo record storico per quel ticker, o None se vuoto."""
    try:
        result = supabase.table("prices_history") \
            .select("date") \
            .eq("ticker", ticker) \
            .order("date", desc=True) \
            .limit(1) \
            .execute()
        if result.data and result.data[0].get("date"):
            return result.data[0]["date"]
    except Exception:
        pass
    return None


def fetch_history(ticker, period):
    """Scarica history da yfinance per il periodo richiesto."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period, interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        return hist
    except Exception as e:
        print(f"  ✗ {ticker}: {str(e)[:80]}")
        return None


def hist_to_rows(ticker, hist, currency, since_date=None):
    """Trasforma il DataFrame yfinance in lista di dict pronta per upsert.
    Se since_date è dato, filtra solo righe successive a quella data."""
    rows = []
    for idx, row in hist.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        if since_date and date_str <= since_date:
            continue
        close = row.get("Close")
        if close is None or (isinstance(close, float) and (close != close)):  # NaN check
            continue
        rows.append({
            "ticker": ticker,
            "date": date_str,
            "price": float(close),
            "currency": currency,
        })
    return rows


def upsert_rows(supabase, rows):
    """Upsert su prices_history con conflict resolution su (ticker, date)."""
    if not rows:
        return 0
    batch_size = 500
    saved = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            supabase.table("prices_history").upsert(batch, on_conflict="ticker,date").execute()
            saved += len(batch)
        except Exception as e:
            print(f"    ✗ batch upsert {i}-{i+len(batch)}: {str(e)[:120]}")
    return saved


def main():
    print("=" * 60)
    print("BACKFILL PRICES HISTORY — yfinance → Supabase")
    print("=" * 60)

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠ SUPABASE_URL o SUPABASE_KEY mancanti")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 1. Recupera ticker
    db_tickers = get_all_tickers_from_db(supabase)
    all_tickers = sorted(set(db_tickers + BENCHMARK_TICKERS))
    print(f"\n📊 Ticker totali da processare: {len(all_tickers)}")

    # 2. Per ogni ticker: incremental o full backfill
    full_count = 0
    incremental_count = 0
    skipped_count = 0
    total_rows_added = 0

    for i, ticker in enumerate(all_tickers, 1):
        last_date = get_last_date_for_ticker(supabase, ticker)

        if last_date is None:
            # FULL backfill: 5 anni di storico
            period = "5y"
            mode = "FULL"
            since = None
        else:
            # Controlla se serve aggiornare (ultimo record vecchio di almeno 1 giorno)
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
                days_old = (datetime.utcnow().date() - last_dt).days
                if days_old < 1:
                    print(f"  · [{i}/{len(all_tickers)}] {ticker}: già aggiornato (last: {last_date})")
                    skipped_count += 1
                    continue
            except Exception:
                pass
            # INCREMENTAL: ultimi 30 giorni (sovrabbondante per riempire eventuali giorni mancanti)
            period = "1mo"
            mode = "INCR"
            since = last_date

        hist = fetch_history(ticker, period)
        if hist is None:
            continue

        currency = infer_currency(ticker)
        rows = hist_to_rows(ticker, hist, currency, since_date=since)
        if not rows:
            print(f"  · [{i}/{len(all_tickers)}] {ticker} [{mode}]: nessuna riga nuova")
            continue

        saved = upsert_rows(supabase, rows)
        if mode == "FULL":
            full_count += 1
        else:
            incremental_count += 1
        total_rows_added += saved
        print(f"  ✓ [{i}/{len(all_tickers)}] {ticker} [{mode}] ({currency}): +{saved} righe")

        time.sleep(0.25)  # politeness yfinance

    print()
    print(f"📊 Riepilogo:")
    print(f"   • Full backfill: {full_count}")
    print(f"   • Incremental: {incremental_count}")
    print(f"   • Già aggiornati: {skipped_count}")
    print(f"   • Righe totali aggiunte: {total_rows_added}")
    print("✓ Backfill completato.")


if __name__ == "__main__":
    main()
