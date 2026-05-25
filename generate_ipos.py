"""
generate_ipos.py
Pesca IPO da Finnhub + arricchimento AI Claude.
Salva su ipos_live (UPSERT su ticker+ipo_date).
Settimanale via GitHub Actions.

Versione con flush immediato + timeout robusti per debugging GitHub Actions.
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone, date
from supabase import create_client
import anthropic

# Forza unbuffered output: ogni print() appare subito nei log GitHub
print("[boot] generate_ipos.py avvio", flush=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

print(f"[boot] env check: SUPABASE_URL={'OK' if SUPABASE_URL else 'MANCANTE'}, "
      f"SUPABASE_KEY={'OK' if SUPABASE_KEY else 'MANCANTE'}, "
      f"ANTHROPIC_API_KEY={'OK' if ANTHROPIC_API_KEY else 'MANCANTE'}, "
      f"FINNHUB_API_KEY={'OK' if FINNHUB_API_KEY else 'MANCANTE'}", flush=True)

MODEL = "claude-sonnet-4-5"

DAYS_BACK = 30
DAYS_FORWARD = 180
MAX_IPOS_TO_ENRICH = 15  # ridotto da 30 per velocità

# Timeout
HTTP_TIMEOUT = 20
AI_TIMEOUT = 60


def fetch_finnhub_ipos():
    """Pesca IPO calendar da Finnhub (free tier 60 req/min)."""
    today = date.today()
    date_from = (today - timedelta(days=DAYS_BACK)).isoformat()
    date_to = (today + timedelta(days=DAYS_FORWARD)).isoformat()
    url = f"https://finnhub.io/api/v1/calendar/ipo?from={date_from}&to={date_to}&token={FINNHUB_API_KEY}"
    print(f"[Finnhub] GET {url[:80]}...", flush=True)
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        print(f"[Finnhub] HTTP {r.status_code} in {r.elapsed.total_seconds():.1f}s", flush=True)
        if r.status_code != 200:
            print(f"[Finnhub] errore body: {r.text[:300]}", flush=True)
            return []
        data = r.json()
        if isinstance(data, dict):
            if "ipoCalendar" in data and isinstance(data["ipoCalendar"], list):
                return data["ipoCalendar"]
            if data.get("error"):
                print(f"[Finnhub] errore API: {data['error']}", flush=True)
                return []
            print(f"[Finnhub] formato inatteso (dict): {list(data.keys())[:5]}", flush=True)
            return []
        if isinstance(data, list):
            return data
        print(f"[Finnhub] formato inatteso: {type(data)}", flush=True)
        return []
    except requests.Timeout:
        print(f"[Finnhub] TIMEOUT dopo {HTTP_TIMEOUT}s", flush=True)
        return []
    except Exception as e:
        print(f"[Finnhub] errore connessione: {e}", flush=True)
        return []


def normalize_finnhub_ipo(item):
    """Normalizza schema Finnhub: { date, exchange, name, numberOfShares, price, status, symbol, totalSharesValue }"""
    symbol = (item.get("symbol") or "").strip()
    name = (item.get("name") or "").strip()
    exchange = (item.get("exchange") or "").strip()
    ipo_date_str = (item.get("date") or "").strip()
    price_field = (item.get("price") or "").strip()
    shares = item.get("numberOfShares")
    total_value = item.get("totalSharesValue")
    fh_status = (item.get("status") or "").lower()

    today = date.today()
    try:
        ipo_d = datetime.fromisoformat(ipo_date_str).date()
    except Exception:
        ipo_d = None

    if "withdrawn" in fh_status:
        status = "withdrawn"
    elif "priced" in fh_status:
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

    price_range = ""
    if price_field:
        if "-" in price_field:
            parts = price_field.split("-")
            try:
                lo = float(parts[0].strip())
                hi = float(parts[1].strip())
                price_range = f"${lo:.0f}-${hi:.0f}"
            except Exception:
                price_range = price_field
        else:
            try:
                p = float(price_field)
                price_range = f"${p:.0f}"
            except Exception:
                price_range = price_field

    geo_map = {
        "NASDAQ": "USA", "NYSE": "USA", "AMEX": "USA",
        "LSE": "EU", "LON": "EU", "EURONEXT": "EU",
        "XETRA": "EU", "MIL": "EU", "BIT": "EU",
        "HKEX": "Asia", "HK": "Asia", "TSE": "Asia",
        "TSX": "Canada",
    }
    geo = "USA"
    if exchange:
        ex_up = exchange.upper()
        for k, v in geo_map.items():
            if k in ex_up:
                geo = v
                break

    expected_date_label = ipo_d.strftime("%d %b %Y") if ipo_d else "TBD"
    market_cap_estimate = None
    if total_value:
        try:
            market_cap_estimate = int(total_value)
        except Exception:
            pass

    return {
        "ticker": symbol,
        "name": name,
        "exchange": exchange,
        "geo": geo,
        "ipo_date": ipo_date_str if ipo_d else None,
        "expected_date": expected_date_label,
        "price_range": price_range,
        "shares_offered": int(shares) if shares else None,
        "market_cap_estimate": market_cap_estimate,
        "status": status,
        "raw_data": item,
    }


def fetch_existing_ipos(supabase):
    try:
        res = supabase.table("ipos_live").select("ticker, ipo_date, enriched_at, status").execute()
        out = {}
        for row in (res.data or []):
            key = f"{row['ticker']}_{row.get('ipo_date') or 'TBD'}"
            out[key] = row
        return out
    except Exception as e:
        print(f"[existing fetch] {e}", flush=True)
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
        print(f"  parse error: {e}", flush=True)
        return None


def enrich_ipo_with_ai(anthropic_client, ipo_basic):
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

Se NON CONOSCI questa azienda (micro-cap, SPAC, società minore), restituisci comunque un'analisi basata sul nome/settore/exchange con disclaimer chiaro nella thesis. NON inventare dati finanziari precisi.

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
  "thesis": "4-6 frasi sulla tesi di investimento con rischi e opportunità.",
  "pros": ["Punto 1", "Punto 2", "Punto 3"],
  "cons": ["Rischio 1", "Rischio 2", "Rischio 3"],
  "catalysts": ["Catalyst 1", "Catalyst 2"],
  "competitors": ["Concorrente 1", "Concorrente 2"],
  "comparables": [
    {{"name":"Snowflake","ticker":"SNOW","ipo_date":"Set 2020","ipo_price":"$120","perf_first_day":"+111%","perf_1y":"+38%","note":"Comparable settoriale"}}
  ],
  "use_of_proceeds": "Come useranno il capitale",
  "founders": ["Nome Cognome (CEO)"],
  "employees": 1100,
  "founded": 2017,
  "revenue_growth": "+186%",
  "gross_margin": "62%",
  "ebitda_margin": "31%",
  "debt_eq": "1.8x",
  "valuation_method": "EV/Revenue 15x su run-rate $2.3B",
  "last_round": "$23B",
  "score": 88,
  "upside": "+52%",
  "risk": "MED",
  "rating": "BUY",
  "target": "$53B val",
  "offer_size": "$2.5B",
  "lead_underwriters": ["Morgan Stanley", "Goldman Sachs"],
  "suited_for": ["growth", "aggressive"]
}}

Se non conosci dati precisi, usa "—" o null. Score 0-100. Rating BUY/HOLD/AVOID. Risk LOW/MED/HIGH."""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
            timeout=AI_TIMEOUT,
        )
        return parse_json(response.content[0].text.strip())
    except Exception as e:
        print(f"  [AI error] {e}", flush=True)
        return None


