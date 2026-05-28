"""
Pulisce le IPO in `ipos_live` SENZA cancellarle:
1. Per ogni IPO la cui descrizione `business` inizia col nome dell'azienda
   (es. "Databricks — ...", "Waymo: ..."), rimuove quel prefisso lasciando
   solo la descrizione pura.
2. Rimuove il duplicato Revolut (elimina il record RVMD, tiene REVO).

NON cancella né ricrea l'intera tabella. Modifica solo i record necessari.

Variabili d'ambiente richieste: SUPABASE_URL, SUPABASE_KEY
"""

import os
import re
import sys
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠ SUPABASE_URL o SUPABASE_KEY non configurati")
    sys.exit(1)

# Ticker del duplicato Revolut da eliminare (teniamo REVO).
DUPLICATE_TICKER_TO_DELETE = "RVMD"


def strip_name_prefix(name, business):
    """Se business inizia con il nome azienda seguito da un separatore
    (—, -, :, ,), restituisce business senza quel prefisso. Altrimenti None."""
    if not name or not business:
        return None
    b = business.strip()
    n = name.strip()
    # Confronto case-insensitive sull'inizio
    if not b.lower().startswith(n.lower()):
        return None
    rest = b[len(n):].lstrip()
    # Rimuove un eventuale separatore iniziale (— – - : , .)
    rest = re.sub(r"^[\u2014\u2013\-:,.]+\s*", "", rest)
    rest = rest.strip()
    # Sicurezza: non lasciare una stringa vuota
    if not rest:
        return None
    return rest


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("=" * 60)
    print("PULIZIA ipos_live (no cancellazioni di massa)")
    print("=" * 60)

    rows = (supabase.table("ipos_live").select("ticker, name, business").execute().data) or []
    print(f"\nIPO totali: {len(rows)}")

    # 1) Pulizia descrizioni che iniziano col nome
    fixed = 0
    for r in rows:
        ticker = r.get("ticker")
        name = r.get("name") or ""
        business = r.get("business") or ""
        cleaned = strip_name_prefix(name, business)
        if cleaned and cleaned != business:
            try:
                supabase.table("ipos_live").update({"business": cleaned}).eq("ticker", ticker).execute()
                print(f"  ✓ {ticker:6} descrizione ripulita")
                fixed += 1
            except Exception as e:
                print(f"  ✗ {ticker:6} errore update: {str(e)[:120]}")
    print(f"\nDescrizioni corrette: {fixed}")

    # 2) Rimozione duplicato Revolut
    print("\nRimozione duplicato Revolut...")
    try:
        existing = supabase.table("ipos_live").select("ticker").eq("ticker", DUPLICATE_TICKER_TO_DELETE).execute().data or []
        if existing:
            supabase.table("ipos_live").delete().eq("ticker", DUPLICATE_TICKER_TO_DELETE).execute()
            print(f"  ✓ {DUPLICATE_TICKER_TO_DELETE} eliminato (duplicato di REVO)")
        else:
            print(f"  · {DUPLICATE_TICKER_TO_DELETE} non presente, niente da fare")
    except Exception as e:
        print(f"  ✗ errore eliminazione duplicato: {str(e)[:120]}")

    print("\n✓ Completato.")


if __name__ == "__main__":
    main()
