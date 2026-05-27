"""
Generatore reading list curata per consulenti finanziari di Theta.
- Esecuzione settimanale (lunedì 07:00 italiane via GitHub Actions cron)
- Usa Claude Haiku con web_search per trovare articoli, paper, podcast rilevanti
- Pulizia: articoli pubblicati da più di 60 giorni vengono cancellati
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


def fetch_existing_articles(supabase, limit=80):
    """Recupera articoli già esistenti (ultimi N) per dare contesto a Claude."""
    try:
        result = supabase.table("curated_articles_live") \
            .select("external_id, title, source, published_date") \
            .order("published_date", desc=True) \
            .limit(limit) \
            .execute()
        return result.data or []
    except Exception as e:
        print(f"  errore fetch articoli esistenti: {e}")
        return []


def cleanup_old_articles(supabase):
    """Cancella articoli pubblicati da più di 60 giorni."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    try:
        result = supabase.table("curated_articles_live") \
            .delete() \
            .lt("published_date", cutoff) \
            .execute()
        deleted_count = len(result.data) if result.data else 0
        print(f"   {deleted_count} articoli obsoleti rimossi (published_date < {cutoff})")
        return deleted_count
    except Exception as e:
        print(f"   errore pulizia: {e}")
        return 0


def build_existing_context(existing):
    if not existing:
        return "(nessun articolo già nel database — popola la lista da zero)"
    lines = []
    for a in existing[:40]:
        title = a.get("title") or ""
        src = a.get("source") or ""
        date = a.get("published_date") or ""
        ext = a.get("external_id") or ""
        lines.append(f"- [{ext}] {src} — {date} — {title}")
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


