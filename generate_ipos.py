"""
generate_ipos.py
Pesca IPO da Finnhub + lista curata di big names + arricchimento AI Claude.
Salva su ipos_live (UPSERT su ticker+ipo_date).

Tier system:
  1 = Big names curati (Stripe, Klarna, SpaceX, OpenAI, ecc.) - sempre in cima
  2 = Finnhub IPO con offer significativo (>$100M)
  3 = Finnhub IPO minori (SPAC, micro-cap)
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone, date
from supabase import create_client
import anthropic

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
MAX_FINNHUB_IPOS = 12  # Limito Finnhub per non sprecare AI
HTTP_TIMEOUT = 20
AI_TIMEOUT = 60

# Soglia per tier 2 vs tier 3
TIER2_MIN_OFFER_USD = 100_000_000  # $100M minimum offer
TIER2_MIN_SHARES = 5_000_000        # o 5M shares

# ═══════════════════════════════════════════════════════════════
# TIER 1 — BIG NAMES CURATED
# Aziende attese in IPO 2026-2028 (alcune confermate, altre rumored)
# ═══════════════════════════════════════════════════════════════
TIER1_BIG_NAMES = [
    # Fintech
    {"ticker": "STRP", "name": "Stripe", "geo": "USA", "exchange": "NYSE", "expected_date": "H2 2026", "status": "pre-ipo", "hint_sector": "fintech / payments"},
    {"ticker": "KLAR", "name": "Klarna", "geo": "EU", "exchange": "NYSE", "expected_date": "Q3 2026", "status": "pre-ipo", "hint_sector": "fintech / BNPL"},
    {"ticker": "REVO", "name": "Revolut", "geo": "EU", "exchange": "LSE/NASDAQ", "expected_date": "2026-2027", "status": "rumored", "hint_sector": "fintech / neobank"},
    {"ticker": "CHYM", "name": "Chime Financial", "geo": "USA", "exchange": "NASDAQ", "expected_date": "Q4 2026", "status": "pre-ipo", "hint_sector": "fintech / neobank"},
    {"ticker": "PLAID", "name": "Plaid", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2026-2027", "status": "rumored", "hint_sector": "fintech / open banking"},
    {"ticker": "BREX", "name": "Brex", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2027", "status": "rumored", "hint_sector": "fintech / corporate cards"},

    # AI / Data
    {"ticker": "OPENAI", "name": "OpenAI", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2027+", "status": "rumored", "hint_sector": "AI foundation models"},
    {"ticker": "ANTHRO", "name": "Anthropic", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2027+", "status": "rumored", "hint_sector": "AI foundation models"},
    {"ticker": "XAI", "name": "xAI", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2026-2027", "status": "rumored", "hint_sector": "AI Musk"},
    {"ticker": "DBX2", "name": "Databricks", "geo": "USA", "exchange": "NASDAQ", "expected_date": "Q4 2026", "status": "pre-ipo", "hint_sector": "Data / AI lakehouse"},
    {"ticker": "CHRE", "name": "Cohere", "geo": "Canada", "exchange": "NASDAQ", "expected_date": "2026-2027", "status": "rumored", "hint_sector": "AI enterprise LLM"},

    # Crypto
    {"ticker": "KRAK", "name": "Kraken", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2026", "status": "rumored", "hint_sector": "crypto exchange"},
    {"ticker": "CIRCLE", "name": "Circle Internet", "geo": "USA", "exchange": "NYSE", "expected_date": "2026", "status": "pre-ipo", "hint_sector": "crypto stablecoin USDC"},

    # Tech consumer
    {"ticker": "DISCRD", "name": "Discord", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2026", "status": "rumored", "hint_sector": "social communication"},
    {"ticker": "CANVA", "name": "Canva", "geo": "Australia", "exchange": "NASDAQ", "expected_date": "2026", "status": "rumored", "hint_sector": "design SaaS"},
    {"ticker": "NOTION", "name": "Notion Labs", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2027", "status": "rumored", "hint_sector": "productivity SaaS"},

    # Space / Defense / Auto
    {"ticker": "SPCX", "name": "SpaceX", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2027+", "status": "rumored", "hint_sector": "space / aerospace"},
    {"ticker": "ANDR", "name": "Anduril Industries", "geo": "USA", "exchange": "NYSE", "expected_date": "2026-2027", "status": "rumored", "hint_sector": "defense tech AI"},
    {"ticker": "WAYMO", "name": "Waymo", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2027+", "status": "rumored", "hint_sector": "autonomous driving"},
    {"ticker": "WYVE", "name": "Wayve Technologies", "geo": "EU", "exchange": "NASDAQ", "expected_date": "Q1 2027", "status": "pre-ipo", "hint_sector": "autonomous driving"},

    # Recently IPO'd (utili come reference per il consulente)
    {"ticker": "CRWV", "name": "CoreWeave", "geo": "USA", "exchange": "NASDAQ", "expected_date": "Q1 2025 (IPO completed)", "status": "priced", "hint_sector": "AI cloud GPU"},
    {"ticker": "RDDT", "name": "Reddit", "geo": "USA", "exchange": "NYSE", "expected_date": "Mar 2024 (IPO completed)", "status": "priced", "hint_sector": "social media"},
    {"ticker": "ALAB", "name": "Astera Labs", "geo": "USA", "exchange": "NASDAQ", "expected_date": "Mar 2024 (IPO completed)", "status": "priced", "hint_sector": "AI connectivity chip"},
    {"ticker": "TEM", "name": "Tempus AI", "geo": "USA", "exchange": "NASDAQ", "expected_date": "Giu 2024 (IPO completed)", "status": "priced", "hint_sector": "biotech AI"},

    # Healthcare
    {"ticker": "RXRX2", "name": "Recursion Pharmaceuticals", "geo": "USA", "exchange": "NASDAQ", "expected_date": "2026 (follow-on)", "status": "pre-ipo", "hint_sector": "biotech AI drug discovery"},
]


def fetch_finnhub_ipos():
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
            print(f"[Finnhub] formato inatteso (dict): {list(data.keys())[:5]}", flush=True)
            return []
        if isinstance(data, list):
            return data
        return []
    except requests.Timeout:
        print(f"[Finnhub] TIMEOUT", flush=True)
        return []
    except Exception as e:
        print(f"[Finnhub] errore: {e}", flush=True)
        return []


def normalize_finnhub_ipo(item):
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
        "LSE": "EU", "EURONEXT": "EU", "XETRA": "EU", "MIL": "EU",
        "HKEX": "Asia", "TSE": "Asia", "TSX": "Canada",
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

    # Determina tier 2 vs 3 in base a offer size
    shares_int = int(shares) if shares else 0
    if market_cap_estimate and market_cap_estimate >= TIER2_MIN_OFFER_USD:
        tier = 2
    elif shares_int >= TIER2_MIN_SHARES:
        tier = 2
    else:
        tier = 3

    return {
        "ticker": symbol,
        "name": name,
        "exchange": exchange,
        "geo": geo,
        "ipo_date": ipo_date_str if ipo_d else None,
        "expected_date": expected_date_label,
        "price_range": price_range,
        "shares_offered": shares_int or None,
        "market_cap_estimate": market_cap_estimate,
        "status": status,
        "tier": tier,
        "raw_data": item,
    }


def build_tier1_basic(entry):
    """Trasforma un entry TIER1 nel formato basic per l'AI."""
    return {
        "ticker": entry["ticker"],
        "name": entry["name"],
        "exchange": entry.get("exchange") or "—",
        "geo": entry.get("geo") or "USA",
        "ipo_date": None,
        "expected_date": entry.get("expected_date") or "TBD",
        "price_range": "—",
        "shares_offered": None,
        "market_cap_estimate": None,
        "status": entry.get("status") or "rumored",
        "tier": 1,
        "hint_sector": entry.get("hint_sector") or "",
        "raw_data": {"curated": True},
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
    is_curated = ipo_basic.get("tier") == 1
    hint = ipo_basic.get("hint_sector", "")
    curated_note = f"\nNOTA: questa è un'azienda molto nota di settore '{hint}'. Usa la tua conoscenza dettagliata per generare un'analisi approfondita basata sui rumor di mercato e dati pubblici. Se è 'rumored', dichiaralo chiaramente nella thesis." if is_curated else ""

    prompt = f"""Sei un equity research analyst senior italiano. Devi arricchire un'IPO con analisi finanziaria + TIMELINE ROADSHOW per un consulente Fineco italiano.

═══════════════════════════════════
DATI GREZZI DELL'IPO
═══════════════════════════════════
Ticker: {ipo_basic.get('ticker', '—')}
Nome: {ipo_basic.get('name', '—')}
Exchange: {ipo_basic.get('exchange', '—')}
Geo: {ipo_basic.get('geo', '—')}
Data attesa: {ipo_basic.get('expected_date', 'TBD')}
Range prezzo: {ipo_basic.get('price_range', '—')}
Status: {ipo_basic.get('status', '—')}
Azioni offerte: {ipo_basic.get('shares_offered', '—')}{curated_note}

═══════════════════════════════════
ISTRUZIONI
═══════════════════════════════════
Se NON CONOSCI l'azienda (micro-cap, SPAC sconosciuto), restituisci comunque un'analisi BASIC basata su nome/settore/exchange. Dichiara chiaramente nella thesis che è un'azienda minore non ben coperta.

Per le AZIENDE NOTE (Stripe, Klarna, OpenAI, SpaceX, Revolut, ecc.) usa la tua conoscenza specifica per fornire analisi approfondita: founders reali, ultimo round funding, ARR/revenue noti, comparable IPO recenti del settore.

Settori (slug): ai, semis, defense, nuclear, biotech, fintech, luxury, auto, energy, utilities, consumer, industrials, telecom, reits, emerging, gold, copper, crypto, health, real_estate, other.

Profili (suited_for): conservative, balanced, growth, aggressive.

═══════════════════════════════════
TIMELINE ROADSHOW — OBBLIGATORIO
═══════════════════════════════════
5-7 milestone con date in italiano abbreviate ("Mag 2024", "Q2 2026", "H2 2026", ecc):
- 2-3 PASSATE (status: "done") - round funding, breakeven, filing iniziale
- 1 CORRENTE (status: "active") - cosa sta succedendo ora
- 2-3 FUTURE (status: "pending") - listing, lock-up, prima trimestrale

NIENTE jargon vuoto. Lessico semplice. Frasi brevi.

═══════════════════════════════════
FORMATO OUTPUT (SOLO JSON, NESSUN TESTO PRIMA O DOPO)
═══════════════════════════════════
{{
  "sector": "AI Infrastructure",
  "category": "ai",
  "headline": "Prima IPO pura-play AI cloud (max 70 caratteri)",
  "summary": "1-2 frasi cosa fa l'azienda",
  "business": "2-3 frasi modello di business",
  "thesis": "4-6 frasi tesi con rischi e opportunità",
  "pros": ["Punto 1", "Punto 2", "Punto 3"],
  "cons": ["Rischio 1", "Rischio 2", "Rischio 3"],
  "catalysts": ["Catalyst 1", "Catalyst 2"],
  "competitors": ["Concorrente 1", "Concorrente 2"],
  "comparables": [
    {{"name":"Snowflake","ticker":"SNOW","ipo_date":"Set 2020","ipo_price":"$120","perf_first_day":"+111%","perf_1y":"+38%","note":"Comparable settoriale"}}
  ],
  "timeline": [
    {{"date":"Mag 2024","title":"Series C $1B","note":"Round guidato da SoftBank","status":"done"}},
    {{"date":"Q1 2026","title":"S-1 filing","note":"Documenti SEC depositati","status":"done"}},
    {{"date":"Q2 2026","title":"Roadshow investitori","note":"Presentazioni a fondi USA e EU","status":"active"}},
    {{"date":"Q3 2026","title":"Pricing & listing","note":"Prezzo finale, primo giorno trading","status":"pending"}},
    {{"date":"Q4 2026","title":"Lock-up insider (180 giorni)","note":"Insider non possono vendere","status":"pending"}},
    {{"date":"Q2 2027","title":"Prima trimestrale public","note":"Test execution revenue guidance","status":"pending"}}
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
  "last_round": "$23B (Mag 2024)",
  "score": 88,
  "upside": "+52%",
  "risk": "MED",
  "rating": "BUY",
  "target": "$53B val",
  "offer_size": "$2.5B",
  "lead_underwriters": ["Morgan Stanley", "Goldman Sachs"],
  "suited_for": ["growth", "aggressive"]
}}

Se non conosci dati precisi, usa "—" o null. Score 0-100. Rating BUY/HOLD/AVOID. Risk LOW/MED/HIGH.
La timeline DEVE avere almeno 5 milestone."""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
            timeout=AI_TIMEOUT,
        )
        return parse_json(response.content[0].text.strip())
    except Exception as e:
        print(f"  [AI error] {e}", flush=True)
        return None


