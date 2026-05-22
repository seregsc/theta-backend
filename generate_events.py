"""
Generatore di eventi economici live per Theta.
FONTE DATI: Forex Factory JSON settimanale (gratuito, dati ufficiali).
URL: https://nfs.faireconomy.media/ff_calendar_thisweek.json

Per ogni evento di alta rilevanza:
- Dati strutturati (data, ora, paese, impatto, valori): da Forex Factory
- Analisi italiana (summary, baseline, surprise, what to watch, preparation): generata da Claude
"""
import os
import json
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"

FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
KEEP_HOURS = 240  # 10 giorni di archivio

# Filtriamo solo paesi rilevanti per consulenti italiani
RELEVANT_COUNTRIES = {"USD", "EUR", "GBP", "CNY", "JPY", "CHF", "CAD"}

# Massimo eventi da generare (per non sforare token Claude)
MAX_EVENTS = 12


MONTHS_IT = ["Gen", "Feb", "Mar", "Apr", "Mag", "Giu", "Lug", "Ago", "Set", "Ott", "Nov", "Dic"]

# Mappatura Forex Factory country code → Theta region/label
COUNTRY_MAP = {
    "USD": {"region": "USA", "label": "Macro USA"},
    "EUR": {"region": "Eurozona", "label": "Macro EU"},
    "GBP": {"region": "UK", "label": "Macro UK"},
    "JPY": {"region": "Giappone", "label": "Macro JP"},
    "CNY": {"region": "Cina", "label": "Macro CN"},
    "CHF": {"region": "Svizzera", "label": "Macro CH"},
    "CAD": {"region": "Canada", "label": "Macro CA"},
}


