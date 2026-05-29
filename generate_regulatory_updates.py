"""
Generatore aggiornamenti normativi per Theta.
- Esecuzione ogni 3 giorni via GitHub Actions cron
- Usa Claude Haiku con web_search per scansionare CONSOB, BdI, ESMA, EIOPA, IVASS, MEF
- Archivio permanente: non cancella nulla, solo aggiunge/aggiorna
- Upsert su external_id per evitare duplicati

FIX DATA: la published_date viene validata per rifiutare date impossibili
(nel futuro o troppo vecchie). Claude a volte allucina la data di un documento;
ora viene scartato l'aggiornamento con data non plausibile invece di salvarlo.
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

# Finestra di plausibilita per published_date: le news normative devono essere
# delle ultime ~10 settimane e MAI nel futuro.
MAX_AGE_DAYS = 70          # ~10 settimane indietro
FUTURE_TOLERANCE_DAYS = 1  # tollera al massimo "domani" per fusi orari


def fetch_existing_updates(supabase, limit=50):
    """Recupera gli aggiornamenti gia nel database (ultimi N) per dare contesto a Claude."""
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
        return "(nessun aggiornamento gia nel database - popola la lista da zero)"
    lines = []
    for u in existing[:30]:
        title = u.get("title") or ""
        auth = u.get("authority") or ""
        date = u.get("published_date") or ""
        ext = u.get("external_id") or ""
        lines.append(f"- [{ext}] {auth} - {date} - {title}")
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
    today = datetime.now()
    today_str = today.strftime("%d/%m/%Y")
    today_iso = today.strftime("%Y-%m-%d")
    six_weeks_ago = (today - timedelta(days=42)).strftime("%Y-%m-%d")
    current_year = today.year

    prompt = f"""Sei un assistente che monitora gli aggiornamenti normativi del settore consulenza finanziaria italiano per professionisti.

DATA DI OGGI: {today_str} (formato ISO: {today_iso}). Siamo nell'anno {current_year}.

OBIETTIVO
Trova e sintetizza gli aggiornamenti normativi piu rilevanti pubblicati nelle ultime 6 settimane (dal {six_weeks_ago} a {today_iso}) che impattano il lavoro dei consulenti finanziari italiani.

USA WEB SEARCH per trovare contenuti reali. Cerca nelle fonti ufficiali:
- CONSOB -> consob.it/web/area-pubblica/comunicati-stampa
- Banca d'Italia -> bancaditalia.it/media/comunicati
- ESMA -> esma.europa.eu/news
- EIOPA -> eiopa.europa.eu/media/news
- IVASS -> ivass.it/normativa/news
- MEF -> mef.gov.it/ufficio-stampa
- Agenzia delle Entrate -> agenziaentrate.gov.it
- Diritto Bancario -> dirittobancario.it
- Risparmio Gestito -> risparmiogestito.it

REGOLA CRITICA SULLE DATE (la piu importante):
- published_date DEVE essere la data REALE di pubblicazione del documento, letta direttamente dalla fonte. NON inventarla, NON stimarla, NON arrotondarla.
- published_date deve essere compresa tra {six_weeks_ago} e {today_iso} (le ultime 6 settimane). MAI una data nel futuro. MAI una data di un altro anno.
- Se non riesci a determinare con certezza la data di pubblicazione dalla fonte, SCARTA quell'aggiornamento: non includerlo affatto.
- Ricontrolla ogni published_date prima di scriverla: l'anno deve essere {current_year}, e il giorno/mese devono corrispondere esattamente a quanto riportato dalla fonte ufficiale.
- effective_date (data di entrata in vigore) puo essere nel futuro: e l'unica data che puo superare oggi.

REGOLA CRITICA SUL LINK (source_url):
- source_url DEVE essere l'URL DIRETTO della pagina specifica del documento/comunicato (es. la pagina della singola delibera, del comunicato stampa, delle linee guida), NON la homepage generica del sito.
- ESEMPIO SBAGLIATO: "https://www.consob.it" oppure "https://www.consob.it/web/area-pubblica/comunicati-stampa" (pagina indice).
- ESEMPIO CORRETTO: "https://www.consob.it/web/area-pubblica/dettaglio-news?viewId=news_comunicato_xyz" (la pagina del singolo documento citato).
- Prendi l'URL ESATTO dal risultato di ricerca web che hai usato per trovare quella specifica normativa. Deve portare l'utente direttamente al documento, non a una lista o alla home.
- Se non hai un URL diretto al documento specifico ma solo la homepage, SCARTA quell'aggiornamento (non includerlo): un link generico non e utile al consulente.

CATEGORIE da usare (esattamente uno tra questi, lowercase):
- "mifid" -> MiFID, consulenza, suitability, product governance, profilatura
- "fiscale" -> capital gain, tassazione, IRPEF, dichiarazioni, ISA
- "aml" -> antiriciclaggio, KYC, segnalazioni sospette
- "esg" -> SFDR, taxonomy, sostenibilita, preferenze ESG
- "pensioni" -> fondi pensione, TFR, previdenza, Casse
- "trasparenza" -> KID, costi cumulati, disclosure
- "vigilanza" -> CONSOB sanzioni, ispezioni, albo OCF
- "previdenza" -> INPS, contributi, riforme pensionistiche
- "successioni" -> eredita, donazioni, passaggio generazionale

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

