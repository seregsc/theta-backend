"""
Generatore aggiornamenti normativi per Theta.
- Esecuzione ogni 3 giorni via GitHub Actions cron
- Usa Claude Haiku con web_search per scansionare CONSOB, BdI, ESMA, EIOPA, IVASS, MEF
- Archivio permanente: non cancella nulla, solo aggiunge/aggiorna
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


def fetch_existing_updates(supabase, limit=50):
    """Recupera gli aggiornamenti già nel database (ultimi N) per dare contesto a Claude."""
    try:
        result = supabase.table("regulatory_updates_live") \
            .select("external_id, title, authority, published_date") \
            .order("published_date", desc=True) \
            .limit(limit) \
            .execute()
        return result.data or []
    except Exception as e:
        print(f"  errore fetch aggiornamenti esistenti: {e}")
        return []


def build_existing_context(existing):
    if not existing:
        return "(nessun aggiornamento già nel database — popola la lista da zero)"
    lines = []
    for u in existing[:30]:
        title = u.get("title") or ""
        auth = u.get("authority") or ""
        date = u.get("published_date") or ""
        ext = u.get("external_id") or ""
        lines.append(f"- [{ext}] {auth} — {date} — {title}")
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


def generate_updates(client, existing_context):
    today_str = datetime.now().strftime("%d %B %Y, %A")
    today_iso = datetime.now().strftime("%Y-%m-%d")
    six_weeks_ago = (datetime.now() - timedelta(days=42)).strftime("%Y-%m-%d")

    prompt = f"""Sei un assistente che monitora gli aggiornamenti normativi del settore consulenza finanziaria italiano per professionisti.

DATA OGGI: {today_str} ({today_iso})

OBIETTIVO
Trova e sintetizza gli aggiornamenti normativi più rilevanti pubblicati nelle ultime 6 settimane (dal {six_weeks_ago} a oggi) che impattano il lavoro dei consulenti finanziari italiani.

USA WEB SEARCH per trovare contenuti reali. Cerca nelle fonti ufficiali:
- CONSOB → consob.it/web/area-pubblica/comunicati-stampa
- Banca d'Italia → bancaditalia.it/media/comunicati
- ESMA → esma.europa.eu/news
- EIOPA → eiopa.europa.eu/media/news
- IVASS → ivass.it/normativa/news
- MEF → mef.gov.it/ufficio-stampa
- Agenzia delle Entrate → agenziaentrate.gov.it
- Diritto Bancario → dirittobancario.it
- Risparmio Gestito → risparmiogestito.it

CATEGORIE da usare (esattamente uno tra questi, lowercase):
- "mifid" → MiFID, consulenza, suitability, product governance, profilatura
- "fiscale" → capital gain, tassazione, IRPEF, dichiarazioni, ISA
- "aml" → antiriciclaggio, KYC, segnalazioni sospette
- "esg" → SFDR, taxonomy, sostenibilità, preferenze ESG
- "pensioni" → fondi pensione, TFR, previdenza, Casse
- "trasparenza" → KID, costi cumulati, disclosure
- "vigilanza" → CONSOB sanzioni, ispezioni, albo OCF
- "previdenza" → INPS, contributi, riforme pensionistiche
- "successioni" → eredità, donazioni, passaggio generazionale

AUTHORITY da usare (preserva il nome ufficiale):
- "CONSOB"
- "Banca d'Italia"
- "ESMA"
- "EIOPA"
- "IVASS"
- "MEF"
- "Agenzia delle Entrate"
- "OCF"
- "Commissione Europea"
- "Governo Italiano"

IMPACT_LEVEL — quanto impatta il consulente:
- "HIGH" → cambia il modo di lavorare (es. nuovo obbligo di profilatura, nuova aliquota fiscale, nuova categoria di prodotto)
- "MED" → utile conoscerlo, può richiedere adeguamento procedure
- "LOW" → informativo, contesto utile ma poco operativo

AGGIORNAMENTI GIÀ NEL DATABASE (NON duplicare):
{existing_context}

REGOLE
1. Includi SOLO aggiornamenti realmente pubblicati e verificabili. Se non sei sicuro, NON inventare.
2. Tono delle sintesi: italiano semplice, frasi brevi, professionale ma comprensibile.
3. summary_short: 1-2 frasi (max 200 caratteri).
4. summary_long: 2-3 paragrafi (~600-1000 caratteri) strutturato come: cosa è cambiato, perché riguarda il consulente, cosa fare/sapere.
5. Mantieni external_id stabile e descrittivo: "{{authority-lower}}-{{tipo-doc}}-{{numero-o-anno}}" (es. "consob-delibera-23456-2026", "esma-guidelines-suitability-2026").
6. source_lang: "it" o "en" a seconda del documento originale.
7. effective_date: solo se chiara dal documento, altrimenti null.
8. Genera tra 8 e 15 aggiornamenti rilevanti. Privilegia HIGH impact.