def generate_articles(client, existing_context):
    today_str = datetime.now().strftime("%d %B %Y, %A")
    today_iso = datetime.now().strftime("%Y-%m-%d")
    six_weeks_ago = (datetime.now() - timedelta(days=42)).strftime("%Y-%m-%d")

    prompt = f"""Sei un editor che cura una reading list settimanale per consulenti finanziari italiani professionisti.

DATA OGGI: {today_str} ({today_iso})

OBIETTIVO
Trova 10-15 articoli, paper, ricerche, podcast pubblicati nelle ultime 6 settimane (dal {six_weeks_ago} a oggi) che un consulente finanziario italiano dovrebbe leggere/ascoltare per fare meglio il proprio lavoro.

USA WEB SEARCH per trovare contenuti REALI e RECENTI. Fonti prioritarie:

ASSET MANAGER & RICERCA ISTITUZIONALE (en):
- BlackRock Investment Institute — blackrock.com/institute
- Vanguard Research — corporate.vanguard.com/research
- JP Morgan Asset Management — Market Insights
- Pimco Insights — pimco.com/en-us/insights
- Amundi Research Center — amundi.com/research
- Pictet Asset Management — Insights
- Goldman Sachs Asset Management — insights
- Robeco — Insights

ISTITUZIONALI ITALIA / EU (it/en):
- Banca d'Italia — Temi di discussione e Note di stabilità
- BCE — Working papers e Financial Stability Review
- FMI — IMF Working Papers
- Banca dei Regolamenti Internazionali (BIS) — Quarterly Review

MEDIA SPECIALIZZATI ITALIA (it):
- Morningstar Italia — morningstar.it
- FundsPeople Italia — fundspeople.com/it
- Bluerating — bluerating.com
- Advisor Online — advisoronline.it
- FocusRisparmio — focusrisparmio.com
- Wall Street Italia — wallstreetitalia.com
- Milano Finanza — milanofinanza.it
- Diritto Bancario — dirittobancario.it (per contenuti normativi/commentary)
- Citywire Italia

CONSULTING & STRATEGY (en):
- McKinsey & Co. — Financial Services Insights
- Boston Consulting Group — Wealth Management Reports
- Deloitte — Wealth & Asset Management Outlook
- PwC — Asset & Wealth Management

PODCAST E ALTRI FORMATI:
- Bluerating Podcast
- FocusRisparmio Podcast
- WSI Talk
- The Compound (Ritholtz Wealth)
- Animal Spirits

ARTICOLI GIÀ NEL DATABASE (NON duplicare):
{existing_context}

CATEGORIE (esattamente uno tra questi, lowercase):
- "strategia" → outlook trimestrali, asset allocation, geopolitica
- "mercati" → analisi azionario, obbligazionario, valute, commodities
- "asset_class" → ETF, private markets, alternative, gold
- "wealth" → wealth management, passaggio generazionale, family office
- "comportamento" → finanza comportamentale, bias, neuroeconomia
- "pratica" → relazione cliente, vendita, marketing, business
- "normativa" → commentary normativo, opinioni esperti (NON aggiornamenti CONSOB ufficiali — quelli vanno altrove)
- "innovazione" → AI, fintech, blockchain, robo-advisor

FORMAT (esattamente uno):
- "articolo" → articolo giornalistico, blog post
- "paper" → paper accademico, ricerca scientifica
- "report" → report istituzionale, outlook annuale
- "podcast" → episodio podcast (con durata)
- "video" → intervista video, webinar registrato

IMPORTANCE — quanto è imperdibile:
- "HIGH" → pubblicazione di rilievo, autori top, contenuto unico
- "MED" → buona qualità, vale la pena
- "LOW" → contesto utile ma non urgente

REGOLE
1. SOLO contenuti realmente esistenti e verificabili — usa web_search per confermare URL e date.
2. NO contenuti vecchi, scaduti o paywall completamente bloccati.
3. Bilancia il mix: almeno 3 articoli leggeri (italiani), 3 ricerche pesanti (inglesi), 1-2 paper accademici, 1-2 podcast.
4. Bilancia anche le categorie: non più di 3 articoli sulla stessa categoria.
5. Se trovi l'og:image o un'immagine di copertina nell'URL/anteprima, includila in image_url.
6. reading_time: stima realistica in minuti (1500 parole = ~7 min).
7. summary: 2-3 frasi che spieghino DI COSA parla l'articolo.
8. why_relevant: 1-2 frasi che spieghino PERCHÉ il consulente dovrebbe leggerlo.
9. Mantieni external_id stabile e descrittivo (es. "blackrock-q2-2026-geopolitical-dashboard").

OUTPUT — rispondi SOLO con array JSON, senza markdown, senza commenti:

[
  {{
    "external_id": "blackrock-q2-2026-outlook",
    "title": "Titolo originale dell'articolo",
    "source": "BlackRock Investment Institute",
    "author": "BlackRock Research Team",
    "published_date": "2026-04-15",
    "category": "strategia",
    "format": "report",
    "language": "en",
    "summary": "Sintesi 2-3 frasi di cosa parla l'articolo.",
    "why_relevant": "1-2 frasi: perché il consulente deve leggerlo.",
    "reading_time": 12,
    "image_url": "https://...jpg",
    "source_url": "https://www.blackrock.com/institute/...",
    "tags": ["geopolitica", "asset_allocation", "outlook"],
    "color": "#0ea5e9",
    "importance": "HIGH"
  }},
  ...
]

COLORI per category (usa esattamente questi):
- strategia → "#0ea5e9" (blu)
- mercati → "#10b981" (verde)
- asset_class → "#06b6d4" (ciano)
- wealth → "#8b5cf6" (viola scuro)
- comportamento → "#ec4899" (rosa)
- pratica → "#f59e0b" (ambra)
- normativa → "#dc2626" (rosso)
- innovazione → "#a855f7" (viola)

Genera la lista adesso."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=12000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 15,
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


def validate_article(a):
    """Valida un articolo prima del salvataggio."""
    required = ["external_id", "title", "source", "published_date", "category", "format", "source_url"]
    for field in required:
        if not a.get(field):
            return False, f"campo mancante: {field}"
    try:
        datetime.strptime(a["published_date"], "%Y-%m-%d")
    except ValueError:
        return False, "formato published_date non valido"
    valid_categories = ["strategia", "mercati", "asset_class", "wealth", "comportamento", "pratica", "normativa", "innovazione"]
    if a["category"] not in valid_categories:
        return False, f"categoria non valida: {a['category']}"
    valid_formats = ["articolo", "paper", "report", "podcast", "video"]
    if a["format"] not in valid_formats:
        return False, f"format non valido: {a['format']}"
    valid_importance = ["HIGH", "MED", "LOW"]
    if a.get("importance") and a["importance"] not in valid_importance:
        return False, f"importance non valido: {a['importance']}"
    return True, None


def save_article(supabase, a):
    """Upsert su external_id."""
    row = {
        "external_id": a.get("external_id"),
        "title": a.get("title"),
        "source": a.get("source"),
        "author": a.get("author"),
        "published_date": a.get("published_date"),
        "category": a.get("category"),
        "format": a.get("format"),
        "language": a.get("language") or "it",
        "summary": a.get("summary"),
        "why_relevant": a.get("why_relevant"),
        "reading_time": a.get("reading_time"),
        "image_url": a.get("image_url"),
        "source_url": a.get("source_url"),
        "tags": a.get("tags") or [],
        "color": a.get("color") or "#0ea5e9",
        "importance": a.get("importance") or "MED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Rimuovi None per non sovrascrivere campi esistenti
    row = {k: v for k, v in row.items() if v is not None}

    try:
        supabase.table("curated_articles_live") \
            .upsert(row, on_conflict="external_id") \
            .execute()
        return True
    except Exception as exc:
        print(f"   errore salvataggio {a.get('external_id')}: {exc}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print("1. Pulizia articoli obsoleti (pubblicati da > 60 giorni)...")
    cleanup_old_articles(supabase)
    print()

    print("2. Recupero articoli già esistenti...")
    existing = fetch_existing_articles(supabase)
    print(f"   {len(existing)} articoli nel database\n")
    existing_context = build_existing_context(existing)

    print("3. Generazione articoli via Claude Haiku + web_search...")
    articles = generate_articles(client, existing_context)

    if not articles or not isinstance(articles, list):
        print("Generazione fallita.")
        return

    print(f"   {len(articles)} articoli generati\n")

    print("4. Validazione e salvataggio...")
    saved = 0
    skipped = 0
    for a in articles:
        valid, err = validate_article(a)
        if not valid:
            print(f"   SKIP {a.get('external_id') or '?'}: {err}")
            skipped += 1
            continue
        if save_article(supabase, a):
            saved += 1

    print(f"\n   {saved} salvati, {skipped} skippati")
    print("\nFine.")


if __name__ == "__main__":
    main()
