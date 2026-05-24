import os
import yfinance as yf
from supabase import create_client
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Lista completa dei ticker disponibili in Theta (da SECTOR_ASSETS del frontend)
ALL_TICKERS = [
    "1211.HK", "ABBV", "ADYEN.AS", "AEM", "AFRM", "AGG", "AMD", "AMGN", "AMT", "AMZN",
    "ANET", "ANTO.L", "ASML", "AVGO", "AZN.L", "BA.L", "BABA", "BAC", "BHP", "BIIB",
    "BMW.DE", "BNP.PA", "BP", "BP.L", "BRBY.L", "BTC", "BTPM", "BTPM.MI", "BWXT", "BYD",
    "CAT", "CCI", "CCJ", "CDI.PA", "CEG", "CFR.SW", "COIN", "COP", "COPX", "COST",
    "CPR.MI", "CRUD.MI", "CRWD", "CVX", "DE", "DELL", "DFEN.MI", "DIS", "DLR", "DNN",
    "DTE.DE", "DUK", "EEM", "EMB", "EMR", "ENB.TO", "ENEL.MI", "ENI.MI", "EQIX", "EQNR",
    "EQNR.OL", "ERO", "ETH", "ETHA", "ETHE", "ETN", "EWI", "EWY", "EWZ", "EZU",
    "F", "FBK.MI", "FBTC", "FCX", "FEZ", "FNV", "FXI", "GD", "GDX", "GDXJ",
    "GE", "GILD", "GLD", "GLD.MI", "GLEN.L", "GM", "GOLD", "GOOGL", "GS", "HAG.DE",
    "HD", "HII", "HON", "HYG", "IAU", "IBE.MC", "IBIT", "IBND", "IBND.MI", "ICLN",
    "IEF", "INDA", "INFY", "INTC", "ISP.MI", "IVN.TO", "IXC", "JNJ", "JPM", "KER.PA",
    "KO", "LDO.MI", "LLY", "LMT", "LQD", "MA", "MARA", "MBG.DE", "MC.PA", "MCD",
    "MCHI", "MELI", "META", "MMM", "MONC.MI", "MRK", "MS", "MSFT", "MSTR", "MU",
    "NEE", "NEM", "NESN.SW", "NKE", "NLR", "NOC", "NRG", "NVDA", "NVO", "O",
    "OKLO", "OR.PA", "ORA.PA", "P911.DE", "PAAS", "PFE", "PG", "PLD", "PLTR", "QCOM",
    "QQQ", "RACE", "REGN", "RHM.DE", "RIO", "RIOT", "RMS.PA", "RR.", "RTX", "SBUX",
    "SCCO", "SHEL", "SHY", "SIE.DE", "SLB", "SLV", "SMR", "SO", "SOL", "SPG",
    "SPY", "SQ", "STLAM.MI", "T", "TECK", "TIT.MI", "TLT", "TM", "TMUS", "TRN.MI",
    "TSLA", "TSM", "TTE.PA", "TXT", "UCG.MI", "UEC", "URA", "V", "VOD.L", "VRT",
    "VRTX", "VST", "VWO", "VZ", "WELL", "WMT", "WPM", "WTIO.MI", "XAUUSD", "XLU",
    "XOM", "XOP",
]

# Alias: alcuni ticker hanno un formato diverso su Yahoo Finance
YF_ALIAS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XAUUSD": "GC=F",     # oro futures su Yahoo
    "BTPM": "IEAC.MI",    # ETF BTP proxy (BTPM non è su Yahoo, uso BTP medi euro)
    "BTPM.MI": "IEAC.MI",
    "RR.": "RR.L",        # Rolls-Royce su LSE
}


def fetch_price_yf(ticker_original):
    """Recupera prezzo corrente da yfinance per un ticker Theta.
    Restituisce (price, currency) o (None, None)."""
    yf_ticker = YF_ALIAS.get(ticker_original, ticker_original)
    try:
        t = yf.Ticker(yf_ticker)
        # Tentativo 1: fast_info (più rapido)
        try:
            fi = t.fast_info
            price = fi.get("last_price")
            currency = fi.get("currency") or "USD"
            if price and price > 0:
                return float(price), currency
        except Exception:
            pass
        # Tentativo 2: history degli ultimi 5 giorni
        hist = t.history(period="5d")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
            # Currency via info (può fallire silently)
            currency = "USD"
            try:
                currency = t.info.get("currency") or "USD"
            except Exception:
                pass
            return price, currency
        # Tentativo 3: info
        info = t.info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        currency = info.get("currency") or "USD"
        if price and price > 0:
            return float(price), currency
        return None, None
    except Exception as e:
        print(f"  [yf error] {ticker_original} (yf:{yf_ticker}): {e}")
        return None, None


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print(f"[update_prices] {len(ALL_TICKERS)} ticker da aggiornare...")
    
    now_iso = datetime.now(timezone.utc).isoformat()
    updated_prices_count = 0
    updated_holdings_count = 0
    
    # Recupera prezzi e salva su tabella prices
    prices_data = {}
    for i, ticker in enumerate(ALL_TICKERS, 1):
        print(f"  [{i}/{len(ALL_TICKERS)}] {ticker}...", end=" ", flush=True)
        price, currency = fetch_price_yf(ticker)
        if price is None:
            print("FAIL")
            continue
        prices_data[ticker] = price
        print(f"{currency} {price:.2f}")
        
        # Salva o aggiorna in tabella prices
        try:
            supabase.table("prices").upsert({
                "ticker": ticker,
                "price": price,
                "currency": currency,
                "source": "yfinance",
                "created_at": now_iso,
            }, on_conflict="ticker").execute()
            updated_prices_count += 1
        except Exception as e:
            print(f"    [prices save error] {e}")
    
    print(f"\n[update_prices] Aggiornati {updated_prices_count}/{len(ALL_TICKERS)} prezzi.")
    
    # Aggiorna anche current_value degli holdings esistenti
    res = supabase.table("holdings").select("id, ticker, quantity").execute()
    holdings = res.data or []
    for h in holdings:
        ticker = h.get("ticker")
        if ticker not in prices_data:
            continue
        new_value = float(h.get("quantity", 0)) * prices_data[ticker]
        try:
            supabase.table("holdings").update({
                "current_value": new_value,
            }).eq("id", h["id"]).execute()
            updated_holdings_count += 1
        except Exception as e:
            print(f"  [holdings save error] {h['id']}: {e}")
    
    print(f"[update_prices] Aggiornati {updated_holdings_count} holdings.")


if __name__ == "__main__":
    main()