def merge_ipo(basic, enriched):
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
    try:
        for k in ["pros", "cons", "catalysts", "competitors", "comparables", "founders",
                  "lead_underwriters", "suited_for", "raw_data"]:
            if k in row and row[k] is not None and not isinstance(row[k], (list, dict)):
                row[k] = None
        clean = {k: v for k, v in row.items() if v is not None}
        clean["updated_at"] = datetime.now(timezone.utc).isoformat()
        supabase.table("ipos_live").upsert(clean, on_conflict="ticker,ipo_date").execute()
        return True
    except Exception as e:
        print(f"  [save error] {row.get('ticker')}: {e}", flush=True)
        return False


def cleanup_old_ipos(supabase):
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    try:
        supabase.table("ipos_live").delete().lt("ipo_date", cutoff).execute()
        print(f"[cleanup] vecchie IPO rimosse", flush=True)
    except Exception as e:
        print(f"[cleanup error] {e}", flush=True)


def main():
    if not FINNHUB_API_KEY:
        print("[!] FINNHUB_API_KEY mancante. Termino.", flush=True)
        return

    print("[init] Creo client Supabase + Anthropic...", flush=True)
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print("[init] Client OK", flush=True)
    except Exception as e:
        print(f"[init] errore: {e}", flush=True)
        return

    print("[ipos] Avvio fetch IPO calendar da Finnhub...", flush=True)
    raw_ipos = fetch_finnhub_ipos()
    print(f"[ipos] {len(raw_ipos)} record grezzi da Finnhub", flush=True)

    if not raw_ipos:
        print("[ipos] Nessun record. Termino.", flush=True)
        return

    print(f"[ipos] DEBUG primo record keys: {list(raw_ipos[0].keys())[:15]}", flush=True)

    normalized = []
    for item in raw_ipos:
        try:
            n = normalize_finnhub_ipo(item)
            if n.get("ticker") and n.get("name"):
                normalized.append(n)
        except Exception as e:
            print(f"  [normalize error] {e}", flush=True)

    print(f"[ipos] {len(normalized)} dopo normalizzazione", flush=True)

    def sort_key(n):
        status_priority = {"upcoming": 0, "priced": 1, "recent": 2, "pre-ipo": 3, "past": 4, "withdrawn": 5}
        return (
            status_priority.get(n.get("status"), 6),
            -(n.get("market_cap_estimate") or 0),
            n.get("ipo_date") or "9999",
        )
    normalized.sort(key=sort_key)

    to_process = normalized[:MAX_IPOS_TO_ENRICH]
    print(f"[ipos] {len(to_process)} verranno arricchite con AI (limite={MAX_IPOS_TO_ENRICH})", flush=True)

    print("[ipos] Carico esistenti dal DB...", flush=True)
    existing = fetch_existing_ipos(supabase)
    print(f"[ipos] {len(existing)} IPO già esistenti nel DB", flush=True)

    success = 0
    skipped = 0
    start_time = time.time()

    for i, basic in enumerate(to_process, 1):
        ticker = basic.get("ticker")
        ipo_date = basic.get("ipo_date") or "TBD"
        key = f"{ticker}_{ipo_date}"
        elapsed = time.time() - start_time

        if key in existing:
            existing_row = existing[key]
            old_status = existing_row.get("status")
            enriched_at = existing_row.get("enriched_at")
            if enriched_at and old_status == basic.get("status"):
                try:
                    enr_dt = datetime.fromisoformat(enriched_at.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - enr_dt).days < 7:
                        skipped += 1
                        print(f"  [{i}/{len(to_process)}] SKIP {ticker} (già arricchito)", flush=True)
                        continue
                except Exception:
                    pass

        print(f"  [{i}/{len(to_process)}] [{elapsed:.0f}s] AI per {ticker} ({basic.get('name', '')[:40]})...", flush=True)
        t0 = time.time()
        enriched = enrich_ipo_with_ai(anthropic_client, basic)
        t_ai = time.time() - t0
        print(f"    AI rispose in {t_ai:.1f}s", flush=True)

        if not enriched:
            print(f"    AI fallita, salvo solo dati base", flush=True)
            row = basic
            row["enriched_at"] = None
        else:
            row = merge_ipo(basic, enriched)

        if save_ipo(supabase, row):
            success += 1
            cat = row.get("category", "—")
            sc = row.get("score", "—")
            print(f"    OK: {cat}, score {sc}", flush=True)

        time.sleep(0.3)

    cleanup_old_ipos(supabase)
    print(f"\n[ipos] Completato: {success}/{len(to_process)} salvate, {skipped} skipped. "
          f"Tempo totale: {time.time() - start_time:.1f}s", flush=True)


if __name__ == "__main__":
    main()
