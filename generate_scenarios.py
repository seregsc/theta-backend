"""
Generatore di scenari di stress test giornalieri.
Legge le news recenti con analisi italiana e chiede a Claude di sintetizzare
2-3 scenari di stress test concreti, nello stesso formato degli scenari statici di Theta.
"""
import os
import json
import re
from datetime import datetime, timedelta, timezone
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"

NUM_SCENARIOS = 3


def fetch_recent_news_context(supabase, limit=20):
    """Prende le news più recenti con analisi italiana per fare context."""
    since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    result = supabase.table("news") \
        .select("title_it, summary_it, impact_it, tickers, sentiment, published_at") \
        .gte("published_at", since) \
        .not_.is_("title_it", "null") \
        .not_.is_("impact_it", "null") \
        .order("published_at", desc=True) \
        .limit(limit) \
        .execute()
    return result.data or []


def build_news_context(news_items):
    """Compatta le news in un blocco testuale per il prompt."""
    lines = []
    for n in news_items:
        title = n.get("title_it") or "—"
        impact = n.get("impact_it") or ""
        tickers = n.get("tickers") or ""
        lines.append(f"- {title} [ticker: {tickers}] — impatto: {impact[:200]}")
    return "\n".join(lines)


def parse_scenarios_response(text):
    """
    Estrae il blocco JSON dalla risposta di Claude.
    Claude può rispondere con ```json ... ``` o con JSON puro.
    """
    # Cerca blocco markdown ```json ... ```
    match = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if match:
        json_text = match.group(1).strip()
    else:
        # Cerca un array JSON che inizia con [
        match = re.search(r"\[\s*\{.+\}\s*\]", text, re.DOTALL)
        if match:
            json_text = match.group(0)
        else:
            json_text = text.strip()

    try:
        data = json.loads(json_text)
        if isinstance(data, list):
            return data
        return None
    except json.JSONDecodeError as e:
        print(f"  ✗ Errore parsing JSON: {e}")
        print(f"  Risposta ricevuta (primi 500 char): {text[:500]}")
        return None


