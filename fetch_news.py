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
    
    # Dedup
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
    """
    prompt = f"""Sei un analista finanziario senior italiano che scrive per consulenti professionisti. Riceverai una notizia in inglese e devi produrre un'analisi completa in italiano fluido e professionale, da pubblicare sull'app Theta.

REGOLE GENERALI
- Scrivi in italiano corretto, sintassi naturale, lessico finanziario professionale.
- Sii fattuale: usa SOLO informazioni presenti nel testo originale. Non inventare numeri, date, eventi, o citazioni.
- Tono: neutro, informativo, mai sensazionalistico.
- Non dare consigli espliciti di acquisto/vendita.

INPUT
Titolo originale (EN): {title_en}
Sommario originale (EN): {summary_en or "—"}
Ticker citati: {tickers_str}

OUTPUT — rispondi ESATTAMENTE in questo formato, con i 4 blocchi etichettati TITOLO, SOMMARIO, IMPATTO, STRATEGIA. Niente preamboli, niente conclusioni:

TITOLO: <titolo in italiano, max 110 caratteri. Riformulato, non tradotto letterale. Informativo e neutro.>

SOMMARIO: <riassunto sostanzioso in italiano, 4-6 frasi (600-900 caratteri). Includi: il contesto della notizia, i numeri/dati chiave presenti nell'originale, gli attori coinvolti, il significato per il mercato. Scrivi come un articolo giornalistico breve, non come bullet point. Se l'originale è povero di dettagli, espandi con il contesto settoriale plausibile (es. "il comparto X è sotto pressione per…") ma SENZA inventare fatti specifici.>

IMPATTO: <analisi di 3-4 frasi (350-550 caratteri) sull'impatto previsto. Indica: quali settori/asset sono direttamente coinvolti, in che direzione potrebbero muoversi, quali correlazioni di mercato sono rilevanti (es. tassi, dollaro, oro, oil). Concreto e ragionato.>

STRATEGIA: <suggerimento operativo di 3-4 frasi (350-550 caratteri) per un consulente che gestisce portafogli. Cosa potrebbe fare: monitorare un asset specifico, considerare riallocazione settoriale, valutare hedging, attendere conferme tecniche. Mai categorico ("compra X"), sempre condizionale ("se il segnale si conferma…", "per clienti con profilo Y…"). Cita ticker concreti se rilevanti.>"""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        
        # Parsing: cerca i 4 blocchi
        sections = {"TITOLO": None, "SOMMARIO": None, "IMPATTO": None, "STRATEGIA": None}
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
        # Chiudi l'ultima sezione
        if current_key and current_lines:
            sections[current_key] = " ".join(current_lines).strip()
        
        return sections["TITOLO"], sections["SOMMARIO"], sections["IMPATTO"], sections["STRATEGIA"]
    except Exception as e:
        print(f"  ✗ errore analisi: {e}")
        return None, None, None, None


def upsert_news(supabase, news_items, client):
    new_count, updated_count = 0, 0
    
    for item in news_items:
        ext_id = item.get("external_id")
        if not ext_id:
            continue
        
        existing = supabase.table("news").select("id, title_it, impact_it").eq("external_id", ext_id).execute()
        
        if existing.data:
            existing_row = existing.data[0]
            # Se già ha la traduzione completa (con impact), salta
            if existing_row.get("title_it") and existing_row.get("impact_it"):
                print(f"  · già analizzata: {(item.get('title') or '')[:60]}…")
                continue
            
            # Altrimenti rigenera analisi completa
            title_it, summary_it, impact_it, strategy_it = generate_full_analysis(
                client, item.get("title"), item.get("summary"), item.get("tickers")
            )
            if title_it:
                supabase.table("news").update({
                    "title_it": title_it,
                    "summary_it": summary_it,
                    "impact_it": impact_it,
                    "strategy_it": strategy_it,
                }).eq("id", existing_row["id"]).execute()
                updated_count += 1
                print(f"  ↻ aggiornata: {title_it[:60]}…")
        else:
            title_it, summary_it, impact_it, strategy_it = generate_full_analysis(
                client, item.get("title"), item.get("summary"), item.get("tickers")
            )
            item["title_it"] = title_it
            item["summary_it"] = summary_it
            item["impact_it"] = impact_it
            item["strategy_it"] = strategy_it
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
    
    print("Analisi completa AI e salvataggio...")
    new_count, updated_count = upsert_news(supabase, items, client)
    print(f"\n✓ {new_count} news nuove, {updated_count} aggiornate con analisi italiana.")


if __name__ == "__main__":
    main()
