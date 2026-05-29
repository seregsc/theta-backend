"""
Generatore reading list curata per consulenti finanziari di Theta.
- Esecuzione 2 volte al giorno (07:00 e 15:00 italiane via GitHub Actions cron)
- Genera pochi articoli per volta (3-5) per un flusso continuo invece di un blocco settimanale
- Usa Claude Haiku con web_search per trovare articoli, paper, podcast rilevanti
- Pulizia: articoli pubblicati da piu di 60 giorni vengono cancellati
- Upsert su external_id per evitare duplicati

FIX IMMAGINE/LOGO: ogni articolo ha SEMPRE un'immagine valida.
Se Claude non fornisce image_url (o ne fornisce una non plausibile), lo script
ricava deterministicamente il logo del publisher dal dominio del source_url
(via Logo.dev). Cosi nell'app non compaiono mai card con sfondo "rotto".
"""
import os
import json
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5"

# Mappa fonte (substring, lowercase) -> dominio, per il logo del publisher.
# Usata come fallback quando non si ricava un dominio valido dal source_url.
SOURCE_LOGO_DOMAINS = {
    "blackrock": "blackrock.com",
    "vanguard": "vanguard.com",
    "jp morgan": "jpmorgan.com",
    "j.p. morgan": "jpmorgan.com",
    "jpmorgan": "jpmorgan.com",
    "pimco": "pimco.com",
    "amundi": "amundi.com",
    "pictet": "pictet.com",
    "goldman": "goldmansachs.com",
    "robeco": "robeco.com",
    "morningstar": "morningstar.com",
    "fundspeople": "fundspeople.com",
    "bluerating": "bluerating.com",
    "advisor online": "advisoronline.it",
    "advisoronline": "advisoronline.it",
    "focusrisparmio": "focusrisparmio.com",
    "wall street italia": "wallstreetitalia.com",
    "milano finanza": "milanofinanza.it",
    "diritto bancario": "dirittobancario.it",
    "citywire": "citywire.com",
    "mckinsey": "mckinsey.com",
    "boston consulting": "bcg.com",
    "bcg": "bcg.com",
    "deloitte": "deloitte.com",
    "pwc": "pwc.com",
    "banca d'italia": "bancaditalia.it",
    "bce": "ecb.europa.eu",
    "banca centrale europea": "ecb.europa.eu",
    "ecb": "ecb.europa.eu",
    "fmi": "imf.org",
    "imf": "imf.org",
    "bis": "bis.org",
    "ritholtz": "ritholtzwealth.com",
    "compound": "ritholtzwealth.com",
    "animal spirits": "ritholtzwealth.com",
    "schroders": "schroders.com",
    "fidelity": "fidelity.com",
    "invesco": "invesco.com",
    "state street": "ssga.com",
    "franklin": "franklintempleton.com",
    "templeton": "franklintempleton.com",
    "axa": "axa-im.com",
    "eurizon": "eurizoncapital.com",
    "anima": "animasgr.it",
    "generali": "generali.com",
    "intesa": "intesasanpaolo.com",
    "unicredit": "unicredit.it",
    "mediobanca": "mediobanca.com",
    "azimut": "azimut.it",
    "mediolanum": "bancamediolanum.it",
    "dws": "dws.com",
    "natixis": "im.natixis.com",
    "allianz": "allianzgi.com",
    "wellington": "wellington.com",
    "t. rowe": "troweprice.com",
    "nordea": "nordea.com",
    "bnp": "bnpparibas.com",
    "bloomberg": "bloomberg.com",
    "financial times": "ft.com",
    "reuters": "reuters.com",
    "wall street journal": "wsj.com",
    "economist": "economist.com",
    "il sole 24 ore": "ilsole24ore.com",
    "cfa institute": "cfainstitute.org",
}

