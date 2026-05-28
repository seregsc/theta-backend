"""
Elenca le IPO presenti in `ipos_live` su Supabase. NON modifica nulla.
Serve solo a capire quante e quali IPO ci sono prima di decidere come rigenerarle.

Variabili d'ambiente richieste: SUPABASE_URL, SUPABASE_KEY
"""

import os
import sys
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠ SUPABASE_URL o SUPABASE_KEY non configurati")
    sys.exit(1)


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = supabase.table("ipos_live").select("ticker, name, sector, status, tier, business").order("tier").execute()
    rows = res.data or []

    print("=" * 60)
    print(f"IPO PRESENTI IN ipos_live: {len(rows)}")
    print("=" * 60)

    for r in rows:
        biz = (r.get("business") or "")
        # Segnala se la descrizione inizia col nome dell'azienda (es. "Databricks —")
        starts_with_name = ""
        name = r.get("name") or ""
        if name and biz.strip().lower().startswith(name.strip().lower()):
            starts_with_name = "  ⚠ business inizia col nome"
        print(f"  [{r.get('tier','?')}] {r.get('ticker',''):6} {name:24} {r.get('status','')}{starts_with_name}")

    print()
    print("Copia tutto questo output e incollalo nella chat.")


if __name__ == "__main__":
    main()
