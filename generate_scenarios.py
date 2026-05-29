"""
Generatore di stress test live per Theta.
- 1 scenario nuovo al giorno (alle 7:00 italiane via cron-job.org)
- Mai cancellati: archivio storico permanente
- Diversificazione: il prompt riceve i titoli degli scenari passati per non duplicare
- Include analisi settoriale: affected_sectors + sector_impact per impatti INDIRETTI sui clienti
- NOVITÀ: genera precedenti storici SPECIFICI insieme allo scenario
- NOVITÀ: genera l'immagine AI subito (Pollinations) e la salva su Supabase Storage
"""
import os
import json
import time
import urllib.parse
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Nome modello VALIDO (con suffisso data). Senza data l'API fallisce silenziosamente.
MODEL = "claude-sonnet-4-5-20250929"

# Bucket pubblico su Supabase Storage per le immagini scenari
IMAGE_BUCKET = "scenario-images"


def fetch_recent_news_context(supabase, limit=10):
    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    result = supabase.table("news") \
        .select("title_it, impact_it, tickers, published_at") \
        .gte("published_at", since) \
        .not_.is_("title_it", "null") \
        .order("published_at", desc=True) \
        .limit(limit) \
        .execute()
    return result.data or []


def fetch_existing_scenario_titles(supabase, limit=60):
    """Recupera i titoli degli scenari già generati per evitare duplicati."""
    try:
        result = supabase.table("scenarios_live") \
            .select("title, description") \
            .order("generated_at", desc=True) \
            .limit(limit) \
            .execute()
        return result.data or []
    except Exception as e:
        print(f"  errore fetch scenari esistenti: {e}")
        return []


def build_news_context(news_items):
    lines = []
    for n in news_items:
        title = n.get("title_it") or "—"
        impact = (n.get("impact_it") or "")[:150]
        lines.append(f"- {title} — {impact}")
    return "\n".join(lines)


def build_avoid_list(existing):
    if not existing:
        return "(nessuno - sei libero di scegliere qualsiasi tipo di scenario)"
    lines = []
    for s in existing[:30]:
        title = s.get("title") or ""
        desc = (s.get("description") or "")[:80]
        lines.append(f"- {title}: {desc}")
    return "\n".join(lines)


def parse_response(text):
    """Estrae il singolo oggetto JSON dalla risposta."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline > 0:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].strip()
        elif "```" in text:
            text = text[:text.rfind("```")].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return None
    except json.JSONDecodeError as e:
        print(f"  errore parsing JSON: {e}")
        print(f"  primi 300 char: {text[:300]}")
        return None


def generate_scenario(client, news_context, avoid_list):
    today_str = datetime.now().strftime("%d %B %Y, %A")
    prompt = f"""Sei un risk manager senior italiano. Genera UNO scenario di stress test diverso da qualsiasi altro già esistente, ispirato dal contesto macro/geopolitico attuale.

DATA OGGI: {today_str}

CONTESTO — News recenti dal mercato (per ispirazione, non per copia):
{news_context}

SCENARI GIÀ GENERATI IN PASSATO (non duplicare, neanche concettualmente):
{avoid_list}

REGOLE DI CREATIVITÀ
- Esplora aree diverse: geopolitica, macroeconomia, tecnologia, energia, regulation, cigni neri, settori specifici, eventi politici.
- Non riciclare temi simili a quelli già fatti. Trova angoli originali.
- Mantieni plausibilità: lo scenario deve essere realistico e ragionato, non assurdo.
- Lo scenario non è una previsione live: è un esercizio mentale di preparazione per il consulente.

LESSICO — OBBLIGATORIO LEGGIBILE
Il consulente leggerà questi scenari e li userà spesso per spiegare ai clienti NON esperti cosa potrebbe succedere. Quindi:
- Italiano SEMPLICE, frasi brevi (max 25 parole).
- NIENTE jargon vuoto: "fondamentali solidi", "outlook deteriorato", "rationale", "guidance", "tesi compromessa".
- Sostituisci tecnicismi con sinonimi semplici:
  - "drawdown" → "calo dai massimi"
  - "spread" → "differenza tra rendimenti"
  - "bps" (basis points) → "punti percentuali (un bps = 0,01%)"
  - "VIX" → "indice della paura (VIX)"
  - "hedge" → "protezione/copertura"
  - "duration" → "durata media dell'obbligazione"
  - "yield" → "rendimento"
