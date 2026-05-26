"""
Audit Tickers — diagnostica quali ticker esistono nelle tabelle ma NON hanno storico.

Confronta:
- Set A: ticker DISTINCT da holdings, etf_catalog, opportunities (più ovunque ci sia un ticker)
- Set B: ticker DISTINCT in prices_history (lo storico effettivo)
- A - B = ticker che dovrebbero avere storico ma non ce l'hanno

Classifica i mancanti per pattern:
- Suffisso Yahoo standard (.MI .DE .L .PA .AS ...) → probabile correggibile
- ISIN (12 char alfanumerici, formato es. IE00B4L5Y983) → fondi/obbligazioni
- No suffisso, formato US (1-5 lettere) → US stock, dovrebbe funzionare
- Pattern strano (numerico, troppi/troppo pochi caratteri) → ticker custom

Esecuzione: python audit_tickers.py
Output: stampa report e salva CSV in /tmp/audit_tickers_results.csv
"""

import os
import re
import csv
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")


def fetch_distinct_tickers(supabase, table, column):
    """Legge tutti i ticker distinti da una tabella, paginato."""
    tickers = set()
    offset = 0
    page_size = 1000
    while True:
        try:
            result = supabase.table(table).select(column).range(offset, offset + page_size - 1).execute()
        except Exception as e:
            print(f"  ⚠ {table}.{column}: {str(e)[:100]}")
            return tickers
        if not result.data:
            break
        for row in result.data:
            t = row.get(column)
            if t:
                tickers.add(t.strip())
        if len(result.data) < page_size:
            break
        offset += page_size
    return tickers


def classify_ticker(t: str) -> str:
    """Classifica un ticker in una categoria per capire perché potrebbe non funzionare."""
    t = t.strip()
    if not t:
        return "vuoto"
    # ISIN: 12 char, prima 2 lettere, poi 9 alfanum, poi 1 cifra
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", t):
        return "ISIN (fondo/obbligazione)"
    # Suffisso Yahoo
    if "." in t:
        suffix = "." + t.rsplit(".", 1)[1]
        known = {".MI", ".DE", ".F", ".PA", ".AS", ".BR", ".LS", ".MC", ".VI", ".HE", ".IR",
                 ".L", ".SW", ".TO", ".V", ".HK", ".T", ".KS", ".AX", ".NS", ".BO", ".SA"}
        if suffix in known:
            return f"Yahoo suffisso {suffix}"
        return f"Suffisso sconosciuto {suffix}"
    # Indici Yahoo
    if t.startswith("^"):
        return "Indice Yahoo (^)"
    # Crypto/Futures/Forex
    if "-USD" in t or "-EUR" in t:
        return "Crypto pair"
    if t.endswith("=F"):
        return "Futures (=F)"
    if t.endswith("=X"):
        return "Forex (=X)"
    # US ticker (1-5 lettere)
    if re.fullmatch(r"[A-Z]{1,5}", t):
        return "US ticker (no suffisso)"
    # Numerico o misto strano
    if re.search(r"\d", t):
        return "Pattern con numeri (custom?)"
    return "Altro/sconosciuto"


def main():
    print("=" * 70)
    print("AUDIT TICKERS — confronto catalogo vs storico effettivo")
    print("=" * 70)

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠ SUPABASE_URL o SUPABASE_KEY mancanti")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 1. Tutti i ticker che L'APP USA (da varie tabelle)
    print("\n📥 Fetching ticker dalle tabelle di catalogo...")
    holdings_t = fetch_distinct_tickers(supabase, "holdings", "ticker")
    print(f"   holdings:       {len(holdings_t):>4} ticker")
    etf_t = fetch_distinct_tickers(supabase, "etf_catalog", "ticker")
    print(f"   etf_catalog:    {len(etf_t):>4} ticker")
    opp_t = fetch_distinct_tickers(supabase, "opportunities", "ticker")
    print(f"   opportunities:  {len(opp_t):>4} ticker")

    all_catalog = holdings_t | etf_t | opp_t
    print(f"   UNION distinct: {len(all_catalog):>4} ticker")

    # 2. Ticker che hanno effettivamente storico
    print("\n📥 Fetching ticker da prices_history...")
    history_t = fetch_distinct_tickers(supabase, "prices_history", "ticker")
    print(f"   prices_history: {len(history_t):>4} ticker")

    # 3. Diff
    missing = sorted(all_catalog - history_t)
    extra = sorted(history_t - all_catalog)  # ticker storici "orfani"

    print()
    print(f"📊 DIFFERENZE:")
    print(f"   ✗ Ticker NEL catalogo MA SENZA storico: {len(missing)}")
    print(f"   · Ticker NELLO storico ma fuori catalogo: {len(extra)} (benchmark + ticker rimossi)")

    # 4. Classifica i mancanti
    classification = {}
    for t in missing:
        cat = classify_ticker(t)
        classification.setdefault(cat, []).append(t)

    print()
    print(f"🔍 CLASSIFICAZIONE TICKER MANCANTI ({len(missing)} totali):")
    print()
    for cat in sorted(classification.keys(), key=lambda k: -len(classification[k])):
        ticks = classification[cat]
        print(f"  [{len(ticks):>3}] {cat}")
        # Mostra primi 8 esempi
        sample = ticks[:8]
        print(f"        es: {', '.join(sample)}{' …' if len(ticks) > 8 else ''}")

    # 5. Quale tabella ha più contributo ai mancanti?
    print()
    print("📍 Provenienza dei mancanti (può essere in più tabelle):")
    from_holdings = sum(1 for t in missing if t in holdings_t)
    from_etf = sum(1 for t in missing if t in etf_t)
    from_opp = sum(1 for t in missing if t in opp_t)
    print(f"   · da holdings:      {from_holdings}")
    print(f"   · da etf_catalog:   {from_etf}")
    print(f"   · da opportunities: {from_opp}")

    # 6. Salva CSV
    csv_path = "/tmp/audit_tickers_results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "category", "in_holdings", "in_etf_catalog", "in_opportunities"])
        for t in missing:
            writer.writerow([
                t,
                classify_ticker(t),
                "Y" if t in holdings_t else "",
                "Y" if t in etf_t else "",
                "Y" if t in opp_t else "",
            ])
    print(f"\n✓ CSV salvato in {csv_path}")

    # 7. Mostra primi 30 ticker mancanti in chiaro
    print()
    print(f"🔍 Primi 30 ticker mancanti (per ispezione veloce):")
    for t in missing[:30]:
        sources = []
        if t in holdings_t: sources.append("hold")
        if t in etf_t: sources.append("etf")
        if t in opp_t: sources.append("opp")
        print(f"  {t:<22} [{classify_ticker(t):<25}] ← {','.join(sources)}")
    if len(missing) > 30:
        print(f"  … e altri {len(missing) - 30} (vedi CSV)")


if __name__ == "__main__":
    main()
