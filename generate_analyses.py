import os
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Modello da usare. Sonnet è il miglior rapporto qualità/prezzo.
MODEL = "claude-sonnet-4-5"


def get_latest_prices():
    """Legge da Supabase l'ultimo prezzo per ogni ticker distinto."""
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    # Prendiamo tutti i prezzi degli ultimi 90 minuti, poi tieniamo il più recente per ticker
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


def generate_commentary(client, price_row):
    """Chiede a Claude un commento breve sul titolo."""
    ticker = price_row["ticker"]
    price = price_row.get("price")
    change = price_row.get("change_percent") or 0
    currency = price_row.get("currency") or "USD"
    
    prompt = f"""Sei un analista finanziario professionale. Scrivi un commento breve (massimo 120 parole) in italiano sul seguente titolo.

Titolo: {ticker}
Prezzo: {price} {currency}
Variazione oggi: {change:+.2f}%

Includi:
- Un'osservazione sul movimento di prezzo (se rilevante)
- Un contesto di mercato o settoriale (1-2 frasi)
- Un giudizio neutro: situazione attuale, non raccomandazione di acquisto/vendita

Tono: professionale, sintetico, italiano fluido. Non usare emoji né elenchi puntati. Non dare consigli di investimento personalizzati."""
    
    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
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
    print("Lettura ultimi prezzi dal database...")
    prices = get_latest_prices()
    print(f"Trovati {len(prices)} ticker con dati recenti.\n")
    
    if not prices:
        print("Nessun prezzo nel database. Esco.")
        return
    
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    print(f"Generazione analisi con {MODEL}...\n")
    
    for i, row in enumerate(prices, 1):
        ticker = row["ticker"]
        try:
            commentary = generate_commentary(client, row)
            save_analysis(
                supabase, ticker, row.get("price"),
                row.get("change_percent"), commentary,
            )
            preview = commentary[:80].replace("\n", " ")
            print(f"  ✓ [{i}/{len(prices)}] {ticker}: {preview}…")
        except Exception as e:
            print(f"  ✗ [{i}/{len(prices)}] {ticker}: errore {e}")
    
    print("\n✓ Fatto!")


if __name__ == "__main__":
    main()
