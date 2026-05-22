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
    """Scarica news generiche + ticker-specific da MarketAux."""
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
    """Converte una news MarketAux nel formato del database."""
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
    """
    Genera 4 sezioni in italiano via Claude:
    TITOLO, SOMMARIO (lungo), IMPATTO (analisi mercati), STRATEGIA (operativa).
    """
    tickers_str = tickers_csv if tickers_csv else "—"
    prompt = f"""Sei un analista finanziario senior italiano che scrive per consulenti professionisti. Riceverai una notizia in inglese e devi produrre un'analisi completa in italiano fluido e professionale, da pubblicare sull'app Theta.

REGOLE GENERALI
- Scrivi in italiano corretto, sintassi naturale, lessico finanziario professionale.
- Sii fattuale: usa SOLO informazion