OUTPUT — rispondi SOLO con array JSON, senza markdown, senza commenti:

[
  {{
    "external_id": "consob-comunicato-2026-05-20",
    "title": "Titolo conciso e chiaro dell'aggiornamento",
    "authority": "CONSOB",
    "category": "mifid",
    "published_date": "2026-05-20",
    "effective_date": "2026-07-01",
    "summary_short": "Sintesi in 1-2 frasi che spiega cosa è cambiato.",
    "summary_long": "Paragrafo 1: cosa è cambiato esattamente.\\n\\nParagrafo 2: perché riguarda il consulente finanziario, in italiano semplice.\\n\\nParagrafo 3: cosa fare ora o cosa monitorare.",
    "impact_level": "HIGH",
    "affects_advisors": true,
    "source_url": "https://www.consob.it/...",
    "source_lang": "it",
    "tags": ["suitability", "product_governance"],
    "color": "#0ea5e9"
  }},
  ...
]

COLORI per category (usa esattamente questi):
- mifid → "#0ea5e9" (blu)
- fiscale → "#10b981" (verde)
- aml → "#dc2626" (rosso)
- esg → "#22c55e" (verde acceso)
- pensioni → "#8b5cf6" (viola scuro)
- trasparenza → "#f59e0b" (ambra)
- vigilanza → "#ef4444" (rosso)
- previdenza → "#a855f7" (viola)
- successioni → "#ec4899" (rosa)

Genera la lista adesso."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=10000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 12,
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


def validate_update(u):
    """Valida un aggiornamento prima del salvataggio."""
    required = ["external_id", "title", "authority", "category", "published_date"]
    for field in required:
        if not u.get(field):
            return False, f"campo mancante: {field}"
    try:
        datetime.strptime(u["published_date"], "%Y-%m-%d")
    except ValueError:
        return False, "formato published_date non valido (atteso YYYY-MM-DD)"
    if u.get("effective_date"):
        try:
            datetime.strptime(u["effective_date"], "%Y-%m-%d")
        except ValueError:
            return False, "formato effective_date non valido (atteso YYYY-MM-DD)"
    valid_impact = ["HIGH", "MED", "LOW"]
    if u.get("impact_level") and u["impact_level"] not in valid_impact:
        return False, f"impact_level non valido: {u['impact_level']}"
    return True, None


def save_update(supabase, u):
    """Upsert su external_id."""
    row = {
        "external_id": u.get("external_id"),
        "title": u.get("title"),
        "authority": u.get("authority"),
        "category": u.get("category"),
        "published_date": u.get("published_date"),
        "effective_date": u.get("effective_date"),
        "summary_short": u.get("summary_short"),
        "summary_long": u.get("summary_long"),
        "impact_level": u.get("impact_level") or "MED",
        "affects_advisors": u.get("affects_advisors", True),
        "source_url": u.get("source_url"),
        "source_lang": u.get("source_lang") or "it",
        "tags": u.get("tags") or [],
        "color": u.get("color") or "#0ea5e9",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Rimuovi None per non sovrascrivere campi esistenti
    row = {k: v for k, v in row.items() if v is not None}

    try:
        supabase.table("regulatory_updates_live") \
            .upsert(row, on_conflict="external_id") \
            .execute()
        return True
    except Exception as exc:
        print(f"   errore salvataggio {u.get('external_id')}: {exc}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print("1. Recupero aggiornamenti già esistenti...")
    existing = fetch_existing_updates(supabase)
    print(f"   {len(existing)} aggiornamenti nel database\n")
    existing_context = build_existing_context(existing)

    print("2. Generazione aggiornamenti via Claude Haiku + web_search...")
    updates = generate_updates(client, existing_context)

    if not updates or not isinstance(updates, list):
        print("Generazione fallita.")
        return

    print(f"   {len(updates)} aggiornamenti generati\n")

    print("3. Validazione e salvataggio...")
    saved = 0
    skipped = 0
    for u in updates:
        valid, err = validate_update(u)
        if not valid:
            print(f"   SKIP {u.get('external_id') or '?'}: {err}")
            skipped += 1
            continue
        if save_update(supabase, u):
            saved += 1

    print(f"\n   {saved} salvati, {skipped} skippati")
    print("\nFine.")


if __name__ == "__main__":
    main()
