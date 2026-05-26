"""
Backfill Prices History — Scarica storico daily di ogni ticker e salva in Supabase.

Strategia smart:
- Al primo run per un ticker: scarica 5 anni di storico
- Run successivi: scarica solo ultimi 30 giorni (incremental update)
- Se FORCE_FULL=true: forza il 5y backfill per TUTTI i ticker (una tantum)

Ticker presi da:
  - Tabelle Supabase: holdings, etf_catalog, opportunities
  - Lista BENCHMARK_TICKERS (indici, materie prime, valute)
  - Lista APP_TICKERS (asset hardcoded nell'app, scoperti via discover_hardcoded_tickers.py)

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
FORCE_FULL = os.environ.get("FORCE_FULL", "").lower() in ("true", "1", "yes")

# ─────────────────────────────────────────────────────────────────────
# BENCHMARK: indici, materie prime, bond, valute usati nei confronti
# ─────────────────────────────────────────────────────────────────────
BENCHMARK_TICKERS = [
    "SPY", "QQQ", "DIA", "IWM", "VTI",
    "VWCE.DE", "SWDA.MI", "IWDA.AS", "EUNL.DE",
    "FTSEMIB.MI",
    "BTC-USD", "ETH-USD",
    "GC=F", "CL=F", "SI=F",
    "TLT", "IEF", "SHY",
    "^TNX",
]

# ─────────────────────────────────────────────────────────────────────
# APP_TICKERS: asset hardcoded nell'app (scoperti via discover_hardcoded_tickers)
# Lista verificata: tutti restituiscono dati validi da yfinance.
# ─────────────────────────────────────────────────────────────────────
APP_TICKERS = [
    "1211.HK", "AAPL", "ABBV", "ACWI.MI",
    "ADYEN.AS", "AEM", "AFRM", "AGG",
    "AI4U.MI", "AIR.PA", "AMD", "AMGN",
    "AMT", "AMZN", "ANET", "ANTO.L",
    "ARM", "ASML", "AVGO", "AZN.L",
    "BA.L", "BABA", "BAC", "BAMI.MI",
    "BHP", "BIIB", "BMW.DE", "BNP.PA",
    "BP", "BP.L", "BRBY.L", "BTC",
    "BWXT", "BYD", "CAT", "CCI",
    "CCJ", "CDI.PA", "CEG", "CFR.SW",
    "CHYM", "COIN", "COP", "COPX",
    "COST", "CPR.MI", "CRUD.MI", "CRWD",
    "CRWV", "CVX", "DE", "DELL",
    "DIS", "DJE.PA", "DLR", "DNN",
    "DTE.DE", "DUK", "EEM", "EMB",
    "EMR", "ENB.TO", "ENEL.MI", "ENI.MI",
    "EQIX", "EQNR", "EQNR.OL", "ERO",
    "ETH", "ETHA", "ETHE", "ETN",
    "EUNX.DE", "EWI", "EWJ", "EWY",
    "EWZ", "EZU", "F", "FBK.MI",
    "FBTC", "FCX", "FEZ", "FNV",
    "FXI", "GD", "GDX", "GDXJ",
    "GE", "GILD", "GLD", "GLEN.L",
    "GM", "GOLD", "GOOGL", "GS",
    "HAG.DE", "HD", "HII", "HON",
    "HYG", "IAU", "IBE.MC", "IBIT",
    "IBND", "ICLN", "IEF", "INDA",
    "INFY", "INTC", "ISP.MI", "IVN.TO",
    "IXC", "JNJ", "JPM", "KER.PA",
    "KLAR", "KO", "LDO.MI", "LLY",
    "LMT", "LQD", "MA", "MAR",
    "MARA", "MBG.DE", "MC.PA", "MCD",
    "MCHI", "MELI", "META", "MMM",
    "MONC.MI", "MRK", "MS", "MSFT",
    "MSTR", "MU", "NEE", "NEM",
    "NESN.SW", "NKE", "NLR", "NOC",
    "NRG", "NVDA", "NVO", "O",
    "OKLO", "OR.PA", "ORA.PA", "P911.DE",
    "PAAS", "PFE", "PG", "PLD",
    "PLTR", "QCOM", "QQQ", "R2US.MI",
    "RACE", "REGN", "RHM.DE", "RIO",
    "RIOT", "RMS.PA", "RTX", "RVMD",
    "SAAB-B.ST", "SAN.PA", "SBUX", "SCCO",
    "SHEL", "SHY", "SIE.DE", "SLB",
    "SLV", "SMCI", "SMR", "SO",
    "SPG", "SPY", "STLAM.MI", "T",
    "TECK", "TIT.MI", "TLT", "TM",
    "TMUS", "TRN.MI", "TSLA", "TSM",
    "TTE.PA", "TXT", "UCG.MI", "UEC",
    "URA", "V", "VALE", "VOD.L",
    "VRT", "VRTX", "VST", "VWO",
    "VZ", "WELL", "WMT", "WPM",
    "XEON.MI", "XLF", "XLU", "XMME.MI",
    "XOM", "XOP",
]


# ─────────────────────────────────────────────────────────────────────
# CURRENCY MAPPING — dedotta dal suffisso del ticker
# ─────────────────────────────────────────────────────────────────────
SUFFIX_TO_CURRENCY = {
    ".MI": "EUR", ".DE": "EUR", ".F":  "EUR", ".PA": "EUR",
    ".AS": "EUR", ".BR": "EUR", ".LS": "EUR", ".MC": "EUR",
    ".VI": "EUR", ".HE": "EUR", ".IR": "EUR",
    ".L":  "GBP", ".SW": "CHF",
    ".TO": "CAD", ".V":  "CAD",
    ".HK": "HKD", ".T":  "JPY", ".KS": "KRW",
    ".AX": "AUD", ".NS": "INR", ".BO": "INR", ".SA": "BRL",
    ".OL": "NOK", ".ST": "SEK", ".CO": "DKK",
}


def infer_currency(ticker: str) -> str:
    """Ritorna la valuta probabile per un ticker yfinance basandosi sul suffisso."""
    t = ticker.upper().strip()
    if "-USD" in t:
        return "USD"
    if "-EUR" in t:
        return "EUR"
    if t.startswith("^"):
        if "STOXX" in t or t == "^FTSEMIB":
            return "EUR"
        if t == "^FTSE":
            return "GBP"
        if t == "^N225":
            return "JPY"
        return "USD"
    if t.endswith("=F") or t.endswith("=X"):
        return "USD"
    if "." in t:
        suffix = "." + t.split(".")[-1]
        if suffix in SUFFIX_TO_CURRENCY:
            return SUFFIX_TO_CURRENCY[suffix]
    return "USD"


def get_all_tickers_from_db(supabase):
    """Ticker distinti da holdings + etf_catalog + opportunities."""
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
    rows = []
    for idx, row in hist.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        if since_date and date_str <= since_date:
            continue
        close = row.get("Close")
        if close is None or (isinstance(close, float) and (close != close)):
            continue
        rows.append({
            "ticker": ticker,
            "date": date_str,
            "price": float(close),
            "currency": currency,
        })
    return rows


def upsert_rows(supabase, rows):
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
    if FORCE_FULL:
        print("⚡ FORCE_FULL=true → ricarica 5y per TUTTI i ticker")
    print("=" * 60)

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠ SUPABASE_URL o SUPABASE_KEY mancanti")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    db_tickers = get_all_tickers_from_db(supabase)
    all_tickers = sorted(set(db_tickers + BENCHMARK_TICKERS + APP_TICKERS))
    print(f"\n📊 Ticker totali da processare: {len(all_tickers)}")
    print(f"   · da DB:        {len(db_tickers)}")
    print(f"   · benchmark:    {len(BENCHMARK_TICKERS)}")
    print(f"   · app tickers:  {len(APP_TICKERS)}")
    print(f"   · UNION:        {len(all_tickers)}")

    full_count = 0
    incremental_count = 0
    skipped_count = 0
    total_rows_added = 0

    for i, ticker in enumerate(all_tickers, 1):
        if FORCE_FULL:
            period = "5y"
            mode = "FULL"
            since = None
        else:
            last_date = get_last_date_for_ticker(supabase, ticker)
            if last_date is None:
                period = "5y"
                mode = "FULL"
                since = None
            else:
                try:
                    last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
                    days_old = (datetime.utcnow().date() - last_dt).days
                    if days_old < 1:
                        print(f"  · [{i}/{len(all_tickers)}] {ticker}: già aggiornato")
                        skipped_count += 1
                        continue
                except Exception:
                    pass
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

        time.sleep(0.25)

    print()
    print(f"📊 Riepilogo:")
    print(f"   • Full backfill: {full_count}")
    print(f"   • Incremental: {incremental_count}")
    print(f"   • Già aggiornati: {skipped_count}")
    print(f"   • Righe totali aggiunte: {total_rows_added}")
    print("✓ Backfill completato.")


if __name__ == "__main__":
    main()
