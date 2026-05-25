"""
generate_opportunities.py
Analizza i prezzi live + storico + news per generare occasioni di mercato.

3 categorie:
  - 'crolli': asset con calo significativo (1d / 7d / 30d)
  - 'beneficiari': asset che possono beneficiare di news HIGH recenti (via AI)
  - 'sottovalutati': asset con score alto, lontani dal target di prezzo

Eseguito 2-3 volte al giorno via GitHub Actions.
"""

import os
import json
from datetime import datetime, timezone, timedelta
from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


# ============================================================
# UTILITIES
# ============================================================

def normalize_sentiment(raw):
    """Converte sentiment in stringa standard. Gestisce numerico, stringa, None."""
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
    """Converte tickers in lista di stringhe. Gestisce list, JSON string, comma-separated string."""
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        # Prova come JSON
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except Exception:
            pass
        # Fallback: split per virgola
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def fetch_prices_with_history(supabase):
    """Carica ultimi prezzi + history. Ritorna dict {ticker: {price, change_1d, change_7d, change_30d, currency}}"""
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
    """Carica le news degli ultimi N ore."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        res = supabase.table("news").select("*").gte("published_at", since).order("published_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        print(f"[news fetch error] {e}")
        return []


# ============================================================
# CATEGORIA 1: CROLLI RECENTI (matematica)
# ============================================================

def compute_crolli(prices_data, top_n=8):
    """Trova asset con i crolli più significativi."""
    candidates = []
    for ticker, p in prices_data.items():
        ch_1d = p.get("change_1d") or 0
        ch_7d = p.get("change_7d")
        is_crash_1d = ch_1d <= -3
        is_crash_7d = ch_7d is not None and ch_7d <= -7
        if not (is_crash_1d or is_crash_7d):
            continue
        values = [x for x in [ch_1d, ch_7d, p.get("change_30d") or 0] if x is not None]
        worst = min(values) if values else 0
        score = min(100, int(abs(worst) * 3))
        if is_crash_1d and abs(ch_1d) > abs(ch_7d or 0):
            title = f"Crollo del giorno: {ch_1d:+.1f}%"
        elif is_crash_7d:
            title = f"Crollo a 7 giorni: {ch_7d:+.1f}%"
        else:
            title = f"Drawdown significativo: {ch_1d:+.1f}%"
        candidates.append({
            "category": "crolli",
            "ticker": ticker,
            "current_price": p["price"],
            "currency": p["currency"],
            "change_pct_1d": ch_1d,
            "change_pct_7d": ch_7d,
            "change_pct_30d": p.get("change_30d"),
            "score": score,
            "title": title,
            "reason": "Calo significativo del prezzo nelle ultime sessioni. Possibile occasione di ingresso se la tesi sull'asset è ancora valida.",
            "expected_move": "Da valutare",
            "time_horizon": "short",
            "risk": "MED" if abs(worst) < 15 else "HIGH",
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


# ============================================================
# CATEGORIA 2: BENEFICIARI EVENTI (AI Claude)
# ============================================================

def compute_beneficiari(news_list, prices_data, top_n=6):
    """Usa Claude Haiku per identificare asset che possono beneficiare delle news recenti."""
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

    prompt = f"""Sei un analista finanziario. Analizza le news recenti e identifica fino a 6 asset (tra quelli disponibili) che possono BENEFICIARE significativamente da questi eventi nelle prossime settimane.

NEWS RECENTI (ultime 72 ore):
{news_text}

ASSET DISPONIBILI (ticker): {', '.join(available_tickers[:200])}

Per ogni asset suggerito, fornisci JSON con:
- ticker (deve essere uno della lista)
- title: breve titolo dell'opportunità (max 60 caratteri)
- reason: spiegazione di max 250 caratteri (PERCHÉ beneficierà)
- news_driver: la news principale che genera l'opportunità (max 100 caratteri)
- expected_move: range stimato come "+X-Y%" (es. "+5-12%")
- time_horizon: "short" (1-4 settimane) | "medium" (1-3 mesi) | "long" (3-12 mesi)
- risk: "LOW" | "MED" | "HIGH"
- score: 50-95 (priorità/qualità dell'opportunità)

Rispondi SOLO con un JSON array, nessun altro testo. Esempio:
[
  {{"ticker": "GLD", "title": "Oro: hedge contro escalation geopolitica", "reason": "...", "news_driver": "...", "expected_move": "+8-15%", "time_horizon": "medium", "risk": "LOW", "score": 80}}
]
"""
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
        results = []
        for item in parsed[:top_n]:
            ticker = item.get("ticker")
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
                "score": int(item.get("score") or 70),
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
# CATEGORIA 3: SOTTOVALUTATI (matematica)
# ============================================================

def compute_sottovalutati(prices_data, top_n=6):
    """Asset in flat/leggero ribasso ma fondamentalmente attraenti."""
    candidates = []
    for ticker, p in prices_data.items():
        ch_7d = p.get("change_7d")
        ch_30d = p.get("change_30d")
        if ch_30d is None:
            continue
        if not (-15 <= ch_30d <= -5):
            continue
        if (ch_7d or 0) < -10:
            continue
        score = 60 + min(20, int(abs(ch_30d)))
        candidates.append({
            "category": "sottovalutati",
            "ticker": ticker,
            "current_price": p["price"],
            "currency": p["currency"],
            "change_pct_1d": p.get("change_1d"),
            "change_pct_7d": ch_7d,
            "change_pct_30d": ch_30d,
            "score": score,
            "title": f"Sottovalutato: {ch_30d:+.1f}% in 30 giorni",
            "reason": "L'asset è in flat/ribasso moderato senza crash brutali. Possibile valore se i fondamentali sono solidi.",
            "expected_move": "+5-12%",
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
    print("[opportunities] Inizio analisi...")

    prices_data = fetch_prices_with_history(supabase)
    print(f"[opportunities] Caricati {len(prices_data)} ticker con prezzi/storia")

    news = fetch_recent_news(supabase, hours=72, limit=30)
    print(f"[opportunities] Caricate {len(news)} news ultime 72h")

    crolli = compute_crolli(prices_data, top_n=8)
    print(f"[opportunities] Trovati {len(crolli)} crolli")

    beneficiari = compute_beneficiari(news, prices_data, top_n=6)
    print(f"[opportunities] Trovati {len(beneficiari)} beneficiari AI")

    sottovalutati = compute_sottovalutati(prices_data, top_n=6)
    print(f"[opportunities] Trovati {len(sottovalutati)} sottovalutati")

    all_ops = crolli + beneficiari + sottovalutati

    expires = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
    try:
        supabase.table("opportunities").delete().lt("created_at", expires).execute()
    except Exception as e:
        print(f"[cleanup error] {e}")

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