- Mantieni SEMPRE i numeri precisi (% variazioni, ticker, prezzi target).
- Nella DESCRIZIONE racconta la storia come la racconteresti a un cliente: "succede X, poi Y, poi Z".
- Negli IMPATTI usa frasi pratiche tipo "petrolio sale del 30-50%" non "shock energetico negativo".
- Nella HEDGE_STRATEGY parla al TU al consulente con verbi concreti: «valuta», «considera», «alleggerisci», «monitora».

CRUCIALE — IMPATTO SETTORIALE PER CLIENTI
Oltre ai ticker specifici di winners/losers, devi indicare quali SETTORI sarebbero impattati dallo scenario.
I clienti spesso detengono ETF settoriali (non solo singoli titoli), quindi serve identificare gli IMPATTI INDIRETTI tramite settore.

SETTORI DISPONIBILI (usa SOLO questi tag esatti):
- ai → Intelligenza Artificiale (ETF tipo XAIQ, AIQ, BOTZ)
- semis → Semiconduttori (SMH, SOXX, NVDA, AMD, TSM, ASML)
- defense → Difesa & Sicurezza (ITA, XAR, RTX, LMT, BA)
- nuclear → Nucleare (URA, NLR, CCJ)
- space → Settore Spaziale (UFO, ROK)
- energy → Energia tradizionale (XLE, XOM, CVX, oil & gas)
- health → Sanità (XLV, JNJ, PFE, biotech)
- finance → Banche & Finanza (XLF, JPM, GS, banche EU)
- consumer → Beni di consumo (XLP, XLY)
- industrial → Industria (XLI, CAT, GE)
- lusso → Lusso (LVMH, KER, MC.PA, RACE)
- auto → Automotive (TSLA, F, GM, STLA, VOW)
- emerging → Mercati emergenti (EEM, VWO, China)
- bonds → Obbligazioni (TLT, AGG, IEF, BTPS)
- gold → Oro & Metalli preziosi (GLD, IAU, SLV)
- commodities → Materie prime (DBA, oil, gas, rame)
- crypto → Criptovalute (BTC, ETH)
- real_estate → Immobiliare (VNQ, REIT generici)
- utilities → Utility (XLU)
- reit → Immobiliare quotato (SPG, PLD)
- indices → Indici di mercato (SPY, QQQ, IWM, DIA, ACWI)

PRECEDENTI STORICI — REGOLA FONDAMENTALE
Devi fornire 2-3 precedenti storici VERAMENTE simili a QUESTO scenario specifico.
- NON eventi generici della stessa "categoria", ma eventi che hanno la STESSA dinamica concreta.
- Esempio sbagliato: per uno scenario "Cina blocca esportazioni di terre rare" NON citare il lancio di iPhone o ChatGPT solo perché è "tecnologia".
- Esempio giusto: per uno scenario "Cina blocca esportazioni di terre rare" cita gli embarghi reali di terre rare (2010 Cina-Giappone), o lo Smoot-Hawley Tariff Act (1930), o l'embargo petrolifero OPEC (1973).
- Ogni precedente deve avere: anno/periodo, nome dell'evento, riassunto in 1 frase di cosa successe ai mercati, dettaglio di 2 frasi con numeri concreti (% di calo, durata, recovery), sentiment ("pos"/"neg"/"warn" a seconda di come andò).

OUTPUT — rispondi SOLO con JSON puro, un singolo oggetto, senza markdown:

