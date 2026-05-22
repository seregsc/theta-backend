
import os
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

MARKETAUX_KEY = os.environ.get("MARKETAUX_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL = "claude-sonnet-4-5"
TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN", "SPY"]


def fetch_news_for_tickers(tickers):
    """
    Scarica le news più recenti.
    
    Mix di due chiamate:
    - news generiche di mercato/economia/geopolitica (per settori finanziari)
    - news specifiche sui ticker selezionati
    
    Così abbiamo varietà nel database.
    """
    published_after = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S")
    all_news = []
    url = "https://api.marketaux.com/v1/news/all"
    
    # Chiamata 1: news generiche finanza/tech/energia/salute
    params1 = {
        "industries": "Finance,Technology,Energy,Healthcare",
        "filter_entities": "true",
        "language": "en",
        "limit": 3,
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
    
    # Chiamata 2: news sui ticker specifici
    params2 = {
        "symbols": ",".join(tickers),
        "filter_entities": "true",
        "language": "en",
        "limit": 3,
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
    
    # Deduplicazione su uuid
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


def translate_to_italian(client, title_en, summary_en):
    prompt = f"""Traduci la seguente notizia finanziaria in italiano professionale, conciso e adatto a un consulente finanziario.

Titolo originale (EN): {title_en}
Sommario originale (EN): {summary_en or "—"}

Rispondi ESATTAMENTE in questo formato, senza preamboli, senza aggiungere altro:

TITOLO: <titolo italiano, max 100 caratteri, neutro e informativo>
SOMMARIO: <sommario italiano in 1-2 frasi, max 250 caratteri, sintetico>"""
    
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        title_it, summary_it = None, None
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("TITOLO:"):
                title_it = line.replace("TITOLO:", "").strip()
            elif line.startswith("SOMMARIO:"):
                summary_it = line.replace("SOMMARIO:", "").strip()
        return title_it, summary_it
    except Exception as e:
        print(f"  ✗ errore traduzione: {e}")
        return None, None


def upsert_news(supabase, news_items, client):
    """Salva le news nuove e aggiorna quelle esistenti senza traduzione italiana."""
    new_count, updated_count = 0, 0
    
    for item in news_items:
        ext_id = item.get("external_id")
        if not ext_id:
            continue
        
        existing = supabase.table("news").select("id, title_it").eq("external_id", ext_id).execute()
        
        if existing.data:
            existing_row = existing.data[0]
            if existing_row.get("title_it"):
                print(f"  · già tradotta: {(item.get('title') or '')[:60]}…")
                continue
            
            title_it, summary_it = translate_to_italian(client, item.get("title"), item.get("summary"))
            if title_it:
                supabase.table("news").update({
                    "title_it": title_it,
                    "summary_it": summary_it,
                }).eq("id", existing_row["id"]).execute()
                updated_count += 1
                print(f"  ↻ aggiornata: {title_it[:60]}…")
        else:
            title_it, summary_it = translate_to_italian(client, item.get("title"), item.get("summary"))
            item["title_it"] = title_it
            item["summary_it"] = summary_it
            try:
                supabase.table("news").insert(item).execute()
                new_count += 1
                preview = (title_it or item.get("title") or "")[:60]
                print(f"  ✓ nuova: {preview}…")
            except Exception as e:
                print(f"  ✗ errore salvataggio: {e}")
    
    return new_count, updated_count


def main():
    print(f"Scarico news per {len(TICKERS)} ticker (ultime 12 ore)...")
    news_raw = fetch_news_for_tickers(TICKERS)
    print(f"Ricevute {len(news_raw)} news da MarketAux.\n")
    
    if not news_raw:
        return
    
    items = [parse_news_item(n) for n in news_raw]
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    
    print("Traduzione e salvataggio...")
    new_count, updated_count = upsert_news(supabase, items, client)
    print(f"\n✓ {new_count} news nuove, {updated_count} aggiornate con traduzione italiana.")


if __name__ == "__main__":
    main()
