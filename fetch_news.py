import os
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

MARKETAUX_KEY = os.environ.get("MARKETAUX_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL = "claude-haiku-4-5-20251001"
TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN", "SPY"]

# Quante news massime salvare per ogni esecuzione
MAX_NEWS_PER_RUN = 1

CATEGORIES = ["azioni", "macroeconomia", "geopolitica", "materie_prime", "generica"]


def fetch_news_for_tickers(tickers):
    # Ampliamo finestra a 24h per avere più candidate da cui scegliere la migliore
    published_after = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    all_news = []
    url = "https://api.marketaux.com/v1/news/all"

    params1 = {
        "industries": "Finance,Technology,Energy,Healthcare",
        "filter_entities": "true",
        "language": "en",
        "limit": 5,
        "published_after": published_after,
        "api_token": MARKETAUX_KEY,
    }
    try:
        r = requests.get(url, params=params1, timeout=15)
        data = r.json()
        if "data" in data:
            all_news.extend(data["data"])
            print(f"  Generic finance news: {len(data['data'])}")
        else:
            print(f"  Generic news error: {data.get('message', data)}")
    except Exception as e:
        print(f"  Errore generic news: {e}")

    params2 = {
        "symbols": ",".join(tickers),
        "filter_entities": "true",
        "language": "en",
        "limit": 5,
        "published_after": published_after,
        "api_token": MARKETAUX_KEY,
    }
    try:
        r = requests.get(url, params=params2, timeout=15)
        data = r.json()
        if "data" in data:
            all_news.extend(data["data"])
            print(f"  Ticker-specific news: {len(data['data'])}")
        else:
            print(f"  Ticker news error: {data.get('message', data)}")
    except Exception as e:
        print(f"  Errore ticker news: {e}")

    # Dedup per uuid
    seen = set()
    unique = []
    for n in all_news:
        uid = n.get("uuid")
        if uid and uid not in seen:
            seen.add(uid)
            unique.append(n)
    print(f"  Total unique news: {len(unique)}")
    return unique


def parse_news_item(item):
    entities = item.get("entities", [])
    tickers = [e.get("symbol") for e in entities if e.get("symbol")]
    sentiments = [e.get("sentiment_score") for e in entities if e.get("sentiment_score") is not None]
    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else None
    return {
        "external_id": item.get("uuid"),
        "title": item.get("title"),
        "summary": item.get("description") or item.get("snippet"),
        "source": item.get("source"),
        "url": item.get("url"),
        "published_at": item.get("published_at"),
        "tickers": ",".join(tickers) if tickers else None,
        "sentiment": avg_sentiment,
    }


def generate_full_analysis(client, title_en, summary_en, tickers_csv):
    tickers_str = tickers_csv if tickers_csv else "—"
    prompt = f"""Sei un analista finanziario senior italiano che scrive direttamente per UN consulente finanziario specifico (l'utente di Theta). Riceverai una notizia in inglese e devi produrre un'analisi completa in italiano professionale + classificare la categoria.

REGOLE GENERALI
- Italiano corretto, lessico finanziario professionale.
- Sii fattuale: usa SOLO informazioni presenti nel testo originale. Non inventare numeri, date, eventi.
- Tono neutro nei primi 3 blocchi (TITOLO, SOMMARIO, IMPATTO).
- Nella STRATEGIA parla direttamente al consulente al TU («valuta», «monitora», «considera», «alleggerisci»). Mai «i consulenti potrebbero».
- Forme condizionali per i consigli («se il segnale si conferma...», «per clienti con profilo X...»).

CATEGORIE — Scegline UNA tra:
- azioni: notizie su singole società quotate, earnings, M&A, IPO, partnership aziendali
- macroeconomia: dati economici (PIL, inflazione, occupazione), banche centrali, tassi
- geopolitica: guerre, conflitti, sanzioni, tensioni internazionali, elezioni, politica estera
- materie_prime: petrolio, gas, oro, metalli, agricoltura, energia
- generica: altro (regolamentazione finanziaria, tecnologia generica, varie)

INPUT
Titolo originale (EN): {title_en}
Sommario originale (EN): {summary_en or "—"}
Ticker citati: {tickers_str}

OUTPUT — rispondi ESATTAMENTE in questo formato, con i 5 blocchi etichettati, senza preamboli:

CATEGORIA: <una sola tra: azioni, macroeconomia, geopolitica, materie_prime, generica>

TITOLO: <titolo italiano, max 110 caratteri, riformulato non tradotto letterale>

SOMMARIO: <riassunto 4-6 frasi (600-900 caratteri): contesto, dati chiave, attori, significato per il mercato. Stile giornalistico breve.>

IMPATTO: <analisi 3-4 frasi (350-550 caratteri): settori/asset coinvolti, direzione, correlazioni.>

STRATEGIA: <suggerimento operativo 3-4 frasi (350-550 caratteri) al TU al consulente. Cita ticker se rilevanti.>"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        sections = {"CATEGORIA": None, "TITOLO": None, "SOMMARIO": None, "IMPATTO": None, "STRATEGIA": None}
        current_key = None
        current_lines = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                if current_key and current_lines:
                    sections[current_key] = " ".join(current_lines).strip()
                    current_lines = []
                continue
            matched_header = False
            for key in sections.keys():
                if line.startswith(f"{key}:"):
                    if current_key and current_lines:
                        sections[current_key] = " ".join(current_lines).strip()
                    current_key = key
                    rest = line[len(key) + 1:].strip()
                    current_lines = [rest] if rest else []
                    matched_header = True
                    break
            if not matched_header and current_key:
                current_lines.append(line)
        if current_key and current_lines:
            sections[current_key] = " ".join(current_lines).strip()

        cat = (sections["CATEGORIA"] or "generica").lower().strip()
        if cat not in CATEGORIES:
            cat = "generica"

        return cat, sections["TITOLO"], sections["SOMMARIO"], sections["IMPATTO"], sections["STRATEGIA"]
    except Exception as e:
        print(f"  errore analisi: {e}")
        return None, None, None, None, None


def upsert_news(supabase, news_items, anthropic_client, max_to_save):
    new_count = 0

    # Filtro: solo news non gia in db
    fresh_items = []
    for item in news_items:
        ext_id = item.get("external_id")
        if not ext_id:
            continue
        existing = supabase.table("news").select("id").eq("external_id", ext_id).execute()
        if not existing.data:
            fresh_items.append(item)

    print(f"  News nuove (non in db): {len(fresh_items)}")

    # Salva solo le prime N
    for item in fresh_items[:max_to_save]:
        cat, title_it, summary_it, impact_it, strategy_it = generate_full_analysis(
            anthropic_client, item.get("title"), item.get("summary"), item.get("tickers")
        )
        item["category"] = cat
        item["title_it"] = title_it
        item["summary_it"] = summary_it
        item["impact_it"] = impact_it
        item["strategy_it"] = strategy_it
        try:
            supabase.table("news").insert(item).execute()
            new_count += 1
            preview = (title_it or item.get("title") or "")[:60]
            print(f"  nuova [{cat}]: {preview}…")
        except Exception as e:
            print(f"  errore salvataggio: {e}")

    return new_count


def main():
    print(f"Scarico news per {len(TICKERS)} ticker (ultime 24 ore, max {MAX_NEWS_PER_RUN} da salvare)...")
    news_raw = fetch_news_for_tickers(TICKERS)
    print(f"Ricevute {len(news_raw)} news da MarketAux.\n")

    if not news_raw:
        return

    items = [parse_news_item(n) for n in news_raw]
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print(f"Analisi AI per max {MAX_NEWS_PER_RUN} news (Claude Haiku)...")
    new_count = upsert_news(supabase, items, anthropic_client, MAX_NEWS_PER_RUN)
    print(f"\nFatto: {new_count} news nuove salvate.")


if __name__ == "__main__":
    main()
