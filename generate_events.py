"""
Generatore di eventi economici live per il calendario di Theta.
Chiede a Claude di sintetizzare 4-6 eventi macro/earnings/policy della settimana corrente
basandosi sul suo training (calendario macro standard) + le news recenti del database.
Stesso formato degli EVENTS hardcoded nell'app.
"""
import os
import json
import re
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"

NUM_EVENTS = 15
KEEP_HOURS = 168  # 7 giorni di archivio


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
    """Estrae il JSON dalla risposta di Claude."""
    match = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1).strip()
    else:
        match = re.search(r"\[\s*\{.+\}\s*\]", text, re.DOTALL)
        if match:
            json_text = match.group(0)
        else:
            json_text = text.strip()
    try:
        data = json.loads(json_text)
        if isinstance(data, list):
            return data
        return None
    except json.JSONDecodeError as e:
        print(f"  ✗ Errore parsing JSON: {e}")
        print(f"  Risposta (primi 500): {text[:500]}")
        return None


def generate_events(client, news_context):
    """Chiede a Claude di generare N eventi economici della settimana."""
    today = datetime.now()
    today_str = today.strftime("%d %B %Y, %A")
    week_end = (today + timedelta(days=7)).strftime("%d %B %Y")

    prompt = f"""Sei un analista finanziario senior italiano. Devi compilare il calendario economico per la settimana che inizia oggi ({today_str}) e finisce il {week_end}.

Genera {NUM_EVENTS} eventi rilevanti previsti nei prossimi 7 giorni. Usa la tua conoscenza del calendario macro standard:
- Riunioni banche centrali (Fed FOMC, BCE, BoE, BoJ) — date note
- Pubblicazione dati macro (CPI USA mensile, NFP primo venerdì del mese, PCE, ISM, ECB rate decisions, dati eurozona)
- Earnings di grandi società (Magnifici 7, principali europee, italiane FTSE MIB)
- Eventi geopolitici noti già calendarizzati

CONTESTO — News recenti dal mercato:
{news_context}

ISTRUZIONI
- Eventi REALI e PLAUSIBILI nella settimana indicata, non inventati di sana pianta.
- Distribuzione: 2-3 eventi macro (banche centrali, dati), 1-2 earnings importanti, eventualmente 1 evento policy/geopolitica.
- Importanza: usa "HIGH", "MED", "LOW".
- Type: "macro", "earnings", "policy", "geopolitica".
- Italiano professionale.
- Per i ticker usa solo quelli reali (AAPL, NVDA, MSFT, TSLA, ENI.MI, ecc.).

OUTPUT — Rispondi ESATTAMENTE in formato JSON, array di {NUM_EVENTS} oggetti, senza preamboli o markdown:

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
    "title": "Titolo evento breve (max 90 caratteri)",
    "summary": "Descrizione di 3-5 frasi (400-700 caratteri) che spiega cosa è questo evento, perché conta per i mercati, qual è il consensus attuale degli analisti se applicabile.",
    "baseline_scenario": "Scenario di base in 3-4 frasi (300-500 caratteri): cosa è probabile che succeda, reazione attesa dei mercati (equity, bond, dollaro).",
    "surprise_scenario": "Scenario di sorpresa in 3-4 frasi (300-500 caratteri): cosa succederebbe se il dato/risultato sorprendesse in positivo o negativo.",
    "impacted_sectors": ["ai", "bonds", "gold"],
    "impacted_tickers": ["NVDA", "TLT", "GLD"],
    "what_to_watch": ["Punto 1", "Punto 2", "Punto 3", "Punto 4"],
    "preparation": "Suggerimento operativo per il consulente in 3-4 frasi (300-500 caratteri): per quali clienti, quale posizionamento, hedge opzionali."
  }}
]

Calcola correttamente "days_from_now" rispetto a {today_str} (0 = oggi, 1 = domani, ecc.) e "date_label" formato '26 Mag'."""

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
        print(f"  ✗ errore Claude: {e}")
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
            print(f"  ↺ eliminati {deleted} eventi vecchi (oltre {keep_recent_hours}h)")
    except Exception as e:
        print(f"  ⚠ errore cleanup: {e}")


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
            print(f"  ✓ salvato: {e.get('title', '?')[:60]}")
        except Exception as exc:
            print(f"  ✗ errore salvataggio: {exc}")
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

    print(f"\n✓ Fatto: {saved}/{len(events)} eventi salvati.")


if __name__ == "__main__":
    main()
