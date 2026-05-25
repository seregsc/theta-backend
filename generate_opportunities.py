"""
generate_opportunities.py
Genera occasioni di mercato reali, basate su soglie rigorose.

Filosofia: se in un giorno non ci sono vere occasioni, NON inserisce nulla.
Niente "riempitivi" o opportunità a basso valore.
"""

import os
import json
from datetime import datetime, timezone, timedelta
from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def normalize_sentiment(raw):
    if isinstance(raw, (int, float)):
        if raw > 0.3:
            return "positive"
        elif raw < -0.3:
            return "negative"
        else:
            return "neutral"
    if isinstance(raw, str) and raw.strip():
        return raw
    return "neutral"


def normalize_tickers(raw):
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except Exception:
            pass
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def fetch_prices_with_history(supabase):
    """Carica ultimi prezzi + history per ogni ticker."""
    res = supabase.table("prices").select("*").execute()
    prices = {p["ticker"]: p for p in (res.data or [])}

    res = supabase.table("prices_history").select("*").order("date", desc=True).limit(20000).execute()
    history = {}
    for row in (res.data or []):
        t = row["ticker"]
        history.setdefault(t, []).append(row)

    today = datetime.now(timezone.utc).date()
    results = {}
    for ticker, p in prices.items():
        current = float(p.get("price") or 0)
        if current <= 0:
            continue
        hist = sorted(history.get(ticker, []), key=lambda r: r["date"], reverse=True)
        change_1d = p.get("change_percent")
        change_7d = None
        change_30d = None
        for h in hist:
            h_date_raw = h["date"]
            if isinstance(h_date_raw, str):
                h_date = datetime.fromisoformat(h_date_raw).date()
            else:
                h_date = h_date_raw
            days = (today - h_date).days
            old_price = float(h.get("price") or 0)
            if old_price <= 0:
                continue
            pct = ((current - old_price) / old_price) * 100
            if change_7d is None and days >= 5:
                change_7d = pct
            if change_30d is None and days >= 25:
                change_30d = pct
            if change_7d is not None and change_30d is not None:
                break
        results[ticker] = {
            "ticker": ticker,
            "price": current,
            "currency": p.get("currency") or "USD",
            "change_1d": change_1d,
            "change_7d": change_7d,
            "change_30d": change_30d,
        }
    return results