def fetch_forex_factory_events():
    """Scarica il JSON settimanale di Forex Factory."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Theta backend)"}
        r = requests.get(FOREX_FACTORY_URL, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        print(f"Forex Factory: ricevuti {len(data)} eventi totali")
        return data
    except Exception as e:
        print(f"Errore Forex Factory: {e}")
        return []


def filter_relevant_events(events):
    """Filtra solo eventi di alta importanza e paesi rilevanti."""
    relevant = []
    for e in events:
        country = e.get("country", "")
        impact = e.get("impact", "").lower()
        if country not in RELEVANT_COUNTRIES:
            continue
        if impact not in ("high", "medium"):
            continue
        relevant.append(e)
    print(f"Eventi rilevanti dopo filtro: {len(relevant)}")
    return relevant


def normalize_event(e):
    """Normalizza un evento Forex Factory in formato per il prompt Claude."""
    country = e.get("country", "USD")
    title = e.get("title", "")
    date_str = e.get("date", "")
    impact = e.get("impact", "Medium").upper()
    forecast = e.get("forecast", "")
    previous = e.get("previous", "")
    actual = e.get("actual", "")

    # Parse data ISO (es: "2026-05-22T08:30:00-04:00")
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        # Converti in CET (UTC+1 / UTC+2 estate). Approx: usiamo UTC+2.
        dt_cet = dt.astimezone(timezone(timedelta(hours=2)))
        event_date = dt_cet.strftime("%Y-%m-%d")
        date_label = f"{dt_cet.day} {MONTHS_IT[dt_cet.month - 1]}"
        event_time = dt_cet.strftime("%H:%M CET")
        days_from_now = (dt_cet.date() - datetime.now(timezone.utc).date()).days
    except Exception as ex:
        print(f"  errore parse data {date_str}: {ex}")
        event_date = None
        date_label = ""
        event_time = ""
        days_from_now = 0

    country_info = COUNTRY_MAP.get(country, {"region": country, "label": f"Macro {country}"})

    return {
        "ff_title": title,
        "ff_country": country,
        "event_date": event_date,
        "date_label": date_label,
        "event_time": event_time,
        "days_from_now": days_from_now,
        "type": "macro",
        "type_label": country_info["label"],
        "importance": impact,
        "region": country_info["region"],
        "forecast": forecast,
        "previous": previous,
        "actual": actual,
    }


def generate_italian_analysis(client, event):
    """Chiede a Claude di tradurre il titolo e generare l'analisi italiana."""
    forecast_str = f"Consenso analisti: {event['forecast']}" if event.get("forecast") else ""
    previous_str = f"Valore precedente: {event['previous']}" if event.get("previous") else ""
    extra = "\n".join([s for s in [forecast_str, previous_str] if s])

    prompt = f"""Sei un analista finanziario senior italiano. Devi tradurre e arricchire un evento del calendario economico per il consulente finanziario che usa Theta.

EVENTO ORIGINALE (Forex Factory)
Titolo: {event['ff_title']}
Paese: {event['ff_country']}
Importanza: {event['importance']}
Data: {event['event_date']} alle {event['event_time']}
{extra}

ISTRUZIONI
- Traduci il titolo in italiano in modo informativo e neutrale.
- Genera analisi in italiano professionale, lessico finanziario.
- Sii fattuale: usa SOLO informazioni presenti. Non inventare numeri o valori.
- Tono neutro nei primi blocchi.
- Nella PREPARATION (preparazione) parla direttamente al consulente al TU («valuta», «monitora», «considera», «alleggerisci»). Mai «i consulenti potrebbero».
- NON usare markdown, asterischi, cancelletti.
- NON usare virgolette doppie all'interno delle stringhe: usa virgolette singole o «caporali».
- NON usare newline dentro le stringhe.

OUTPUT — rispondi SOLO con JSON puro, un singolo oggetto, senza preamboli né markdown:

{{
  "title": "Titolo italiano breve (max 90 caratteri)",
  "summary": "Descrizione 3-5 frasi (400-700 caratteri): cosa è l'evento, perché conta per i mercati, qual è il consensus attuale.",
  "baseline_scenario": "Scenario base 3-4 frasi (300-500 caratteri): cosa è probabile che succeda, reazione attesa dei mercati.",
  "surprise_scenario": "Scenario sorpresa 3-4 frasi (300-500 caratteri): cosa succederebbe se il dato sorprendesse.",
  "impacted_sectors": ["ai", "bonds"],
  "impacted_tickers": ["NVDA", "TLT"],
  "what_to_watch": ["Punto 1", "Punto 2", "Punto 3", "Punto 4"],
  "preparation": "Suggerimento operativo 3-4 frasi (300-500 caratteri) al TU («Valuta di...», «Monitora attentamente...»)."
}}"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
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
        return json.loads(text)
    except Exception as e:
        print(f"    errore Claude: {e}")
        return None


def cleanup_old_events(supabase, keep_recent_hours=240):
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


def check_existing_event(supabase, ff_title, event_date):
    """Verifica se un evento è già nel database (per non duplicare)."""
    try:
        result = supabase.table("events_live") \
            .select("id") \
            .eq("event_date", event_date) \
            .ilike("title", f"%{ff_title[:30]}%") \
            .execute()
        return len(result.data) > 0
    except Exception:
        return False


def save_event(supabase, e, analysis):
    row = {
        "event_date": e.get("event_date"),
        "date_label": e.get("date_label"),
        "event_time": e.get("event_time"),
        "days_from_now": e.get("days_from_now"),
        "type": e.get("type"),
        "type_label": e.get("type_label"),
        "importance": e.get("importance"),
        "region": e.get("region"),
        "title": analysis.get("title"),
        "summary": analysis.get("summary"),
        "baseline_scenario": analysis.get("baseline_scenario"),
        "surprise_scenario": analysis.get("surprise_scenario"),
        "impacted_sectors": analysis.get("impacted_sectors"),
        "impacted_tickers": analysis.get("impacted_tickers"),
        "what_to_watch": analysis.get("what_to_watch"),
        "preparation": analysis.get("preparation"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("events_live").insert(row).execute()
        return True
    except Exception as exc:
        print(f"    errore salvataggio: {exc}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print("1. Cleanup eventi vecchi (oltre 10 giorni)...")
    cleanup_old_events(supabase, keep_recent_hours=KEEP_HOURS)

    print("2. Scarico calendario da Forex Factory...")
    raw_events = fetch_forex_factory_events()
    if not raw_events:
        print("Nessun dato. Esco.")
        return

    relevant = filter_relevant_events(raw_events)
    if not relevant:
        print("Nessun evento rilevante trovato. Esco.")
        return

    # Ordina per data ascendente (eventi più imminenti per primi)
    relevant.sort(key=lambda x: x.get("date", ""))

    # Limita al massimo configurato
    relevant = relevant[:MAX_EVENTS]
    print(f"Elaboro {len(relevant)} eventi più imminenti.\n")

    print(f"3. Genero analisi italiana e salvo (uno per volta)...")
    saved = 0
    skipped = 0
    failed = 0

    for i, e in enumerate(relevant, 1):
        normalized = normalize_event(e)
        if not normalized.get("event_date"):
            failed += 1
            continue

        # Salta se già nel database
        if check_existing_event(supabase, normalized["ff_title"], normalized["event_date"]):
            skipped += 1
            print(f"  [{i}/{len(relevant)}] · gia in db: {normalized['ff_title'][:60]}")
            continue

        print(f"  [{i}/{len(relevant)}] {normalized['ff_title'][:60]}")
        analysis = generate_italian_analysis(client, normalized)

        if not analysis or not analysis.get("title"):
            failed += 1
            print(f"    saltato: analisi fallita")
            continue

        if save_event(supabase, normalized, analysis):
            saved += 1
            print(f"    ok: {analysis.get('title', '?')[:60]}")
        else:
            failed += 1

    print(f"\nFatto: {saved} nuovi salvati, {skipped} già esistenti, {failed} falliti.")


if __name__ == "__main__":
    main()
