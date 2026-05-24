"""
Generatore di stress test live per Theta.
- 1 scenario nuovo al giorno (alle 7:00 italiane via cron-job.org)
- Mai cancellati: archivio storico permanente
- Diversificazione: il prompt riceve i titoli degli scenari passati per non duplicare
"""
import os
import json
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"


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
- Esplora aree diverse: geopolitica, macroeconomia, tecnologia, energia, regulation, cisne neri, settori specifici, eventi politici.
- Non riciclare temi simili a quelli già fatti. Trova angoli originali.
- Mantieni plausibilità: lo scenario deve essere realistico e ragionato, non assurdo.
- Italiano professionale, lessico finanziario.
- Lo scenario non è una previsione live: è un esercizio mentale di preparazione per il consulente.

OUTPUT — rispondi SOLO con JSON puro, un singolo oggetto, senza markdown:

{{
  "title": "Titolo evocativo, max 80 caratteri (es. 'Iran chiude Stretto di Hormuz, petrolio a 150$')",
  "icon": "🌐",
  "probability": "Bassa (15%) | Media (35%) | Alta (60%)",
  "severity": "Lieve | Media | Severa | Critica",
  "time_horizon": "1-3 mesi | 3-6 mesi | 6-12 mesi | 12-24 mesi",
  "description": "Descrizione 5-7 frasi (700-1000 caratteri) che spiega cosa potrebbe accadere, perché è plausibile, quale catena di eventi porterebbe a questo scenario.",
  "trigger_events": ["Evento trigger 1", "Evento trigger 2", "Evento trigger 3", "Evento trigger 4"],
  "market_impact": {{
    "equity_global": "+5% / -15% / etc",
    "equity_emerging": "+/-X%",
    "bond_10y_us": "+50bps / -30bps",
    "oil": "+/-X%",
    "gold": "+/-X%",
    "usd_index": "+/-X%",
    "vix": "valore stimato"
  }},
  "winners": [
    {{"ticker": "XOM", "name": "ExxonMobil", "expected_move": "+15-25%", "why": "Spiegazione breve 1-2 frasi sul perché beneficia"}},
    {{"ticker": "GLD", "name": "SPDR Gold", "expected_move": "+10-18%", "why": "..."}}
  ],
  "losers": [
    {{"ticker": "QQQ", "name": "Invesco QQQ", "expected_move": "-20/-30%", "why": "..."}},
    {{"ticker": "EZA", "name": "iShares South Africa", "expected_move": "-25/-35%", "why": "..."}}
  ],
  "hedge_strategy": "Strategia di copertura 3-4 frasi (300-500 caratteri) al TU al consulente: «valuta...», «considera...», «monitora...».",
  "early_warning": ["Segnale precoce 1", "Segnale precoce 2", "Segnale precoce 3"]
}}

Genera un solo scenario, originale, non simile a nessuno dei precedenti."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return parse_response(text)
    except Exception as e:
        print(f"  errore Claude: {e}")
        return None


def save_scenario(supabase, s):
    row = {
        "title": s.get("title"),
        "icon": s.get("icon", "🌐"),
        "probability": s.get("probability"),
        "severity": s.get("severity"),
        "time_horizon": s.get("time_horizon"),
        "description": s.get("description"),
        "trigger_events": s.get("trigger_events"),
        "market_impact": s.get("market_impact"),
        "winners": s.get("winners"),
        "losers": s.get("losers"),
        "hedge_strategy": s.get("hedge_strategy"),
        "early_warning": s.get("early_warning"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("scenarios_live").insert(row).execute()
        return True
    except Exception as exc:
        print(f"  errore salvataggio: {exc}")
        return False


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

    print(f"   Generato: {scenario.get('title')}\n")

    print("4. Salvo nel database...")
    if save_scenario(supabase, scenario):
        print(f"\n   Scenario salvato con successo.")
    else:
        print(f"\n   Errore nel salvataggio.")


if __name__ == "__main__":
    main()