{{
  "title": "Titolo evocativo, max 80 caratteri",
  "icon": "🌐",
  "probability": "Bassa (15%) | Media (35%) | Alta (60%)",
  "severity": "Lieve | Media | Severa | Critica",
  "time_horizon": "1-3 mesi | 3-6 mesi | 6-12 mesi | 12-24 mesi",
  "description": "Descrizione 5-7 frasi (700-1000 caratteri) in italiano semplice.",
  "trigger_events": ["Evento 1", "Evento 2", "Evento 3", "Evento 4"],
  "market_impact": {{
    "equity_global": "+5% / -15%",
    "equity_emerging": "+/-X%",
    "bond_10y_us": "+50bps / -30bps",
    "oil": "+/-X%",
    "gold": "+/-X%",
    "usd_index": "+/-X%",
    "vix": "valore stimato"
  }},
  "affected_sectors": ["ai", "semis", "indices"],
  "sector_impact": {{
    "ai": {{
      "expected_move": "-25/-40%",
      "why": "Spiegazione in italiano semplice di perché questo settore sarebbe penalizzato/avvantaggiato"
    }},
    "semis": {{
      "expected_move": "-20/-30%",
      "why": "..."
    }}
  }},
  "winners": [
    {{"ticker": "XOM", "name": "ExxonMobil", "expected_move": "+15-25%", "why": "Spiegazione in italiano semplice"}},
    {{"ticker": "GLD", "name": "SPDR Gold", "expected_move": "+10-18%", "why": "..."}}
  ],
  "losers": [
    {{"ticker": "QQQ", "name": "Invesco QQQ", "expected_move": "-20/-30%", "why": "..."}},
    {{"ticker": "NVDA", "name": "NVIDIA", "expected_move": "-25/-35%", "why": "..."}}
  ],
  "hedge_strategy": "3-4 frasi al TU al consulente in italiano semplice.",
  "early_warning": ["Segnale 1", "Segnale 2", "Segnale 3"],
  "historical_precedents": [
    {{
      "when": "1973",
      "event": "Embargo petrolifero OPEC",
      "summary": "I paesi OPEC bloccano le esportazioni verso l'Occidente: i mercati crollano e l'inflazione esplode.",
      "detail": "L'S&P 500 perse circa il 48% in 21 mesi, il petrolio quadruplicò da 3$ a 12$ al barile. Recovery completa solo nel 1980. Conseguenze inflattive per tutto il decennio.",
      "sentiment": "neg"
    }},
    {{
      "when": "Anno-Anno",
      "event": "Nome evento storico realmente simile a QUESTO scenario specifico",
      "summary": "Cosa successe ai mercati in una frase.",
      "detail": "Dettagli con numeri concreti: % di calo, durata, recovery.",
      "sentiment": "pos | neg | warn"
    }}
  ]
}}

REGOLE CRITICHE SULL'IMPATTO SETTORIALE:
1. affected_sectors deve contenere TUTTI i settori realmente impattati (sia in positivo che negativo). Per uno scenario "Bolla AI scoppia": tipicamente ai, semis, indices in NEGATIVO + bonds, gold in POSITIVO.
2. Per ogni settore in affected_sectors, deve esserci una entry corrispondente in sector_impact con expected_move e why.
3. expected_move usa SEMPRE il formato "+X-Y%" (positivo) o "-X/-Y%" (negativo).
4. Sii completo: aggiungi anche settori "rifugio" che salgono in caso di scenari negativi (oro, bonds, utility, difesa).

