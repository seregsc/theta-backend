"""
Ticker Discovery — per ogni ticker fallito (delisted/non riconosciuto da yfinance),
prova varianti automatiche e suggerisce il sostituto migliore.

Strategia:
1. Per ogni ticker fallito, estrae il "base" (parte prima del suffisso)
2. Prova varianti con suffissi diversi: .L, .DE, .MI, .PA, .AS, .SW, .F, .HE, .IR e senza suffisso
3. Per ogni variante valida (yfinance la riconosce con storico >0), ne registra:
   - n_rows ottenuti in 5y
   - last_close (prezzo recente, verifica che sia "vivo")
   - currency (info da yfinance)
4. Restituisce un report ordinato: ticker_originale → migliore alternativa
5. NON modifica nulla nel DB — produce solo un CSV/print per revisione manuale

Esecuzione: python ticker_discovery.py
Output: stampa tabella e salva /tmp/ticker_discovery_results.csv
"""

import csv
import time
import yfinance as yf

# I 28 ticker che falliscono sistematicamente nel backfill
FAILED_TICKERS = [
    "ABTC.MI", "AETH.MI", "AGED.DE", "AGGS.MI", "AGRI.MI", "BOTZ.DE",
    "BTPS.MI", "CHIP.DE", "CNDX.MI", "CPXJ.MI", "CSPX.MI", "CSX5.MI",
    "DAXEX.DE", "DFEN.MI", "HEAL.DE", "IDTM.MI", "IJPA.MI", "INRG.DE",
    "IUVF.MI", "LUXE.MI", "SAWD.DE", "SUEU.MI", "URA.MI", "XAIQ.MI",
    "XBTI.MI", "XDAX.DE", "XMJP.DE", "XMWO.DE",
]

# Suffissi da provare, in ordine di "italianità" (preferiamo .MI poi exchange UE)
SUFFIXES_TO_TRY = [".MI", ".L", ".DE", ".PA", ".AS", ".SW", ".F", ".HE", ".IR", ""]


def get_base(ticker: str) -> str:
    """Estrae il codice base senza suffisso exchange."""
    if "." in ticker:
        return ticker.rsplit(".", 1)[0]
    return ticker


def test_variant(candidate: str) -> dict | None:
    """Testa un ticker yfinance. Ritorna info utili o None se non riconosciuto."""
    try:
        t = yf.Ticker(candidate)
        hist = t.history(period="5y", interval="1d", auto_adjust=False)
        if hist.empty:
            return None
        # Verifica che sia "vivo" (ultimo close negli ultimi 90 giorni)
        last_date = hist.index[-1]
        last_close = float(hist["Close"].iloc[-1])
        # Tenta di leggere la currency da info (best effort, può fallire)
        currency = None
        try:
            info = t.fast_info
            currency = info.get("currency") if hasattr(info, "get") else getattr(info, "currency", None)
        except Exception:
            pass
        return {
            "ticker": candidate,
            "n_rows": len(hist),
            "last_close": round(last_close, 4),
            "last_date": last_date.strftime("%Y-%m-%d"),
            "currency": currency or "?",
        }
    except Exception:
        return None


def discover_for_ticker(failed_ticker: str) -> list[dict]:
    """Per il ticker fallito, prova tutte le varianti e ritorna le valide."""
    base = get_base(failed_ticker)
    original_suffix = "." + failed_ticker.rsplit(".", 1)[1] if "." in failed_ticker else ""
    candidates = []
    for suffix in SUFFIXES_TO_TRY:
        if suffix == original_suffix:
            continue  # skippa il suffisso che sappiamo non funzionare
        candidate = base + suffix if suffix else base
        result = test_variant(candidate)
        if result:
            candidates.append(result)
        time.sleep(0.15)  # politeness yfinance
    return candidates


def main():
    print("=" * 70)
    print("TICKER DISCOVERY — cerco alternative per i ticker delisted")
    print("=" * 70)
    print(f"Ticker da analizzare: {len(FAILED_TICKERS)}")
    print()

    results = []

    for i, ticker in enumerate(FAILED_TICKERS, 1):
        print(f"[{i:2}/{len(FAILED_TICKERS)}] {ticker} → cerco varianti…")
        candidates = discover_for_ticker(ticker)
        if candidates:
            # Ordina: priorità a quelli con più storico
            candidates.sort(key=lambda c: c["n_rows"], reverse=True)
            best = candidates[0]
            print(f"           ✓ MIGLIORE: {best['ticker']} ({best['currency']}) — {best['n_rows']} righe, ultimo {best['last_date']} @ {best['last_close']}")
            if len(candidates) > 1:
                alts = ", ".join(f"{c['ticker']} ({c['n_rows']}r)" for c in candidates[1:4])
                print(f"           · alternative: {alts}")
            results.append({
                "original": ticker,
                "best_alt": best["ticker"],
                "currency": best["currency"],
                "n_rows": best["n_rows"],
                "last_date": best["last_date"],
                "last_close": best["last_close"],
                "all_candidates": "; ".join(c["ticker"] for c in candidates),
            })
        else:
            print(f"           ✗ NESSUNA variante trovata")
            results.append({
                "original": ticker,
                "best_alt": "",
                "currency": "",
                "n_rows": 0,
                "last_date": "",
                "last_close": "",
                "all_candidates": "",
            })

    # Salva CSV
    csv_path = "/tmp/ticker_discovery_results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "original", "best_alt", "currency", "n_rows", "last_date", "last_close", "all_candidates"
        ])
        writer.writeheader()
        writer.writerows(results)
    print(f"\n✓ CSV salvato in {csv_path}")

    # Summary
    found = sum(1 for r in results if r["best_alt"])
    not_found = len(results) - found
    print()
    print(f"📊 Riepilogo:")
    print(f"   • Alternative trovate: {found}/{len(FAILED_TICKERS)}")
    print(f"   • Nessuna alternativa: {not_found}")
    print()
    print("Tabella riassuntiva (da rivedere a mano prima di applicare):")
    print()
    print(f"  {'ORIGINALE':<14} → {'MIGLIORE':<14} {'CUR':<5} {'RIGHE':<7} {'ULTIMO':<12} {'PREZZO':<10}")
    print(f"  {'-'*14}   {'-'*14} {'-'*5} {'-'*7} {'-'*12} {'-'*10}")
    for r in results:
        if r["best_alt"]:
            print(f"  {r['original']:<14} → {r['best_alt']:<14} {r['currency']:<5} {r['n_rows']:<7} {r['last_date']:<12} {r['last_close']}")
        else:
            print(f"  {r['original']:<14} → {'???':<14} (nessuna variante trovata)")


if __name__ == "__main__":
    main()
