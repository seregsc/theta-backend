import os
import requests
from supabase import create_client

MARKETAUX_KEY = os.environ.get("MARKETAUX_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Stessi ticker del fetch prezzi
TICKERS = ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN", "SPY"]


def fetch_news_for_tickers(tickers):
    """Scarica le news più recenti per la lista di ticker."""
    url = "https://api.marketaux.com/v1/news/all"
    params = {
        "symbols": ",".join(tickers),
        "filter_entities": "true",
        "language": "en",
        "limit": 3,
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
    """Estrae i campi che ci interessano da una news MarketAux."""
    # Estrai ticker citati nella news
    entities = item.get("entities", [])
    tickers = [e.get("symbol") for e in entities if e.get("symbol")]
    
    # Sentiment medio (MarketAux dà uno score per entità)
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


def save_news(supabase, news_items):
    """Salva le news evitando duplicati grazie a external_id UNIQUE."""
    saved = 0
    for item in news_items:
        if not item.get("external_id"):
            continue
        try:
            supabase.table("news").insert(item).execute()
            saved += 1
        except Exception as e:
            # Se è un duplicato (external_id già esistente), ignora
            if "duplicate" in str(e).lower() or "23505" in str(e):
                pass
            else:
                print(f"  ✗ errore salvataggio: {e}")
    return saved


def main():
    print(f"Scarico news per {len(TICKERS)} ticker...")
    news_raw = fetch_news_for_tickers(TICKERS)
    print(f"Ricevute {len(news_raw)} news da MarketAux.\n")
    
    if not news_raw:
        return
    
    items = [parse_news_item(n) for n in news_raw]
    
    # Stampa anteprima
    for i, item in enumerate(items, 1):
        title = (item["title"] or "")[:80]
        sent = item["sentiment"]
        sent_str = f" sent={sent:+.2f}" if sent is not None else ""
        tickers_str = item["tickers"] or "—"
        print(f"  [{i}] {tickers_str}{sent_str}: {title}…")
    
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    saved = save_news(supabase, items)
    print(f"\n✓ Salvate {saved} news nuove ({len(items) - saved} duplicate ignorate).")


if __name__ == "__main__":
    main()