Genera UN solo scenario, originale, non simile a nessuno dei precedenti."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return parse_response(text)
    except Exception as e:
        print(f"  errore Claude: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# GENERAZIONE IMMAGINE AI (Pollinations - gratuito)
# ═══════════════════════════════════════════════════════════════

def build_image_prompt(scenario):
    title = (scenario.get("title") or "").strip()
    desc = (scenario.get("description") or "").strip()
    severity = (scenario.get("severity") or "").strip()
    desc_short = desc[:200]
    palette = {
        "Critica": "dark crimson and deep charcoal, ominous mood",
        "Severa": "amber and burnt orange, tense mood",
        "Media": "warm orange and slate grey, cautious mood",
    }.get(severity, "cool blue and slate grey, measured mood")
    return (
        f"Editorial conceptual illustration of a financial market risk scenario. "
        f"Theme: {title}. Context: {desc_short}. "
        f"Cinematic, abstract, sophisticated, dramatic lighting, macro economy and markets feeling, "
        f"{palette}. No text, no words, no letters, no numbers, no charts, no logos. "
        f"Wide cinematic banner, premium financial magazine aesthetic."
    )


def seed_from_title(title):
    s = str(title or "scenario")
    return abs(sum((i + 1) * ord(ch) for i, ch in enumerate(s))) % 1000000


def generate_image_bytes(prompt, seed):
    encoded = urllib.parse.quote(prompt, safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1280&height=720&nologo=true&model=flux&seed={seed}"
    )
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 200 and r.content and len(r.content) > 1000:
                return r.content
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Pollinations ko: {last_err}")


def upload_image(supabase, scenario_id, image_bytes):
    path = f"scenario-{scenario_id}.jpg"
    storage = supabase.storage.from_(IMAGE_BUCKET)
    try:
        storage.upload(path, image_bytes, {"content-type": "image/jpeg", "upsert": "true"})
    except Exception as e:
        msg = str(e)
        if "Duplicate" in msg or "already exists" in msg:
            try:
                storage.remove([path])
            except Exception:
                pass
            storage.upload(path, image_bytes, {"content-type": "image/jpeg"})
        else:
            raise
    public = storage.get_public_url(path)
    if isinstance(public, dict):
        public = public.get("publicUrl") or public.get("public_url") or ""
    return public


def generate_and_save_image(supabase, scenario_id, scenario):
    """Genera e carica l'immagine. Ritorna l'URL o None in caso di errore."""
    try:
        prompt = build_image_prompt(scenario)
        img = generate_image_bytes(prompt, seed_from_title(scenario.get("title")))
        url = upload_image(supabase, scenario_id, img)
        if not url:
            return None
        return url
    except Exception as e:
        print(f"   errore generazione immagine: {str(e)[:200]}")
        return None


# ═══════════════════════════════════════════════════════════════
# SALVATAGGIO
# ═══════════════════════════════════════════════════════════════

def save_scenario(supabase, s):
    """Salva lo scenario e ritorna l'id della riga inserita (o None)."""
    row = {
        "title": s.get("title"),
        "icon": s.get("icon", "🌐"),
        "probability": s.get("probability"),
        "severity": s.get("severity"),
        "time_horizon": s.get("time_horizon"),
        "description": s.get("description"),
        "trigger_events": s.get("trigger_events"),
        "market_impact": s.get("market_impact"),
        "affected_sectors": s.get("affected_sectors"),
        "sector_impact": s.get("sector_impact"),
        "winners": s.get("winners"),
        "losers": s.get("losers"),
        "hedge_strategy": s.get("hedge_strategy"),
        "early_warning": s.get("early_warning"),
        "historical_precedents": s.get("historical_precedents"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = supabase.table("scenarios_live").insert(row).execute()
        if res.data and len(res.data) > 0:
            return res.data[0].get("id")
        return None
    except Exception as exc:
        print(f"  errore salvataggio: {exc}")
        return None


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print("1. Recupero contesto news recenti...")
    news = fetch_recent_news_context(supabase, limit=10)
    print(f"   {len(news)} news\n")

    print("2. Recupero scenari già esistenti (per evitare duplicati)...")
    existing = fetch_existing_scenario_titles(supabase, limit=60)
    print(f"   {len(existing)} scenari nel database\n")

    news_context = build_news_context(news) if news else "(nessuna news disponibile)"
    avoid_list = build_avoid_list(existing)

    print("3. Genero nuovo scenario via Claude Sonnet...")
    scenario = generate_scenario(client, news_context, avoid_list)

    if not scenario or not scenario.get("title"):
        print("Generazione fallita.")
        return

    print(f"   Generato: {scenario.get('title')}")
    n_sectors = len(scenario.get("affected_sectors") or [])
    n_winners = len(scenario.get("winners") or [])
    n_losers = len(scenario.get("losers") or [])
    n_precedents = len(scenario.get("historical_precedents") or [])
    print(f"   {n_sectors} settori, {n_winners} winners, {n_losers} losers, {n_precedents} precedenti storici\n")

    print("4. Salvo nel database...")
    scenario_id = save_scenario(supabase, scenario)
    if not scenario_id:
        print("   Errore nel salvataggio.")
        return
    print(f"   Scenario salvato (id={scenario_id})\n")

    print("5. Genero immagine AI (Pollinations) e la carico su Storage...")
    image_url = generate_and_save_image(supabase, scenario_id, scenario)
    if image_url:
        try:
            supabase.table("scenarios_live").update({"image_url": image_url}).eq("id", scenario_id).execute()
            print(f"   Immagine salvata: {image_url[:80]}...")
        except Exception as e:
            print(f"   errore update image_url: {e}")
    else:
        print("   Immagine non generata (lo scenario è salvato comunque).")

    print("\nCompletato.")


if __name__ == "__main__":
    main()