# Domini "social/redirect" da NON usare come logo (non sono publisher reali).
SKIP_DOMAINS = {"t.co", "bit.ly", "lnkd.in", "youtube.com", "youtu.be",
                "twitter.com", "x.com", "linkedin.com", "facebook.com"}


LOGODEV_KEY = os.environ.get("LOGODEV_KEY") or "pk_eURA5T4JQ0i-VwQb55AphA"


def logo_from_domain(domain):
    # Clearbit dismesso (dic 2025): usiamo Logo.dev (stessa key del frontend).
    return f"https://img.logo.dev/{domain}?token={LOGODEV_KEY}&size=128"


def derive_image(a):
    """Restituisce un'immagine SEMPRE valida per l'articolo.
    Priorita: 1) image_url fornita da Claude se plausibile (foto reale),
              2) logo dal mapping della fonte,
              3) logo dal dominio del source_url."""
    # 1) immagine fornita, se sembra un'immagine reale (foto, non logo/clearbit morto)
    img = (a.get("image_url") or "").strip()
    if img.startswith("http") and "clearbit.com" not in img and "logo.dev" not in img:
        low = img.lower()
        if any(low.split("?")[0].endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")) \
           or "og:image" in low or "/image" in low or "/media" in low or "/wp-content" in low:
            return img

    # 2) logo dal nome fonte
    src = (a.get("source") or "").lower()
    for key, domain in SOURCE_LOGO_DOMAINS.items():
        if key in src:
            return logo_from_domain(domain)

    # 3) logo dal dominio del source_url
    su = (a.get("source_url") or "").strip()
    if su:
        try:
            host = urlparse(su).hostname or ""
            host = host.replace("www.", "")
            if host and host not in SKIP_DOMAINS:
                return logo_from_domain(host)
        except Exception:
            pass

    # 4) fallback finale: nessuna immagine (il frontend mostrera il fallback testuale)
    return None


def fetch_existing_articles(supabase, limit=80):
    """Recupera articoli gia esistenti (ultimi N) per dare contesto a Claude."""
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
    """Cancella articoli pubblicati da piu di 60 giorni."""
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
        return "(nessun articolo gia nel database - popola la lista da zero)"
    lines = []
    for a in existing[:40]:
        title = a.get("title") or ""
        src = a.get("source") or ""
        date = a.get("published_date") or ""
        ext = a.get("external_id") or ""
        lines.append(f"- [{ext}] {src} - {date} - {title}")
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
    today = datetime.now()
    today_str = today.strftime("%d/%m/%Y")
    today_iso = today.strftime("%Y-%m-%d")
    six_weeks_ago = (today - timedelta(days=42)).strftime("%Y-%m-%d")
    current_year = today.year

    prompt = f"""Sei un editor che cura una reading list settimanale per consulenti finanziari italiani professionisti.

DATA OGGI: {today_str} (ISO: {today_iso}). Siamo nell'anno {current_year}.

OBIETTIVO
Trova 3-5 articoli, paper, ricerche o podcast pubblicati nelle ultime 6 settimane (dal {six_weeks_ago} a {today_iso}) che un consulente finanziario italiano dovrebbe leggere/ascoltare per fare meglio il proprio lavoro. Sono pochi perche questa lista si aggiorna piu volte al giorno: punta sulla QUALITA e sulla VARIETA rispetto a quanto gia presente, non sulla quantita.

USA WEB SEARCH per trovare contenuti REALI e RECENTI. Fonti prioritarie:

ASSET MANAGER & RICERCA ISTITUZIONALE (en):
- BlackRock Investment Institute - blackrock.com/institute
- Vanguard Research - corporate.vanguard.com/research
- JP Morgan Asset Management - Market Insights
- Pimco Insights - pimco.com/en-us/insights
- Amundi Research Center - amundi.com/research
- Pictet Asset Management - Insights
- Goldman Sachs Asset Management - insights
- Robeco - Insights

ISTITUZIONALI ITALIA / EU (it/en):
- Banca d'Italia - Temi di discussione e Note di stabilita
- BCE - Working papers e Financial Stability Review
- FMI - IMF Working Papers
- Banca dei Regolamenti Internazionali (BIS) - Quarterly Review

MEDIA SPECIALIZZATI ITALIA (it):
- Morningstar Italia - morningstar.it
- FundsPeople Italia - fundspeople.com/it
- Bluerating - bluerating.com
- Advisor Online - advisoronline.it
- FocusRisparmio - focusrisparmio.com
- Wall Street Italia - wallstreetitalia.com
- Milano Finanza - milanofinanza.it
- Diritto Bancario - dirittobancario.it (per contenuti normativi/commentary)
- Citywire Italia

CONSULTING & STRATEGY (en):
- McKinsey & Co. - Financial Services Insights
- Boston Consulting Group - Wealth Management Reports
- Deloitte - Wealth & Asset Management Outlook
- PwC - Asset & Wealth Management

PODCAST E ALTRI FORMATI:
- Bluerating Podcast
- FocusRisparmio Podcast
- WSI Talk
- The Compound (Ritholtz Wealth)
- Animal Spirits

ARTICOLI GIA NEL DATABASE (NON duplicare):
{existing_context}

CATEGORIE (esattamente uno tra questi, lowercase):
- "strategia" -> outlook trimestrali, asset allocation, geopolitica
- "mercati" -> analisi azionario, obbligazionario, valute, commodities
- "asset_class" -> ETF, private markets, alternative, gold
- "wealth" -> wealth management, passaggio generazionale, family office
- "comportamento" -> finanza comportamentale, bias, neuroeconomia
- "pratica" -> relazione cliente, vendita, marketing, business
- "normativa" -> commentary normativo, opinioni esperti (NON aggiornamenti CONSOB ufficiali)
- "innovazione" -> AI, fintech, blockchain, robo-advisor

FORMAT (esattamente uno):
- "articolo" -> articolo giornalistico, blog post
- "paper" -> paper accademico, ricerca scientifica
- "report" -> report istituzionale, outlook annuale
- "podcast" -> episodio podcast (con durata)
- "video" -> intervista video, webinar registrato

IMPORTANCE - quanto e imperdibile:
- "HIGH" -> pubblicazione di rilievo, autori top, contenuto unico
- "MED" -> buona qualita, vale la pena
- "LOW" -> contesto utile ma non urgente

REGOLE
1. SOLO contenuti realmente esistenti e verificabili - usa web_search per confermare URL e date.
2. NO contenuti vecchi, scaduti o paywall completamente bloccati.
3. published_date: deve essere REALE, dell'anno {current_year}, mai nel futuro, mai inventata. Se non sei sicuro della data, scarta l'articolo.
4. Varia il mix rispetto a quanto e gia presente: alterna fonti italiane e internazionali, e tra articoli, ricerche e podcast. Evita di proporre piu volte la stessa fonte o lo stesso tema gia coperto di recente.
5. Bilancia anche le categorie: non piu di 3 articoli sulla stessa categoria.
6. IMMAGINE: includi image_url con un'immagine reale (og:image della pagina, copertina del paper, banner del podcast). Se non la trovi, lascia pure image_url vuoto: lo script applichera automaticamente il logo del publisher. NON inventare URL di immagini inesistenti.
7. source_url: DEVE essere l'URL diretto dell'articolo/documento specifico, non la home del sito.
8. reading_time: stima realistica in minuti (1500 parole = ~7 min).
9. summary: 2-3 frasi che spieghino DI COSA parla l'articolo.
10. why_relevant: 1-2 frasi che spieghino PERCHE il consulente dovrebbe leggerlo.
11. Mantieni external_id stabile e descrittivo (es. "blackrock-q2-{current_year}-geopolitical-dashboard").

OUTPUT - rispondi SOLO con array JSON, senza markdown, senza commenti:

[
  {{
    "external_id": "blackrock-q2-{current_year}-outlook",
    "title": "Titolo originale dell'articolo (anche in inglese se l'originale e inglese)",
    "title_it": "Titolo italiano d'impatto, comprensibile, max 12 parole - DEVE far capire al consulente di cosa parla l'articolo.",
    "source": "BlackRock Investment Institute",
    "author": "BlackRock Research Team",
    "published_date": "{current_year}-04-15",
    "category": "strategia",
    "format": "report",
    "language": "en",
    "summary": "Sintesi 2-3 frasi di cosa parla l'articolo.",
    "why_relevant": "1-2 frasi: perche il consulente deve leggerlo.",
    "reading_time": 12,
    "image_url": "https://...jpg",
    "source_url": "https://www.blackrock.com/institute/...",
    "tags": ["geopolitica", "asset_allocation", "outlook"],
    "color": "#0ea5e9",
    "importance": "HIGH"
  }},
  ...
]

REGOLA CRITICA SU title_it:
- E OBBLIGATORIO per OGNI articolo
- DEVE essere in italiano, anche se l'articolo e in inglese
- DEVE far capire IMMEDIATAMENTE di cosa parla - niente titoli vaghi
- Lunghezza ideale: 8-12 parole
- Esempi BUONI: "Tassi al 3,75%: la BCE pronta a una pausa lunga", "Oro sopra $3.000: cosa cambia per i portafogli prudenti"
- Esempi CATTIVI: "Q1 Outlook", "Market Review", "Investment Insights"

COLORI per category (usa esattamente questi):
- strategia -> "#0ea5e9" (blu)
- mercati -> "#10b981" (verde)
- asset_class -> "#06b6d4" (ciano)
- wealth -> "#8b5cf6" (viola scuro)
- comportamento -> "#ec4899" (rosa)
- pratica -> "#f59e0b" (ambra)
- normativa -> "#dc2626" (rosso)
- innovazione -> "#a855f7" (viola)

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
    required = ["external_id", "title", "title_it", "source", "published_date", "category", "format", "source_url"]
    for field in required:
        if not a.get(field):
            return False, f"campo mancante: {field}"
    if len(a.get("title_it", "")) < 5:
        return False, "title_it troppo corto"

    # published_date: formato + plausibilita (no futuro, no troppo vecchia)
    try:
        pub = datetime.strptime(a["published_date"], "%Y-%m-%d")
    except ValueError:
        return False, "formato published_date non valido"
    now = datetime.now()
    if pub.date() > (now + timedelta(days=1)).date():
        return False, f"published_date nel futuro: {a['published_date']}"
    if pub.date() < (now - timedelta(days=70)).date():
        return False, f"published_date troppo vecchia: {a['published_date']}"

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
    """Upsert su external_id. Garantisce sempre un'immagine valida."""
    # IMMAGINE SEMPRE VALIDA: logo del publisher se manca/non plausibile.
    image = derive_image(a)

    row = {
        "external_id": a.get("external_id"),
        "title": a.get("title"),
        "title_it": a.get("title_it"),
        "source": a.get("source"),
        "author": a.get("author"),
        "published_date": a.get("published_date"),
        "category": a.get("category"),
        "format": a.get("format"),
        "language": a.get("language") or "it",
        "summary": a.get("summary"),
        "why_relevant": a.get("why_relevant"),
        "reading_time": a.get("reading_time"),
        "image_url": image,
        "source_url": a.get("source_url"),
        "tags": a.get("tags") or [],
        "color": a.get("color") or "#0ea5e9",
        "importance": a.get("importance") or "MED",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Rimuovi None per non sovrascrivere campi esistenti (ma image_url puo restare None
    # solo se proprio non ricavabile; in quel caso il frontend usa il fallback testuale).
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

    print("2. Recupero articoli gia esistenti...")
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
