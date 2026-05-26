"""
Discover Hardcoded Tickers — estrae tutti i ticker hardcoded da src/App.jsx
e li testa con yfinance per categorizzarli:

  A) WORKING: yfinance restituisce dati → da aggiungere al backfill
  B) FAILED:  yfinance non riconosce → fondi italiani/proprietari, gestire a UI

Esecuzione: python discover_hardcoded_tickers.py
Output: stampa risultati + salva CSV /tmp/hardcoded_tickers_classified.csv
"""

import re
import csv
import time
import os
import yfinance as yf

# Path dell'App.jsx — assumiamo sia nel repo, in src/ (modifica se altrove)
APPJSX_CANDIDATES = ["src/App.jsx", "App.jsx", "frontend/src/App.jsx"]


def find_appjsx():
    for p in APPJSX_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def extract_tickers_from_appjsx(path):
    """Estrae tutti i valori dopo 'ticker: "..."' nel file."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # Pattern: ticker: "XXX"  (con o senza spazi)
    matches = re.findall(r'ticker:\s*"([^"]+)"', content)
    return sorted(set(matches))


def test_yfinance(ticker, max_retries=1):
    """Veloce: prova history 5 giorni. Se ritorna almeno 1 riga → working."""
    for attempt in range(max_retries + 1):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if not hist.empty:
                return True, len(hist), float(hist["Close"].iloc[-1])
            return False, 0, None
        except Exception:
            if attempt == max_retries:
                return False, 0, None
            time.sleep(0.3)


def main():
    print("=" * 70)
    print("DISCOVER HARDCODED TICKERS — estrazione + classificazione")
    print("=" * 70)

    path = find_appjsx()
    if not path:
        print(f"⚠ App.jsx non trovato. Cercato in: {APPJSX_CANDIDATES}")
        return
    print(f"📄 Sorgente: {path}")

    tickers = extract_tickers_from_appjsx(path)
    print(f"📊 Ticker estratti: {len(tickers)}")
    print()

    working = []
    failed = []

    for i, tk in enumerate(tickers, 1):
        ok, n_rows, last_close = test_yfinance(tk)
        if ok:
            working.append({"ticker": tk, "n_rows": n_rows, "last_close": last_close})
            print(f"  [{i:>3}/{len(tickers)}] ✓ {tk:<14} (close: {last_close})")
        else:
            failed.append({"ticker": tk})
            print(f"  [{i:>3}/{len(tickers)}] ✗ {tk:<14} (no data)")
        time.sleep(0.1)

    # CSV
    csv_path = "/tmp/hardcoded_tickers_classified.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "status", "n_rows", "last_close"])
        for w in working:
            writer.writerow([w["ticker"], "WORKING", w["n_rows"], w["last_close"]])
        for fl in failed:
            writer.writerow([fl["ticker"], "FAILED", "", ""])
    print(f"\n✓ CSV salvato in {csv_path}")

    print()
    print(f"📊 RIEPILOGO:")
    print(f"   ✓ Working (yfinance ok):     {len(working)}")
    print(f"   ✗ Failed (no Yahoo data):    {len(failed)}")
    print()

    # Stampa lista Python ready-to-paste per il backfill
    print("=" * 70)
    print("LISTA WORKING — da aggiungere a BENCHMARK_TICKERS in backfill_prices_history.py")
    print("=" * 70)
    print()
    working_tickers = sorted([w["ticker"] for w in working])
    # Print in batch di 4 per riga
    for i in range(0, len(working_tickers), 4):
        batch = working_tickers[i:i+4]
        print("    " + ", ".join(f'"{t}"' for t in batch) + ",")

    print()
    print("=" * 70)
    print("LISTA FAILED — ticker proprietari/non-Yahoo (gestire a UI)")
    print("=" * 70)
    print()
    for fl in failed:
        print(f"  · {fl['ticker']}")


if __name__ == "__main__":
    main()
