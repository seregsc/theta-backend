"""
Generatore di eventi economici live per il calendario di Theta.
Chiede a Claude di sintetizzare eventi macro/earnings/policy della settimana corrente
basandosi sul suo training (calendario macro standard) + le news recenti del database.
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

NUM_EVENTS = 15
KEEP_HOURS = 168


def fetch_recent_news_context(supabase, limit=15):
    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    result = supabase.table("news") \
        .select("title_it, summary_it, impact_it, tickers, published_at") \
        .gte("published_at", since) \
        .not_.is_("title_it", "null") \
        .order("published_at", desc=True) \
        .limit(limit) \
        .execute()
    return result.data or []


def build_news_context(news_items):
    lines = []
    for n in news_items:
        title = n.get("title_it") or "—"
        impact = (n.get("impact_it") or "")[:200]
        tickers = n.get("tickers") or ""
        lines.append(f"- {title} [ticker: {tickers}] — {impact}")
    return "\n".join(lines)


def parse_events_response(text):
    """Parser JSON robusto: gestisce blocchi markdown e testo extra."""
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline > 0:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].strip()
        elif "```" in text:
            text = text[:text.rfind("```")].strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return None
    except json.JSONDecodeError as e:
        print(f"  errore parsing JSON: {e}")
        print(f"  primi 300 char: {text[:300]}")
        print(f"  ultimi 300 char: {text[-300:]}")
        return None


def generate_events(client, news_context):
    today = datetime.now()
    today_str = today.strftime("%d %B %Y, %A")
    week_end = (today + timedelta(days=7)).strftime("%d %B %Y")

    prompt = f"""Sei un analista finanziario senior italiano. Compila il calendario economico per la settimana che inizia oggi ({today_str}) e finisce il {week_end}.

Genera {NUM_EVENTS} eventi rilevanti previsti nei prossimi 7 giorni. Usa la tua conoscenza del calendario macro standard:
- Riunioni banche centrali (Fed FOMC, BCE, BoE, BoJ) con date note
- Pubblicazione dati macro (CPI USA, NFP primo venerdì del mese, PCE, ISM, dati eurozona, dati Italia)
- Earnings di grandi società (Magnifici 7, principali europee, italiane FTSE MIB)
- Eventi geopolitici noti già calendarizzati (summit, vertici, scadenze)

CONTESTO — News recenti dal mercato:
{news_context}

ISTRUZIONI
- Eventi REALI e PLAUSIBILI nella settimana indicata.
- Distribuzione bilanciata: 6-8 eventi macro, 4-5 earnings, 1-2 policy/geopolitica.
- Importance: "HIGH", "MED", "LOW".
- Type: "macro", "earnings", "policy", "geopolitica".
- Italiano professionale.
- Ticker reali (AAPL, NVDA, MSFT, TSLA, ENI.MI, RHM.DE, ecc.).

OUTPUT — rispondi SOLO con JSON puro, senza preamboli, senza markdown, senza ```. Array di {NUM_EVENTS} oggetti esattamente in questo formato:

[
  {{
    "event_date": "2026-05-26",
    "date_label": "26 Mag",
    "event_time": "14:30 CET",
    "days_from_now": 4,
    "type": "macro",
    "type_label": "Macro USA",
    "importance": "HIGH",
    "region": "USA",
    "title": "Titolo breve (max 90 caratteri)",
    "summary": "Descrizione in 3-5 frasi (400-700 caratteri) che spiega cosa è l'evento, perché conta per i mercati, qual è il consensus.",
    "baseline_scenario": "Scenario base in 3-4 frasi (300-500 caratteri).",
    "surprise_scenario": "Scenario di sorpresa in 3-4 frasi (300-500 caratteri).",
    "impacted_sectors": ["ai", "bonds"],
    "impacted_tickers": ["NVDA", "TLT"],
    "what_to_watch": ["Punto 1", "Punto 2", "Punto 3", "Punto 4"],
    "preparation": "Suggerimento operativo in 3-4 frasi (300-500 caratteri)."
  }}
]

Calcola correttamente days_from_now rispetto a {today_str} (0 = oggi). Date in formato YYYY-MM-DD. date_label tipo "26 Mag"."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=9000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        events = parse_events_response(text)
        return events
    except Exception as e:
        print(f"  errore Claude: {e}")
        return None


def cleanup_old_events(supabase, keep_recent_hours=168):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_recent_hours)).isoformat()
    try:
        result = supabase.table("events_live") \
            .delete() \
            .lt("generated_at", cutoff) \
            .execute()
        deleted = len(result.data) if result.data else 0
        if deleted > 0:
            print(f"  eliminati {deleted} eventi vecchi")
    except Exception as e:
        print(f"  errore cleanup: {e}")


def save_events(supabase, events):
    saved = 0
    for e in events:
        row = {
            "event_date": e.get("event_date"),
            "date_label": e.get("date_label"),
            "event_time": e.get("event_time"),
            "days_from_now": e.get("days_from_now"),
            "type": e.get("type"),
            "type_label": e.get("type_label"),
            "importance": e.get("importance"),
            "region": e.get("region"),
            "title": e.get("title"),
            "summary": e.get("summary"),
            "baseline_scenario": e.get("baseline_scenario"),
            "surprise_scenario": e.get("surprise_scenario"),
            "impacted_sectors": e.get("impacted_sectors"),
            "impacted_tickers": e.get("impacted_tickers"),
            "what_to_watch": e.get("what_to_watch"),
            "preparation": e.get("preparation"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            supabase.table("events_live").insert(row).execute()
            saved += 1
            print(f"  ok: {e.get('title', '?')[:60]}")
        except Exception as exc:
            print(f"  errore salvataggio: {exc}")
    return saved


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print("1. Cleanup eventi vecchi (oltre 7 giorni)...")
    cleanup_old_events(supabase, keep_recent_hours=KEEP_HOURS)

    print("2. Raccolgo contesto news recenti...")
    news = fetch_recent_news_context(supabase, limit=15)
    print(f"   Trovate {len(news)} news\n")

    news_context = build_news_context(news) if news else "(nessuna news recente)"

    print(f"3. Genero {NUM_EVENTS} eventi via Claude...")
    events = generate_events(client, news_context)

    if not events:
        print("Generazione fallita. Esco.")
        return

    print(f"   Ricevuti {len(events)} eventi\n")

    print("4. Salvataggio nel database...")
    saved = save_events(supabase, events)

    print(f"\nFatto: {saved}/{len(events)} eventi salvati.")


if __name__ == "__main__":
    main()