def merge_ipo(basic, enriched):
    merged = {**basic}
    # rimuovi hint_sector che non va salvato in DB
    merged.pop("hint_sector", None)
    if enriched:
        for key in ["sector", "category", "headline", "summary", "business", "thesis",
                    "pros", "cons", "catalysts", "competitors", "comparables",
                    "timeline",
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
                  "lead_underwriters", "suited_for", "raw_data", "timeline"]:
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
    """Rimuove IPO Finnhub molto vecchie (tier 2 e 3). I tier 1 li teniamo sempre."""
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    try:
        supabase.table("ipos_live").delete() \
            .lt("ipo_date", cutoff) \
            .neq("tier", 1) \
            .execute()
        print(f"[cleanup] vecchie IPO rimosse (tier 2-3)", flush=True)
    except Exception as e:
        print(f"[cleanup error] {e}", flush=True)


def process_ipo(anthropic_client, supabase, basic, existing, label):
    """Arricchisce e salva un'IPO. Restituisce True se salvato con successo."""
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
                    print(f"    SKIP {ticker} (già arricchito)", flush=True)
                    return False
            except Exception:
                pass

    print(f"    AI per {ticker} ({basic.get('name', '')[:40]}) [{label}]...", flush=True)
    t0 = time.time()
    enriched = enrich_ipo_with_ai(anthropic_client, basic)
    t_ai = time.time() - t0
    print(f"      AI rispose in {t_ai:.1f}s", flush=True)

    if not enriched:
        row = basic
        row.pop("hint_sector", None)
        row["enriched_at"] = None
    else:
        row = merge_ipo(basic, enriched)
        timeline_count = len(enriched.get("timeline") or [])
        print(f"      timeline: {timeline_count} milestone", flush=True)

    return save_ipo(supabase, row)


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

    existing = fetch_existing_ipos(supabase)
    print(f"[ipos] {len(existing)} IPO già esistenti nel DB", flush=True)

    start_time = time.time()

    # ═══════════════════════════════════════════════════════════════
    # TIER 1 — Big names curated
    # ═══════════════════════════════════════════════════════════════
    print(f"\n=== TIER 1: {len(TIER1_BIG_NAMES)} big names curated ===", flush=True)
    tier1_success = 0
    for i, entry in enumerate(TIER1_BIG_NAMES, 1):
        basic = build_tier1_basic(entry)
        print(f"  [{i}/{len(TIER1_BIG_NAMES)}]", flush=True)
        if process_ipo(anthropic_client, supabase, basic, existing, "Tier1"):
            tier1_success += 1
        time.sleep(0.3)
    print(f"  Tier 1 completato: {tier1_success}/{len(TIER1_BIG_NAMES)}", flush=True)

    # ═══════════════════════════════════════════════════════════════
    # TIER 2 + 3 — Finnhub
    # ═══════════════════════════════════════════════════════════════
    print(f"\n=== TIER 2/3: fetch da Finnhub ===", flush=True)
    raw_ipos = fetch_finnhub_ipos()
    print(f"  {len(raw_ipos)} record grezzi", flush=True)

    if raw_ipos:
        normalized = []
        for item in raw_ipos:
            try:
                n = normalize_finnhub_ipo(item)
                if n.get("ticker") and n.get("name"):
                    normalized.append(n)
            except Exception as e:
                print(f"  [normalize error] {e}", flush=True)

        # Ordina per tier (tier 2 prima di 3), poi market_cap, poi data
        def sort_key(n):
            status_priority = {"upcoming": 0, "priced": 1, "recent": 2, "pre-ipo": 3, "past": 4, "withdrawn": 5}
            return (
                n.get("tier", 3),
                status_priority.get(n.get("status"), 6),
                -(n.get("market_cap_estimate") or 0),
                n.get("ipo_date") or "9999",
            )
        normalized.sort(key=sort_key)

        # Prendi prima quelle tier 2 (significative), poi qualche tier 3
        tier2 = [n for n in normalized if n.get("tier") == 2]
        tier3 = [n for n in normalized if n.get("tier") == 3]
        print(f"  Tier 2 trovate: {len(tier2)}", flush=True)
        print(f"  Tier 3 trovate: {len(tier3)}", flush=True)

        # AI enrichment solo per tier 2 (significative) + i primi 3 tier 3 più grossi
        to_process = tier2[:MAX_FINNHUB_IPOS] + tier3[:3]
        print(f"  Verranno arricchite: {len(to_process)} (tier2={min(len(tier2), MAX_FINNHUB_IPOS)}, tier3=3)", flush=True)

        tier23_success = 0
        for i, basic in enumerate(to_process, 1):
            elapsed = time.time() - start_time
            print(f"  [{i}/{len(to_process)}] [{elapsed:.0f}s tot]", flush=True)
            if process_ipo(anthropic_client, supabase, basic, existing, f"Tier{basic.get('tier')}"):
                tier23_success += 1
            time.sleep(0.3)
        print(f"  Tier 2/3 completato: {tier23_success}/{len(to_process)}", flush=True)

        # I tier 3 rimanenti li salviamo SENZA arricchimento AI (dati grezzi only)
        # così appaiono comunque in UI ma in fondo alla lista
        remaining_tier3 = tier3[3:30]  # max 30 tier 3 grezze in totale
        if remaining_tier3:
            print(f"  Salvo {len(remaining_tier3)} tier 3 rimanenti senza arricchimento AI...", flush=True)
            saved_raw = 0
            for basic in remaining_tier3:
                basic.pop("hint_sector", None)
                basic["enriched_at"] = None
                if save_ipo(supabase, basic):
                    saved_raw += 1
            print(f"  Salvate raw: {saved_raw}", flush=True)

    cleanup_old_ipos(supabase)
    print(f"\n[ipos] TUTTO completato. Tempo totale: {time.time() - start_time:.1f}s", flush=True)


if __name__ == "__main__":
    main()