IMPACT_LEVEL - quanto impatta il consulente:
- "HIGH" -> cambia il modo di lavorare (es. nuovo obbligo di profilatura, nuova aliquota fiscale, nuova categoria di prodotto)
- "MED" -> utile conoscerlo, puo richiedere adeguamento procedure
- "LOW" -> informativo, contesto utile ma poco operativo

AGGIORNAMENTI GIA NEL DATABASE (NON duplicare):
{existing_context}

REGOLE
1. Includi SOLO aggiornamenti realmente pubblicati e verificabili. Se non sei sicuro, NON inventare.
2. Tono delle sintesi: italiano semplice, frasi brevi, professionale ma comprensibile.
3. summary_short: 1-2 frasi (max 200 caratteri).
4. summary_long: 2-3 paragrafi (~600-1000 caratteri) strutturato come: cosa e cambiato, perche riguarda il consulente, cosa fare/sapere.
5. Mantieni external_id stabile e descrittivo: "{{authority-lower}}-{{tipo-doc}}-{{numero-o-anno}}" (es. "consob-delibera-23456-{current_year}", "esma-guidelines-suitability-{current_year}").
6. source_lang: "it" o "en" a seconda del documento originale.
7. effective_date: solo se chiara dal documento, altrimenti null.
8. Genera tra 8 e 15 aggiornamenti rilevanti. Privilegia HIGH impact.

OUTPUT - rispondi SOLO con array JSON, senza markdown, senza commenti:

[
  {{
    "external_id": "consob-comunicato-{current_year}-05-20",
    "title": "Titolo conciso e chiaro dell'aggiornamento",
    "authority": "CONSOB",
    "category": "mifid",
    "published_date": "{current_year}-05-20",
    "effective_date": "{current_year}-07-01",
    "summary_short": "Sintesi in 1-2 frasi che spiega cosa e cambiato.",
    "summary_long": "Paragrafo 1: cosa e cambiato esattamente.\\n\\nParagrafo 2: perche riguarda il consulente finanziario, in italiano semplice.\\n\\nParagrafo 3: cosa fare ora o cosa monitorare.",
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
- mifid -> "#0ea5e9" (blu)
- fiscale -> "#10b981" (verde)
- aml -> "#dc2626" (rosso)
- esg -> "#22c55e" (verde acceso)
- pensioni -> "#8b5cf6" (viola scuro)
- trasparenza -> "#f59e0b" (ambra)
- vigilanza -> "#ef4444" (rosso)
- previdenza -> "#a855f7" (viola)
- successioni -> "#ec4899" (rosa)

Genera la lista adesso. Ricorda: ogni published_date deve essere reale, dell'anno {current_year}, e mai nel futuro."""

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

    # Validazione formato + PLAUSIBILITA della published_date
    try:
        pub = datetime.strptime(u["published_date"], "%Y-%m-%d")
    except ValueError:
        return False, "formato published_date non valido (atteso YYYY-MM-DD)"

    now = datetime.now()
    # Rifiuta date nel futuro (oltre la tolleranza per fusi orari)
    if pub.date() > (now + timedelta(days=FUTURE_TOLERANCE_DAYS)).date():
        return False, f"published_date nel futuro: {u['published_date']} (oggi {now.strftime('%Y-%m-%d')})"
    # Rifiuta date troppo vecchie (probabile allucinazione/anno sbagliato)
    if pub.date() < (now - timedelta(days=MAX_AGE_DAYS)).date():
        return False, f"published_date troppo vecchia: {u['published_date']} (limite {MAX_AGE_DAYS} giorni)"

    if u.get("effective_date"):
        try:
            datetime.strptime(u["effective_date"], "%Y-%m-%d")
        except ValueError:
            return False, "formato effective_date non valido (atteso YYYY-MM-DD)"

    valid_impact = ["HIGH", "MED", "LOW"]
    if u.get("impact_level") and u["impact_level"] not in valid_impact:
        return False, f"impact_level non valido: {u['impact_level']}"

    # Rifiuta source_url che sono solo homepage o pagine-indice generiche.
    # Un link utile deve puntare alla pagina del documento, non a una lista.
    src = (u.get("source_url") or "").strip()
    if src:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(src)
            path = (parsed.path or "").strip("/")
            has_query = bool(parsed.query)  # un ?id=... di solito indica una pagina specifica
            segments = [s for s in path.split("/") if s]
            last = segments[-1].lower() if segments else ""
            # Pagine indice/home da scartare (se l'URL finisce qui senza query specifica)
            index_pages = {
                "", "web", "home", "index", "index.html", "news", "media",
                "comunicati", "comunicati-stampa", "ufficio-stampa", "normativa",
                "area-pubblica", "press", "press-releases", "newsroom",
            }
            if not has_query and (last in index_pages or len(path) < 5):
                return False, f"source_url troppo generico (homepage/indice): {src}"
        except Exception:
            pass
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

    print("1. Recupero aggiornamenti gia esistenti...")
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
