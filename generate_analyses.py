import os
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL = "claude-haiku-4-5-20251001"


def get_latest_prices(supabase):
    """Ultimo prezzo per ticker distinto (max 200 righe lookback)."""
    result = supabase.table("prices") \
        .select("*") \
        .order("created_at", desc=True) \
        .limit(200) \
        .execute()
    rows = result.data
    seen = set()
    latest = []
    for row in rows:
        ticker = row["ticker"]
        if ticker in seen:
            continue
        seen.add(ticker)
        latest.append(row)
    return latest


def get_recent_news_for_ticker(supabase, ticker, hours=48):
    """News degli ultimi N ore che citano il ticker (in italiano se disponibile)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    result = supabase.table("news") \
        .select("title_it, summary_it, title, summary, published_at, sentiment") \
        .like("tickers", f"%{ticker}%") \
        .gte("published_at", since) \
        .order("published_at", desc=True) \
        .limit(5) \
        .execute()
    return result.data


def format_news_for_prompt(news_items):
    """Trasforma le news in un blocco testuale leggibile da Claude."""
    if not news_items:
        return "Nessuna notizia rilevante nelle ultime 48 ore."
    lines = []
    for i, n in enumerate(news_items, 1):
        title = n.get("title_it") or n.get("title") or "—"
        summary = n.get("summary_it") or n.get("summary") or ""
        sentiment = n.get("sentiment")
        sent_str = ""
        if sentiment is not None:
            if sentiment > 0.15:
                sent_str = " [sentiment: positivo]"
            elif sentiment < -0.15:
                sent_str = " [sentiment: negativo]"
            else:
                sent_str = " [sentiment: neutro]"
        lines.append(f"{i}. {title}{sent_str}\n   {summary}")
    return "\n".join(lines)


def generate_commentary(client, price_row, news_items):
    ticker = price_row["ticker"]
    price = price_row.get("price")
    change = price_row.get("change_percent") or 0
    currency = price_row.get("currency") or "USD"
    news_block = format_news_for_prompt(news_items)
    
    prompt = f"""Sei un analista finanziario professionale italiano. Scrivi un commento breve (massimo 130 parole) in italiano sul seguente titolo.

DATI DI MERCATO
Ticker: {ticker}
Prezzo: {price} {currency}
Variazione oggi: {change:+.2f}%

NOTIZIE RECENTI (ultime 48 ore)
{news_block}

ISTRUZIONI
- Collega esplicitamente il movimento di prezzo alle notizie quando c'è una connessione plausibile
- Se non ci sono news rilevanti, non inventare: di' che il movimento non ha catalyst evidenti dalle news
- Sii fattuale e ancorato ai dati forniti, NON inventare numeri o eventi non presenti nelle news
- Tono professionale, sintetico, italiano fluido
- Non usare emoji né elenchi puntati
- Non dare consigli di acquisto/vendita: descrivi la situazione, non raccomandare azioni"""
    
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def save_analysis(supabase, ticker, price, change, commentary):
    return supabase.table("analyses").insert({
        "ticker": ticker,
        "price_at_analysis": price,
        "change_percent": change,
        "commentary": commentary,
        "model_used": MODEL,
    }).execute()


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    
    print("Lettura ultimi prezzi dal database...")
    prices = get_latest_prices(supabase)
    print(f"Trovati {len(prices)} ticker.\n")
    
    if not prices:
        print("Nessun prezzo nel database.")
        return
    
    print(f"Generazione analisi con {MODEL} + news context...\n")
    
    for i, row in enumerate(prices, 1):
        ticker = row["ticker"]
        try:
            news = get_recent_news_for_ticker(supabase, ticker)
            commentary = generate_commentary(client, row, news)
            save_analysis(
                supabase, ticker, row.get("price"),
                row.get("change_percent"), commentary,
            )
            preview = commentary[:80].replace("\n", " ")
            news_count = len(news)
            print(f"  ✓ [{i}/{len(prices)}] {ticker} ({news_count} news): {preview}…")
        except Exception as e:
            print(f"  ✗ [{i}/{len(prices)}] {ticker}: errore {e}")
    
    print("\n✓ Fatto!")


if __name__ == "__main__":
    main()
