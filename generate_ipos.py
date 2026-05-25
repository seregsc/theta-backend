"""
generate_ipos.py
Pesca le IPO upcoming dai prossimi 6 mesi e quelle recenti (ultimi 30 giorni)
da Financial Modeling Prep, e usa Claude per arricchire ognuna con analisi
finanziaria (tesi, pros, cons, target, valuation, ecc.).

Salva su ipos_live (UPSERT su ticker+ipo_date).
Da eseguire settimanalmente via GitHub Actions.
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone, date
from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
FMP_API_KEY = os.environ.get("FMP_API_KEY")

MODEL = "claude-sonnet-4-5"

# Finestra temporale: 30 giorni nel passato + 180 giorni nel futuro
DAYS_BACK = 30
DAYS_FORWARD = 180

# Numero massimo di IPO da arricchire (per non sprecare token AI)
# Le più interessanti vengono filtrate per offer size / market cap
MAX_IPOS_TO_ENRICH = 30


def fetch_fmp_ipos():
    """Pesca IPO calendar da FMP (free tier, 250 calls/day)"""
    today = date.today()
    date_from = (today - timedelta(days=DAYS_BACK)).isoformat()
    date_to = (today + timedelta(days=DAYS_FORWARD)).isoformat()
    url = f"https://financialmodelingprep.com/api/v3/ipo_calendar?from={date_from}&to={date_to}&apikey={FMP_API_KEY}"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            print(f"[FMP] errore HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        if isinstance(data, dict) and data.get("Error Message"):
            print(f"[FMP] errore API: {data['Error Message']}")
            return []
        return data or []
    except Exception as e:
        print(f"[FMP] errore connessione: {e}")
        return []


def normalize_fmp_ipo(item):
    """Normalizza un record FMP nel nostro schema."""
    symbol = (item.get("symbol") or "").strip()
    name = (item.get("company") or "").strip()
    exchange = (item.get("exchange") or "").strip()
    ipo_date_str = (item.get("date") or "").strip()
    price_range = (item.get("priceRange") or "").strip()
    shares = item.get("shares")
    market_cap = item.get("marketCap")
    actions = (item.get("actions") or "").lower()

    # Determina status
    today = date.today()
    try:
        ipo_d = datetime.fromisoformat(ipo_date_str).date()
    except Exception:
        ipo_d = None

    if "priced" in actions:
        status = "priced"
    elif ipo_d:
        if ipo_d < today:
            status = "recent" if (today - ipo_d).days <= 30 else "past"
        elif (ipo_d - today).days <= 7:
            status = "upcoming"
        else:
            status = "pre-ipo"
    else:
        status = "pre-ipo"

    # Geo dal exchange
    geo_map = {
        "NASDAQ": "USA", "NYSE": "USA", "AMEX": "USA",
        "LSE": "EU", "LON": "EU", "EURONEXT": "EU", "ENXT": "EU",
        "XETRA": "EU", "MIL": "EU", "BIT": "EU",
        "HKEX": "Asia", "HK": "Asia", "TSE": "Asia", "JP": "Asia",
        "TSX": "Canada", "TO": "Canada",
    }
    geo = "USA"
    for k, v in geo_map.items():
        if k in exchange.upper():
            geo = v
            break

    expected_date_label = ipo_d.strftime("%d %b %Y") if ipo_d else "TBD"

    return {
        "ticker": symbol,
        "name": name,
        "exchange": exchange,
        "geo": geo,
        "ipo_date": ipo_date_str if ipo_d else None,
        "expected_date": expected_date_label,
        "price_range": price_range,
        "shares_offered": int(shares) if shares else None,
        "market_cap_estimate": int(market_cap) if market_cap else None,
        "status": status,
        "raw_data": item,
    }


def fetch_existing_ipos(supabase):
    """Ritorna dict {ticker: row} per le IPO già nel DB (per evitare ri-enrichment costoso)."""
    try:
        res = supabase.table("ipos_live").select("ticker, ipo_date, enriched_at, status").execute()
        out = {}
        for row in (res.data or []):
            key = f"{row['ticker']}_{row.get('ipo_date') or 'TBD'}"
            out[key] = row
        return out
    except Exception as e:
        print(f"[existing fetch] {e}")
        return {}


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3].strip()
        elif "```" in text:
            text = text[:text.rfind("```")].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception as e:
        print(f"  parse error: {e}")
        print(f"  first 300: {text[:300]}")
        return None


def enrich_ipo_with_ai(anthropic_client, ipo_basic):
    """Chiede a Claude di arricchire un'IPO con analisi finanziaria approfondita."""
    prompt = f"""Sei un equity research analyst senior italiano. Devi arricchire i dati grezzi di un'IPO con un'analisi finanziaria approfondita per un consulente Fineco italiano.

═══════════════════════════════════
DATI GREZZI DELL'IPO
═══════════════════════════════════
Ticker: {ipo_basic.get('ticker', '—')}
Nome: {ipo_basic.get('name', '—')}
Exchange: {ipo_basic.get('exchange', '—')}
Geo: {ipo_basic.get('geo', '—')}
Data IPO: {ipo_basic.get('expected_date', 'TBD')}
Range prezzo: {ipo_basic.get('price_range', '—')}
Status: {ipo_basic.get('status', '—')}
Azioni offerte: {ipo_basic.get('shares_offered', '—')}
Market Cap stimato: {ipo_basic.get('market_cap_estimate', '—')}

═══════════════════════════════════
ISTRUZIONI
═══════════════════════════════════
Genera un'analisi completa basandoti sulla tua conoscenza dell'azienda e del settore.

Se NON CONOSCI questa azienda specifica (potrebbe essere una micro-cap o una società minore recentemente filed), restituisci comunque un'analisi basata sul nome/settore/exchange con disclaimer chiaro nella thesis. NON inventare dati finanziari precisi che non conosci.

Settori (slug interno): ai, semis, defense, nuclear, biotech, fintech, luxury, auto, energy, utilities, consumer, industrials, telecom, reits, emerging, gold, copper, crypto, health, real_estate, other.

Profili (suited_for): conservative, balanced, growth, aggressive.

NIENTE jargon vuoto. Lessico semplice. Frasi brevi.

═══════════════════════════════════
FORMATO OUTPUT (SOLO JSON)
═══════════════════════════════════
{{
  "sector": "AI Infrastructure",
  "category": "ai",
  "headline": "Prima IPO pura-play AI cloud (max 70 caratteri)",
  "summary": "1-2 frasi cosa fa l'azienda",
  "business": "2-3 frasi modello di business",
  "thesis": "4-6 frasi sulla tesi di investimento, con cenni a rischi e opportunità. Cita numeri concreti dove possibile. Se l'azienda non è ben nota, dichiara onestamente i limiti dell'analisi.",
  "pros": ["Punto 1 (1 riga)", "Punto 2", "Punto 3", "Punto 4"],
  "cons": ["Rischio 1", "Rischio 2", "Rischio 3"],
  "catalysts": ["Cosa potrebbe muovere il prezzo nei prossimi mesi 1", "2", "3"],
  "competitors": ["Concorrente 1", "Concorrente 2", "Concorrente 3"],
  "comparables": [
    {{"name":"Snowflake","ticker":"SNOW","ipo_date":"Set 2020","ipo_price":"$120","perf_first_day":"+111%","perf_1y":"+38%","note":"Comparable settoriale"}}
  ],
  "use_of_proceeds": "Come useranno il capitale raccolto",
  "founders": ["Nome Cognome (CEO)"],
  "employees": 1100,
  "founded": 2017,
  "revenue_growth": "+186%",
  "gross_margin": "62%",
  "ebitda_margin": "31%",
  "debt_eq": "1.8x",
  "valuation_method": "EV/Revenue 15x su run-rate $2.3B",
  "last_round": "$23B (Mag 2024, Coatue)",
  "score": 88,
  "upside": "+52%",
  "risk": "MED",
  "rating": "BUY",
  "target": "$53B val",
  "offer_size": "$2.5B",
  "lead_underwriters": ["Morgan Stanley", "Goldman Sachs"],
  "suited_for": ["growth", "aggressive"]
}}

IMPORTANTE: se non conosci dati precisi, scrivi "—" o null invece di inventare numeri.
Lo `score` va da 0 a 100 e riflette quanto questa IPO è interessante in termini di rischio/rendimento.
`rating` è BUY, HOLD o AVOID. Se non sei sicuro, metti HOLD.
`risk` è LOW/MED/HIGH."""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=3500,
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_json(response.content[0].text.strip())
    except Exception as e:
        print(f"  [AI error] {e}")
        return None


