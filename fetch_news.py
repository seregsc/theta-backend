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
    """Scarica le news più recenti (ultime 12 ore)."""
    published_after = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S")
    url = "https://api.marketaux.com/v1/news/all"
    params = {
        "symbols": ",".join(tickers),
        "filter_entities": "true",
        "language": "en",
        "limit": 3,
        "published_after": published_after,
        "api_token": MARKETAUX_KEY,
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
    except Exception as e:
        print(f"Errore di rete: {e}")
        return []
    if "data" not in data:
        print(f"Risposta API inattesa: {data}")
        return []
    return data["data"]


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
        
        # Controlla se esiste già nel database
        existing = supabase.table("news").select("id, title_it").eq("external_id", ext_id).execute()
        
        if existing.data:
            # Esiste già: traduciamo solo se title_it è vuoto
            existing_row = existing.data[0]
            if existing_row.get("title_it"):
                print(f"  · già tradotta: {(item.get('title') or '')[:60]}…")
                continue
            
            # Traduci e aggiorna
            title_it, summary_it = translate_to_italian(client, item.get("title"), item.get("summary"))
            if title_it:
                supabase.table("news").update({
                    "title_it": title_it,
                    "summary_it": summary_it,
                }).eq("id", existing_row["id"]).execute()
                updated_count += 1
                print(f"  ↻ aggiornata: {title_it[:60]}…")
        else:
            # Nuova: traduci e inserisci
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
