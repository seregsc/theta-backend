import os
import yfinance as yf
from supabase import create_client
from datetime import datetime, timezone, date

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Ticker hardcoded di base (compatibilità con setup attuale).
# Ai ticker qui sotto vengono aggiunti dinamicamente:
#  - tutti gli ETF dal catalogo Supabase (etf_catalog)
#  - tutti i ticker effettivamente detenuti dai clienti (holdings)
ALL_TICKERS = [
    "1211.HK", "ABBV", "ACWI.MI", "ADYEN.AS", "AEM", "AFRM", "AGG", "AI4U.MI", "AMD", "AMGN",
    "AMT", "AMZN", "ANET", "ANTO.L", "ASML", "AVGO", "AZN.L", "BA.L", "BABA", "BAC",
    "BHP", "BIIB", "BMW.DE", "BNP.PA", "BP", "BP.L", "BRBY.L", "BTC", "BTPM", "BTPM.MI",
    "BWXT", "BYD", "CAT", "CCI", "CCJ", "CDI.PA", "CEG", "CFR.SW", "COIN", "COP",
    "COPX", "COST", "CPR.MI", "CRUD.MI", "CRWD", "CVX", "DE", "DELL", "DFEN.MI", "DIS",
    "DJE.PA", "DLR", "DNN", "DTE.DE", "DUK", "EEM", "EMB", "EMR", "ENB.TO", "ENEL.MI",
    "ENI.MI", "EQIX", "EQNR", "EQNR.OL", "ERO", "ETH", "ETHA", "ETHE", "ETN", "EWI",
    "EWY", "EWZ", "EZU", "F", "FBK.MI", "FBTC", "FCX", "FEZ", "FNV", "FXI",
    "GD", "GDX", "GDXJ", "GE", "GILD", "GLD", "GLD.MI", "GLEN.L", "GM", "GOLD",
    "GOOGL", "GS", "HAG.DE", "HD", "HII", "HON", "HYG", "IAU", "IBE.MC", "IBIT",
    "IBND", "IBND.MI", "ICLN", "IEF", "INDA", "INFY", "INTC", "ISP.MI", "IVN.TO", "IXC",
    "JNJ", "JPM", "KER.PA", "KO", "LDO.MI", "LLY", "LMT", "LQD", "MA", "MARA",
    "MBG.DE", "MC.PA", "MCD", "MCHI", "MELI", "META", "MMM", "MONC.MI", "MRK", "MS",
    "MSFT", "MSTR", "MU", "NEE", "NEM", "NESN.SW", "NKE", "NLR", "NOC", "NRG",
    "NVDA", "NVO", "O", "OKLO", "OR.PA", "ORA.PA", "P911.DE", "PAAS", "PFE", "PG",
    "PLD", "PLTR", "QCOM", "QQQ", "R2US.MI", "RACE", "REGN", "RHM.DE", "RIO", "RIOT",
    "RMS.PA", "RR.", "RTX", "SBUX", "SCCO", "SHEL", "SHY", "SIE.DE", "SLB", "SLV",
    "SMR", "SO", "SOL", "SPG", "SPY", "SQ", "STLAM.MI", "T", "TECK", "TIT.MI",
    "TLT", "TM", "TMUS", "TRN.MI", "TSLA", "TSM", "TTE.PA", "TXT", "UCG.MI", "UEC",
    "URA", "V", "VOD.L", "VRT", "VRTX", "VST", "VWO", "VZ", "WELL", "WMT",
    "WPM", "WTIO.MI", "XAUUSD", "XEON.MI", "XLU", "XMME.MI", "XOM", "XOP",
]

YF_ALIAS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XAUUSD": "XAUUSD=X",
    "BTPM": "IEAC.MI",
    "BTPM.MI": "IEAC.MI",
    "RR.": "RR.L",
}


def get_all_tickers(supabase):
    """Compone la lista finale dei ticker da scaricare unendo:
      - ALL_TICKERS hardcoded
      - tutti i ticker da etf_catalog (attivi)
      - tutti i ticker effettivamente in holdings dei clienti
    Restituisce la lista deduplicata e ordinata.
    """
    tickers = set(ALL_TICKERS)
    initial_count = len(tickers)

    # 1) ETF catalog (tutti gli ETF Fineco)
    try:
        res = supabase.table("etf_catalog").select("ticker").eq("active", True).execute()
        rows = res.data or []
        etf_tickers = [r["ticker"] for r in rows if r.get("ticker")]
        before = len(tickers)
        tickers.update(etf_tickers)
        added = len(tickers) - before
        print(f"[get_all_tickers] etf_catalog: {len(etf_tickers)} ticker letti, {added} nuovi aggiunti.")
    except Exception as e:
        print(f"[get_all_tickers] errore etf_catalog: {e}")

    # 2) Ticker effettivamente in holdings dei clienti
    try:
        res = supabase.table("holdings").select("ticker").execute()
        rows = res.data or []
        holding_tickers = [r["ticker"] for r in rows if r.get("ticker")]
        before = len(tickers)
        tickers.update(holding_tickers)
        added = len(tickers) - before
        print(f"[get_all_tickers] holdings: {len(holding_tickers)} record letti, {added} nuovi ticker aggiunti.")
    except Exception as e:
        print(f"[get_all_tickers] errore holdings: {e}")

    final_list = sorted(tickers)
    print(f"[get_all_tickers] hardcoded {initial_count} + dinamici {len(final_list) - initial_count} = TOTALE {len(final_list)} ticker.")
    return final_list