def merge_ipo(basic, enriched):
    """Combina dati grezzi FMP + arricchimento AI nello schema finale del DB."""
    merged = {**basic}
    if enriched:
        for key in ["sector", "category", "headline", "summary", "business", "thesis",
                    "pros", "cons", "catalysts", "competitors", "comparables",
                    "use_of_proceeds", "founders", "employees", "founded",
                    "revenue_growth", "gross_margin", "ebitda_margin", "debt_eq",
                    "valuation_method", "last_round", "score", "upside", "risk",
                    "rating", "target", "offer_size", "lead_underwriters", "suited_for"]:
            if enriched.get(key) is not None:
                merged[key] = enriched[key]
    merged["enriched_at"] = datetime.now(timezone.utc).isoformat()
    return merged


def save_ipo(supabase, row):
    """UPSERT su (ticker, ipo_date)"""
    try:
        # Converti raw_data e jsonb fields a JSON serializzabili
        for k in ["pros", "cons", "catalysts", "competitors", "comparables", "founders",
                  "lead_underwriters", "suited_for", "raw_data"]:
            if k in row and row[k] is not None and not isinstance(row[k], (list, dict)):
                row[k] = None
        # Rimuovi campi None opzionali per essere robusti
        clean = {k: v for k, v in row.items() if v is not None}
        # NULL ipo_date diventa "1970-01-01" per la UNIQUE constraint
        if "ipo_date" not in clean:
            clean["ipo_date"] = None
        clean["updated_at"] = datetime.now(timezone.utc).isoformat()
        supabase.table("ipos_live").upsert(clean, on_conflict="ticker,ipo_date").execute()
        return True
    except Exception as e:
        print(f"  [save error] {row.get('ticker')}: {e}")
        return False