def generate_scenarios(client, news_context):
    """Chiede a Claude di generare N scenari di stress test."""
    today = datetime.now().strftime("%d %B %Y")

    prompt = f"""Sei un analista finanziario senior italiano. Devi generare {NUM_SCENARIOS} scenari di stress test concreti per la giornata di oggi ({today}), basandoti sul contesto delle news degli ultimi giorni.

CONTESTO — News recenti dal mercato (titoli, ticker citati, analisi di impatto):
{news_context}

ISTRUZIONI
- Genera ESATTAMENTE {NUM_SCENARIOS} scenari diversi tra loro (no duplicati tematici).
- Ogni scenario deve essere PLAUSIBILE e collegato ai temi emersi dalle news sopra.
- Tematiche valide: politica monetaria, tensioni geopolitiche, settori specifici (AI/tech, energia, difesa), eventi di mercato, choc su singoli asset.
- NON inventare ticker: usa solo quelli che esistono sui mercati reali (es. AAPL, NVDA, MSFT, TSLA, GOOGL, META, AMZN, SPY, XOM, LMT, RHM.DE, ENI.MI, ecc.).
- Italiano professionale, lessico finanziario.
- Probabilità: usa "Bassa (10-25%)", "Media (30-50%)", "Alta (55-75%)", "Molto alta (80%+)".
- Severity: "Lieve", "Media", "Severa", "Estrema".
- Time horizon: "1-3 mesi", "3-6 mesi", "6-12 mesi", "12-24 mesi".

OUTPUT — Rispondi ESATTAMENTE in formato JSON, un array di {NUM_SCENARIOS} oggetti, senza preamboli, senza markdown:

[
  {{
    "title": "Titolo breve dello scenario (max 80 caratteri)",
    "icon": "🌐",
    "probability": "Media (40%)",
    "severity": "Severa",
    "time_horizon": "3-6 mesi",
    "description": "Descrizione narrativa dello scenario in 3-5 frasi (400-700 caratteri). Contesto, cosa scatena lo scenario, conseguenze macro principali.",
    "trigger_events": ["Evento 1", "Evento 2", "Evento 3", "Evento 4"],
    "market_impact": {{
      "equity": "S&P 500 -X/-Y%, EuroStoxx -X/-Y%",
      "bonds": "Treasury 10Y +X/+Y bp",
      "dollar": "Direzione e intensità",
      "gold": "Direzione e percentuale",
      "oil": "Range di prezzo previsto"
    }},
    "winners": [
      {{"ticker": "TICKER", "name": "Nome Azienda", "why": "Motivazione in 1 frase", "expected_move": "+X-Y%"}},
      {{"ticker": "TICKER", "name": "Nome Azienda", "why": "Motivazione", "expected_move": "+X-Y%"}},
      {{"ticker": "TICKER", "name": "Nome Azienda", "why": "Motivazione", "expected_move": "+X-Y%"}}
    ],
    "losers": [
      {{"ticker": "TICKER", "name": "Nome Azienda", "why": "Motivazione", "expected_move": "-X/-Y%"}},
      {{"ticker": "TICKER", "name": "Nome Azienda", "why": "Motivazione", "expected_move": "-X/-Y%"}},
      {{"ticker": "TICKER", "name": "Nome Azienda", "why": "Motivazione", "expected_move": "-X/-Y%"}}
    ],
    "hedge_strategy": "Strategia operativa di copertura in 4-5 frasi (500-700 caratteri). Quali asset acquistare/vendere, quali percentuali di portafoglio, considerazioni per i diversi profili di clienti.",
    "early_warning": ["Segnale 1", "Segnale 2", "Segnale 3", "Segnale 4"]
  }}
]

Icone consigliate per tipo di scenario: 🇨🇳 Cina/Asia, 🇺🇸 USA, 🇪🇺 Europa, 🛢️ Energia, ⚔️ Geopolitica/conflitti, 💰 Politica monetaria, 🤖 Tech/AI, 🏛️ Politica/elezioni, 🌐 Globale/macro, 📉 Crisi/recessione."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        scenarios = parse_scenarios_response(text)
        return scenarios
    except Exception as e:
        print(f"  ✗ errore Claude: {e}")
        return None


def cleanup_old_scenarios(supabase, keep_recent_hours=24):
    """Elimina scenari live più vecchi di N ore (così la tabella non cresce indefinitamente)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_recent_hours)).isoformat()
    try:
        result = supabase.table("scenarios_live") \
            .delete() \
            .lt("generated_at", cutoff) \
            .execute()
        deleted = len(result.data) if result.data else 0
        if deleted > 0:
            print(f"  ↺ eliminati {deleted} scenari vecchi (oltre {keep_recent_hours}h)")
    except Exception as e:
        print(f"  ⚠ errore cleanup: {e}")


def save_scenarios(supabase, scenarios, news_count):
    """Salva gli scenari nel database."""
    saved = 0
    for s in scenarios:
        row = {
            "title": s.get("title"),
            "icon": s.get("icon"),
            "probability": s.get("probability"),
            "severity": s.get("severity"),
            "time_horizon": s.get("time_horizon"),
            "description": s.get("description"),
            "trigger_events": s.get("trigger_events"),
            "market_impact": s.get("market_impact"),
            "winners": s.get("winners"),
            "losers": s.get("losers"),
            "hedge_strategy": s.get("hedge_strategy"),
            "early_warning": s.get("early_warning"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "news_count": news_count,
        }
        try:
            supabase.table("scenarios_live").insert(row).execute()
            saved += 1
            print(f"  ✓ salvato: {s.get('title', '?')[:60]}")
        except Exception as e:
            print(f"  ✗ errore salvataggio: {e}")
    return saved


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    print("1. Cleanup scenari vecchi...")
    cleanup_old_scenarios(supabase, keep_recent_hours=24)

    print("2. Raccolgo contesto dalle news recenti...")
    news = fetch_recent_news_context(supabase, limit=20)
    print(f"   Trovate {len(news)} news con analisi negli ultimi 3 giorni")

    if len(news) < 3:
        print("Troppo poche news per generare scenari significativi. Esco.")
        return

    news_context = build_news_context(news)

    print(f"\n3. Genero {NUM_SCENARIOS} scenari via Claude...")
    scenarios = generate_scenarios(client, news_context)

    if not scenarios:
        print("Generazione fallita. Esco.")
        return

    print(f"   Ricevuti {len(scenarios)} scenari da Claude\n")

    print("4. Salvataggio nel database...")
    saved = save_scenarios(supabase, scenarios, news_count=len(news))

    print(f"\n✓ Fatto: {saved}/{len(scenarios)} scenari salvati con successo.")


if __name__ == "__main__":
    main()