def fetch_recent_news(supabase, hours=72, limit=30):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        res = supabase.table("news").select("*").gte("published_at", since).order("published_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        print(f"[news fetch error] {e}")
        return []


# ============================================================
# CATEGORIA 1: CROLLI RECENTI (soglie SEVERE)
# ============================================================
# Un asset è "in crollo" SOLO se rispetta almeno UNA di queste condizioni:
#   - Variazione 1d <= -10% (giornata molto pesante)
#   - Variazione 7d <= -15% (drawdown settimanale forte)
#   - Variazione 30d <= -25% (deep correction mensile)
# Se nessuna soglia è soddisfatta, NON è un'occasione.

def compute_crolli(prices_data, top_n=10):
    candidates = []
    for ticker, p in prices_data.items():
        ch_1d = p.get("change_1d") or 0
        ch_7d = p.get("change_7d")
        ch_30d = p.get("change_30d")

        # Soglie severe (almeno una deve essere violata)
        crash_1d = ch_1d <= -10
        crash_7d = ch_7d is not None and ch_7d <= -15
        crash_30d = ch_30d is not None and ch_30d <= -25

        if not (crash_1d or crash_7d or crash_30d):
            continue

        # Calcola "peggior drawdown" per scoring
        values = [v for v in [ch_1d, ch_7d, ch_30d] if v is not None]
        worst = min(values) if values else 0
        # Score più alto se il crash è più grave
        score = min(100, 50 + int(abs(worst) * 2))

        # Titolo descrittivo basato su quale soglia è stata violata
        if crash_1d:
            title = f"Crollo del giorno: {ch_1d:+.1f}%"
            timeframe = "ultimo giorno"
        elif crash_7d:
            title = f"Drawdown settimanale: {ch_7d:+.1f}%"
            timeframe = "7 giorni"
        else:
            title = f"Deep correction: {ch_30d:+.1f}% in 30 giorni"
            timeframe = "30 giorni"

        reason = (
            f"Calo significativo nelle ultime sessioni ({timeframe}). "
            "Possibile occasione di ingresso a valutazioni scontate, se la tesi fondamentale è intatta. "
            "Verifica i driver del crollo prima di accumulare."
        )

        candidates.append({
            "category": "crolli",
            "ticker": ticker,
            "current_price": p["price"],
            "currency": p["currency"],
            "change_pct_1d": ch_1d,
            "change_pct_7d": ch_7d,
            "change_pct_30d": ch_30d,
            "score": score,
            "title": title,
            "reason": reason,
            "expected_move": "Recovery 10-25%" if abs(worst) > 20 else "Rimbalzo 5-15%",
            "time_horizon": "short" if crash_1d else "medium",
            "risk": "HIGH" if abs(worst) >= 25 else "MED",
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


# ============================================================
# CATEGORIA 2: BENEFICIARI EVENTI (AI Claude, qualità alta)
# ============================================================

def compute_beneficiari(news_list, prices_data, top_n=6):
    """Usa Claude Haiku per identificare asset con CONVICTION ALTA da news recenti.
    Se nessuna news realmente impattante, restituisce []."""
    if not news_list or not ANTHROPIC_API_KEY:
        return []

    news_summary = []
    for n in news_list[:15]:
        title = n.get("title_it") or n.get("title") or ""
        summary = (n.get("summary_it") or n.get("summary") or "")[:250]
        tickers = normalize_tickers(n.get("tickers"))
        sentiment = normalize_sentiment(n.get("sentiment"))
        tickers_str = ", ".join(tickers) if tickers else "nessuno"
        news_summary.append(f"- [{sentiment.upper()}] {title}\n  {summary}\n  Ticker citati: {tickers_str}")
    news_text = "\n\n".join(news_summary)

    available_tickers = sorted([t for t in prices_data.keys() if prices_data[t].get("price")])

    prompt = f"""Sei un analista finanziario senior. Devi selezionare SOLO opportunità con CONVICTION ALTA.

NEWS RECENTI (ultime 72h):
{news_text}

ASSET DISPONIBILI: {', '.join(available_tickers[:200])}

REGOLE STRINGENTI:
1. Suggerisci asset SOLO se hai CONVICTION ALTA (score >= 70) che possa beneficiare di un evento concreto e specifico nelle news.
2. NON suggerire asset "generici" o "potrebbero andare bene". Solo se c'è un driver chiaro.
3. Se le news non contengono eventi davvero impattanti, RISPONDI con un array vuoto [].
4. Massimo 6 asset, ma è meglio 0 che riempire con suggerimenti deboli.
5. NO ipotesi, SOLO impatti diretti e tangibili documentati dalle news.

Per ogni asset selezionato, fornisci JSON con:
- ticker (deve essere nella lista disponibili)
- title: max 70 caratteri, descrittivo dell'opportunità SPECIFICA
- reason: max 300 caratteri, spiega il LINK CONCRETO tra news e impatto sull'asset
- news_driver: la news specifica che genera l'opportunità (max 100 caratteri)
- expected_move: range stimato "+X-Y%" (es. "+8-15%")
- time_horizon: "short" | "medium" | "long"
- risk: "LOW" | "MED" | "HIGH"
- score: 70-95 (solo conviction alta)

Rispondi SOLO con JSON array, niente altro testo. Se nessuna opportunità di qualità, []."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
        if not isinstance(parsed, list) or len(parsed) == 0:
            return []
        results = []
        for item in parsed[:top_n]:
            ticker = item.get("ticker")
            score = int(item.get("score") or 0)
            # Filtro: scarta sotto 70 (qualità minima)
            if score < 70:
                continue
            if not ticker or ticker not in prices_data:
                continue
            p = prices_data[ticker]
            results.append({
                "category": "beneficiari",
                "ticker": ticker,
                "current_price": p["price"],
                "currency": p["currency"],
                "change_pct_1d": p.get("change_1d"),
                "change_pct_7d": p.get("change_7d"),
                "change_pct_30d": p.get("change_30d"),
                "score": score,
                "title": item.get("title") or f"Opportunità su {ticker}",
                "reason": item.get("reason") or "",
                "news_driver": item.get("news_driver") or "",
                "expected_move": item.get("expected_move") or "+5-10%",
                "time_horizon": item.get("time_horizon") or "medium",
                "risk": item.get("risk") or "MED",
            })
        return results
    except Exception as e:
        print(f"[beneficiari AI error] {e}")
        return []


# ============================================================
# CATEGORIA 3: SOTTOVALUTATI (drawdown moderato, no crash)
# ============================================================
# Un asset è "sottovalutato" se:
#   - Drawdown 30d tra -10% e -25% (moderato, non crollo)
#   - Drawdown 7d > -10% (non in crollo attivo)
#   - Variazione 1d > -5% (non in crash giornaliero)

def compute_sottovalutati(prices_data, top_n=6):
    candidates = []
    for ticker, p in prices_data.items():
        ch_1d = p.get("change_1d") or 0
        ch_7d = p.get("change_7d")
        ch_30d = p.get("change_30d")

        # Servono storici 30d disponibili
        if ch_30d is None:
            continue
        # Drawdown 30d tra -10% e -25%
        if not (-25 <= ch_30d <= -10):
            continue
        # Non in crash 7d
        if (ch_7d or 0) < -10:
            continue
        # Non in crash 1d
        if ch_1d < -5:
            continue

        score = 65 + min(25, int(abs(ch_30d)))

        candidates.append({
            "category": "sottovalutati",
            "ticker": ticker,
            "current_price": p["price"],
            "currency": p["currency"],
            "change_pct_1d": ch_1d,
            "change_pct_7d": ch_7d,
            "change_pct_30d": ch_30d,
            "score": score,
            "title": f"Sottovalutato: {ch_30d:+.1f}% in 30 giorni",
            "reason": (
                "Drawdown moderato senza crash acuti. L'asset si è ritracciato in modo ordinato, "
                "possibile occasione di valore se i fondamentali rimangono solidi e la tesi di lungo periodo è intatta."
            ),
            "expected_move": "+8-18%",
            "time_horizon": "medium",
            "risk": "MED",
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


# ============================================================
# MAIN
# ============================================================

def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("[opportunities] Inizio analisi (soglie severe)...")

    prices_data = fetch_prices_with_history(supabase)
    print(f"[opportunities] Caricati {len(prices_data)} ticker con prezzi/storia")

    news = fetch_recent_news(supabase, hours=72, limit=30)
    print(f"[opportunities] Caricate {len(news)} news ultime 72h")

    crolli = compute_crolli(prices_data, top_n=10)
    print(f"[opportunities] Trovati {len(crolli)} crolli (>= -10% 1d O -15% 7d O -25% 30d)")

    beneficiari = compute_beneficiari(news, prices_data, top_n=6)
    print(f"[opportunities] Trovati {len(beneficiari)} beneficiari AI (conviction >= 70)")

    sottovalutati = compute_sottovalutati(prices_data, top_n=6)
    print(f"[opportunities] Trovati {len(sottovalutati)} sottovalutati (drawdown 30d -10%/-25%)")

    all_ops = crolli + beneficiari + sottovalutati

    # Pulizia: cancella tutto e reinserisci. Se niente è qualificato, la tabella rimane vuota.
    try:
        supabase.table("opportunities").delete().neq("id", 0).execute()
    except Exception as e:
        print(f"[cleanup error] {e}")

    if not all_ops:
        print("[opportunities] Nessuna occasione qualificata trovata oggi. Tabella vuota.")
        return

    saved = 0
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    for op in all_ops:
        try:
            payload = {**op, "expires_at": expires_at}
            supabase.table("opportunities").insert(payload).execute()
            saved += 1
        except Exception as e:
            print(f"  [insert error] {op.get('ticker')}: {e}")

    print(f"[opportunities] Salvate {saved}/{len(all_ops)} opportunità.")


if __name__ == "__main__":
    main()