def cleanup_old_ipos(supabase):
    """Rimuove IPO troppo vecchie (oltre 90 giorni nel passato)."""
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    try:
        res = supabase.table("ipos_live").delete().lt("ipo_date", cutoff).execute()
        print(f"[cleanup] vecchie IPO rimosse")
    except Exception as e:
        print(f"[cleanup error] {e}")


def main():
    if not FMP_API_KEY:
        print("[!] FMP_API_KEY mancante. Termino.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("[ipos] Avvio fetch IPO calendar...")
    raw_ipos = fetch_fmp_ipos()
    print(f"[ipos] {len(raw_ipos)} record grezzi da FMP")

    if not raw_ipos:
        print("[ipos] Nessun record. Termino.")
        return

    # Normalizza
    normalized = []
    for item in raw_ipos:
        try:
            n = normalize_fmp_ipo(item)
            if n.get("ticker") and n.get("name"):
                normalized.append(n)
        except Exception as e:
            print(f"  [normalize error] {e}")

    print(f"[ipos] {len(normalized)} dopo normalizzazione")

    # Ordina: priorità a IPO con market cap più alto + priced/upcoming
    def sort_key(n):
        status_priority = {"upcoming": 0, "priced": 1, "recent": 2, "pre-ipo": 3, "past": 4}
        return (
            status_priority.get(n.get("status"), 5),
            -(n.get("market_cap_estimate") or 0),
            n.get("ipo_date") or "9999",
        )
    normalized.sort(key=sort_key)

    # Limita per non sprecare AI
    to_process = normalized[:MAX_IPOS_TO_ENRICH]
    print(f"[ipos] {len(to_process)} verranno arricchite con AI")

    # Carica IPO già nel DB per skip su quelle già arricchite recentemente
    existing = fetch_existing_ipos(supabase)

    success = 0
    skipped = 0

    for i, basic in enumerate(to_process, 1):
        ticker = basic.get("ticker")
        ipo_date = basic.get("ipo_date") or "TBD"
        key = f"{ticker}_{ipo_date}"

        # Skip se già arricchita di recente (< 7 giorni) e status non cambiato
        if key in existing:
            existing_row = existing[key]
            old_status = existing_row.get("status")
            enriched_at = existing_row.get("enriched_at")
            if enriched_at and old_status == basic.get("status"):
                try:
                    enr_dt = datetime.fromisoformat(enriched_at.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - enr_dt).days < 7:
                        skipped += 1
                        continue
                except Exception:
                    pass

        print(f"  [{i}/{len(to_process)}] Arricchisco {ticker} ({basic.get('name', '')[:40]})...")
        enriched = enrich_ipo_with_ai(anthropic_client, basic)
        if not enriched:
            print(f"    AI fallita, salvo solo dati base")
            row = basic
            row["enriched_at"] = None
        else:
            row = merge_ipo(basic, enriched)

        if save_ipo(supabase, row):
            success += 1
            cat = row.get("category", "—")
            sc = row.get("score", "—")
            print(f"    OK: {cat}, score {sc}")
        # Piccola pausa per non saturare l'API
        time.sleep(0.3)

    # Cleanup IPO molto vecchie
    cleanup_old_ipos(supabase)

    print(f"\n[ipos] Completato: {success}/{len(to_process)} salvate, {skipped} skipped.")


if __name__ == "__main__":
    main()