def fetch_price_yf(ticker_original):
    """Recupera prezzo corrente e variazione % da yfinance.
    Restituisce (price, change_pct, currency) o (None, None, None)."""
    yf_ticker = YF_ALIAS.get(ticker_original, ticker_original)
    try:
        t = yf.Ticker(yf_ticker)
        try:
            fi = t.fast_info
            price = fi.get("last_price")
            prev_close = fi.get("previous_close")
            currency = fi.get("currency") or "USD"
            if price and price > 0:
                change_pct = None
                if prev_close and prev_close > 0:
                    change_pct = ((price - prev_close) / prev_close) * 100.0
                return float(price), change_pct, currency
        except Exception:
            pass
        hist = t.history(period="5d")
        if len(hist) >= 1:
            price = float(hist["Close"].iloc[-1])
            change_pct = None
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                if prev > 0:
                    change_pct = ((price - prev) / prev) * 100.0
            currency = "USD"
            try:
                currency = t.info.get("currency") or "USD"
            except Exception:
                pass
            return price, change_pct, currency
        return None, None, None
    except Exception as e:
        print(f"  [yf error] {ticker_original} (yf:{yf_ticker}): {e}")
        return None, None, None


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Composizione lista ticker dinamica
    tickers_to_fetch = get_all_tickers(supabase)
    print(f"[update_prices] {len(tickers_to_fetch)} ticker da aggiornare...")

    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = date.today().isoformat()  # YYYY-MM-DD

    prices_data = {}
    updated_prices_count = 0
    saved_history_count = 0
    failed_count = 0
    failed_tickers = []

    for i, ticker in enumerate(tickers_to_fetch, 1):
        print(f"  [{i}/{len(tickers_to_fetch)}] {ticker}...", end=" ", flush=True)
        price, change_pct, currency = fetch_price_yf(ticker)
        if price is None:
            print("FAIL")
            failed_count += 1
            failed_tickers.append(ticker)
            continue
        prices_data[ticker] = price
        ch_str = f"({change_pct:+.2f}%)" if change_pct is not None else ""
        print(f"{currency} {price:.2f} {ch_str}")

        # 1. Upsert in tabella prices (snapshot ultimo prezzo)
        try:
            supabase.table("prices").upsert({
                "ticker": ticker,
                "price": price,
                "change_percent": change_pct,
                "currency": currency,
                "fetched_at": now_iso,
                "created_at": now_iso,
            }, on_conflict="ticker").execute()
            updated_prices_count += 1
        except Exception as e:
            print(f"    [prices save error] {e}")

        # 2. Upsert in tabella prices_history (1 record per (ticker, giorno))
        try:
            supabase.table("prices_history").upsert({
                "ticker": ticker,
                "date": today_str,
                "price": price,
                "currency": currency,
            }, on_conflict="ticker,date").execute()
            saved_history_count += 1
        except Exception as e:
            print(f"    [history save error] {e}")

    print(f"\n[update_prices] Aggiornati {updated_prices_count}/{len(tickers_to_fetch)} prezzi.")
    print(f"[update_prices] Salvati {saved_history_count} record in prices_history.")
    print(f"[update_prices] {failed_count} FAIL.")
    if failed_tickers:
        # Stampa max i primi 30 per non intasare il log
        preview = failed_tickers[:30]
        suffix = "" if len(failed_tickers) <= 30 else f" (+ altri {len(failed_tickers) - 30})"
        print(f"[update_prices] Ticker falliti: {', '.join(preview)}{suffix}")

    # 3. Aggiorna current_value degli holdings
    try:
        res = supabase.table("holdings").select("id, ticker, quantity").execute()
        holdings = res.data or []
    except Exception as e:
        print(f"[update_prices] Errore lettura holdings: {e}")
        holdings = []

    updated_holdings_count = 0
    for h in holdings:
        ticker = h.get("ticker")
        if ticker not in prices_data:
            continue
        try:
            new_value = float(h.get("quantity", 0)) * prices_data[ticker]
            supabase.table("holdings").update({
                "current_value": new_value,
            }).eq("id", h["id"]).execute()
            updated_holdings_count += 1
        except Exception as e:
            print(f"  [holdings save error] {h['id']}: {e}")

    print(f"[update_prices] Aggiornati {updated_holdings_count}/{len(holdings)} holdings.")


if __name__ == "__main__":
    main()


