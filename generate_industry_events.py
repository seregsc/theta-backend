"""
Generatore eventi del settore consulenza finanziaria per Theta.
- Esecuzione settimanale (lunedì 06:00 italiane via GitHub Actions cron)
- Usa Claude Haiku con web_search per scoprire eventi nuovi e verificare date
- Pulizia: cancella eventi conclusi da più di 7 giorni
- Upsert su external_id per evitare duplicati
"""
import os
import json
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5"


def fetch_existing_events(supabase):
    """Recupera tutti gli eventi esistenti per dare contesto a Claude."""
    try:
        result = supabase.table("industry_events_live") \
            .select("external_id, title, start_date, end_date, location") \
            .order("start_date", desc=False) \
            .execute()
        return result.data or []
    except Exception as e:
        print(f"  errore fetch eventi esistenti: {e}")
        return []


def cleanup_old_events(supabase):
    """Cancella eventi conclusi da più di 7 giorni."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    try:
        result = supabase.table("industry_events_live") \
            .delete() \
            .lt("end_date", cutoff) \
            .execute()
        deleted_count = len(result.data) if result.data else 0
        print(f"   {deleted_count} eventi obsoleti rimossi (end_date < {cutoff})")
        return deleted_count
    except Exception as e:
        print(f"   errore pulizia: {e}")
        return 0


def build_existing_context(existing):
    if not existing:
        return "(nessun evento già nel database — popola la lista da zero)"
    lines = []
    for e in existing[:30]:
        title = e.get("title") or ""
        start = e.get("start_date") or ""
        loc = e.get("location") or ""
        ext = e.get("external_id") or ""
        lines.append(f"- [{ext}] {title} — {start} ({loc})")
    return "\n".join(lines)


def parse_response(text):
    """Estrae array JSON dalla risposta."""
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
        print(f"  primi 400 char: {text[:400]}")
        return None


def generate_events(client, existing_context):
    today_str = datetime.now().strftime("%d %B %Y, %A")
    today_iso = datetime.now().strftime("%Y-%m-%d")
    next_year = datetime.now().year + 1

    prompt = f"""Sei un assistente che cura il calendario degli eventi professionali per consulenti finanziari italiani.

DATA OGGI: {today_str} ({today_iso})

OBIETTIVO
Genera una lista di TUTTI gli eventi del settore consulenza finanziaria e wealth management in Italia per i prossimi 12 mesi (eventi in corso o futuri, fino a circa {next_year}).

USA WEB SEARCH per verificare date e dettagli ufficiali quando possibile. Cerca su:
- Anasf (Associazione Nazionale Consulenti Finanziari) — sito anasf.it, consulentia.com
- Assogestioni — assogestioni.it, salonedelrisparmio.com
- EFPA Italia — efpa-italia.it
- ITForum — itforum.it
- AIPB (Associazione Italiana Private Banking)
- AIAF (Associazione Italiana Analisti Finanziari)
- FundsPeople Italia
- Bluerating, Advisor Online, FocusRisparmio (eventi sponsorizzati)
- YouFinance, Trader's Magazine

EVENTI GIÀ NEL DATABASE (puoi confermarli o aggiornarne dati):
{existing_context}

REGOLE
1. Includi solo eventi rilevanti per consulenti finanziari, private banker, wealth manager, advisor: convegni nazionali, fiere settore, masterclass certificate (EFPA, CFA), workshop di società di gestione (BlackRock, Fidelity, Amundi, JP Morgan, Pimco, Vanguard).
2. NON includere eventi generici (es. Web Summit, fiere tech generali) né piccole consulenze locali.
3. Dai PRIORITÀ a eventi futuri rispetto ad altri (almeno 10 eventi futuri se possibile).
4. Per ogni evento usa SOLO date che hai verificato o che sono dichiarate ufficialmente.
5. Se non hai certezza sulla data esatta ma sai che l'evento ricorre tipicamente in un mese (es. ConsulenTia in marzo), inserisci comunque l'evento con date orientative e segnala "Date orientative basate su edizioni precedenti" in note.
6. Mantieni external_id stabile per gli eventi ricorrenti (es. "consulentia-2026", "salone-risparmio-2027").

OUTPUT — rispondi SOLO con array JSON, senza markdown, senza commenti:

[
  {{
    "external_id": "consulentia-2027",
    "title": "ConsulenTia 2027",
    "organizer": "Anasf",
    "start_date": "2027-03-16",
    "end_date": "2027-03-18",
    "location": "Roma",
    "venue": "Auditorium Parco della Musica",
    "type": "Convegno",
    "format": "Presenza + streaming",
    "description": "Descrizione 2-3 frasi in italiano sull'evento, tema e target.",
    "url": "https://www.consulentia.com/",
    "color": "#a855f7",
    "note": null
  }},
  ...
]

COLORI da usare (cycle tra questi):
- "#a855f7" (viola) — convegni Anasf
- "#0ea5e9" (blu) — Assogestioni / Salone del Risparmio
- "#10b981" (verde) — fiere generaliste
- "#ec4899" (rosa) — EFPA Italia
- "#f59e0b" (ambra) — ITForum
- "#dc2626" (rosso) — eventi private banking
- "#06b6d4" (ciano) — webinar e masterclass digitali

TIPO usa SOLO uno tra:
- "Convegno"
- "Fiera"
- "Workshop"
- "Masterclass"
- "Webinar"
- "Fiera + Convegno"
- "Convegno + Workshop"

FORMAT usa SOLO uno tra:
- "Presenza"
- "Online"
- "Presenza + streaming"
- "Presenza + online"
- "Webinar online"

DATE: formato YYYY-MM-DD obbligatorio.

Genera la lista completa adesso."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
            }],
            messages=[{"role": "user", "content": prompt}],
        )

        # Concatena tutti i blocchi di testo della risposta
        text_blocks = []
        for block in response.content:
            if block.type == "text":
                text_blocks.append(block.text)
        text = "\n".join(text_blocks).strip()

        return parse_response(text)
    except Exception as e:
        print(f"  errore Claude: {e}")
        return None


def validate_event(e):
    """Valida un evento prima del salvataggio."""
    required = ["external_id", "title", "start_date", "end_date"]
    for field in required:
        if not e.get(field):
            return False, f"campo mancante: {field}"
    try:
        start = datetime.strptime(e["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(e["end_date"], "%Y-%m-%d").date()
        if end < start:
            return False, "end_date prima di start_date"
    except ValueError:
        return False, "formato data non valido (atteso YYYY-MM-DD)"
    return True, None


def save_event(supabase, e):
    """Upsert su external_id."""
    row = {
        "external_id": e.get("external_id"),
        "title": e.get("title"),
        "organizer": e.get("organizer"),
        "start_date": e.get("start_date"),
        "end_date": e.get("end_date"),
        "location": e.get("location"),
        "venue": e.get("venue"),
        "type": e.get("type"),
        "format": e.get("format"),
        "description": e.get("description"),
        "url": e.get("url"),
        "color": e.get("color") or "#0ea5e9",
        "note": e.get("note"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Rimuovi i None per non sovrascrivere campi esistenti con null
    row = {k: v for k, v in row.items() if v is not None}

    try:
        supabase.table("industry_events_live") \
            .upsert(row, on_conflict="external_id") \
            .execute()
        return True
    except Exception as exc:
        print(f"   errore salvataggio {e.get('external_id')}: {exc}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print("1. Pulizia eventi obsoleti (conclusi da > 7 giorni)...")
    cleanup_old_events(supabase)
    print()

    print("2. Recupero eventi già esistenti...")
    existing = fetch_existing_events(supabase)
    print(f"   {len(existing)} eventi nel database\n")
    existing_context = build_existing_context(existing)

    print("3. Generazione/aggiornamento eventi via Claude Haiku + web_search...")
    events = generate_events(client, existing_context)

    if not events or not isinstance(events, list):
        print("Generazione fallita.")
        return

    print(f"   {len(events)} eventi generati\n")

    print("4. Validazione e salvataggio...")
    saved = 0
    skipped = 0
    for e in events:
        valid, err = validate_event(e)
        if not valid:
            print(f"   SKIP {e.get('external_id') or '?'}: {err}")
            skipped += 1
            continue
        if save_event(supabase, e):
            saved += 1

    print(f"\n   {saved} salvati, {skipped} skippati")
    print("\nFine.")


if __name__ == "__main__":
    main()
