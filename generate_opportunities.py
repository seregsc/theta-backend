"""
generate_opportunities.py
Genera occasioni di mercato con:
- Soglie severe (solo opportunità di qualità)
- Descrizioni AI dettagliate e CONCRETE (no frasi vuote)
- Persistenza intelligente: opportunità rimangono finché AI le ritiene valide
- Status: 'active' o 'expired' (scadute restano visibili 30 giorni)
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
        if raw > 0.3: return "positive"
        elif raw < -0.3: return "negative"
        else: return "neutral"
    if isinstance(raw, str) and raw.strip():
        return raw
    return "neutral"


def normalize_tickers(raw):
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if not s: return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except Exception:
            pass
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def fetch_prices_with_history(supabase):
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
        if current <= 0: continue
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
            if old_price <= 0: continue
            pct = ((current - old_price) / old_price) * 100
            if change_7d is None and days >= 5: change_7d = pct
            if change_30d is None and days >= 25: change_30d = pct
            if change_7d is not None and change_30d is not None: break
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


def fetch_existing_opportunities(supabase):
    try:
        res = supabase.table("opportunities").select("*").execute()
        return res.data or []
    except Exception as e:
        print(f"[existing fetch error] {e}")
        return []


def enrich_with_ai(opportunity, news_context=""):
    """Chiede a Claude di arricchire l'opportunità con analisi dettagliata e CONCRETA.
    Prompt rigido per evitare frasi vuote tipo 'rationale solido', 'endorsement major player'."""
    if not ANTHROPIC_API_KEY:
        return opportunity

    ticker = opportunity["ticker"]
    category = opportunity["category"]
    ch_1d = opportunity.get("change_pct_1d") or 0
    ch_7d = opportunity.get("change_pct_7d")
    ch_30d = opportunity.get("change_pct_30d")
    price = opportunity.get("current_price")
    currency = opportunity.get("currency", "USD")

    perf_parts = [f"oggi {ch_1d:+.2f}%"]
    if ch_7d is not None:
        perf_parts.append(f"a 7 giorni {ch_7d:+.2f}%")
    if ch_30d is not None:
        perf_parts.append(f"a 30 giorni {ch_30d:+.2f}%")
    perf_str = ", ".join(perf_parts)

    category_brief = {
        "crolli": "questo asset ha avuto un crollo significativo recente",
        "sottovalutati": "questo asset è in ribasso moderato senza crash, potrebbe essere sottovalutato",
        "beneficiari": "questo asset potrebbe beneficiare di eventi recenti documentati nelle news",
    }

    prompt = f"""Sei un analista finanziario senior italiano. Scrivi un'analisi PROFESSIONALE per un consulente che la userà col cliente.

DATI:
- Asset: {ticker}
- Prezzo attuale: {currency} {price}
- Performance: {perf_str}
- Situazione: {category_brief.get(category, "")}

NEWS RECENTI (può contenere driver rilevanti):
{news_context[:1500] if news_context else "Nessuna news specifica."}

ISTRUZIONI CRITICHE:
1. NON usare MAI frasi vuote tipo: "rationale solido", "driver concreto", "endorsement major player", "fondamentali solidi", "outlook positivo", "guidance robusta", "esposizione strategica", "tesi intatta".
2. USA SOLO FATTI CONCRETI: nomi di prodotti, numeri (ricavi, P/E, margini), eventi specifici (date, news), settori di business reali.
3. Se non sai cosa è successo specificamente, ammetti onestamente: "Il movimento di prezzo non è collegato a news pubbliche identificabili" e basa l'analisi sul contesto settoriale.
4. Scrivi in italiano scorrevole, come un report di banca d'affari rivolto a un consulente.

Fornisci JSON con questi campi obbligatori:
- title: 60-80 caratteri, descrittivo e SPECIFICO (es. "Gilead -15% post-trial fase 3 fallito: rimbalzo se Q4 conferma cash flow")
- summary: 1-2 frasi (max 200 char), sintesi factuale
- reason: 3-5 frasi (300-600 caratteri). Struttura: COSA è successo (fatti) → PERCHÉ è successo (driver) → PERCHÉ è un'occasione adesso (tesi)
- catalyst: 1-2 frasi (max 250 char). Eventi/condizioni FUTURE specifiche che potrebbero far apprezzare l'asset (es. "Earnings Q4 il 15 gennaio: previsto FCF 8B; eventuale riacquisto azioni nel 2026")
- risks: 1-2 frasi (max 250 char). Rischi SPECIFICI a questo asset, non generici
- target_timing: orizzonte (es. "1-3 mesi", "6-12 mesi")
- conviction: 60-95

Rispondi SOLO con JSON, nessun altro testo.

ESEMPIO BUONO (NON copiarlo, usa la logica):
{{
  "title": "Boeing -18% in 30gg: ritardo 737 MAX 10 ma backlog 5.000 aerei intatto",
  "summary": "Boeing scende su delay certificazione MAX 10 ma il backlog di 5000 aerei resta intatto e Airbus non può sostituire.",
  "reason": "Boeing è scesa del 18% in 30 giorni dopo che FAA ha rinviato la certificazione del 737 MAX 10 a Q3 2026. Il mercato teme delay simili per gli altri modelli. Tuttavia il backlog ordini è ai massimi storici (5.000 aerei) e Airbus non ha capacità di produzione per rubargli quota. Il free cash flow tornerà positivo nel Q4 2026 secondo guidance, dimezzando il debito.",
  "catalyst": "Certificazione MAX 10 prevista Q3 2026; consegna primo 777X a Lufthansa nel 2026; guidance FCF positivo nel Q3 earnings di ottobre.",
  "risks": "Nuovi incidenti su 737 MAX (probabilità bassa ma impatto altissimo); strike sindacale a Seattle non risolto; concorrenza COMAC C919 in Cina.",
  "target_timing": "6-12 mesi",
  "conviction": 78
}}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
        opportunity["title"] = parsed.get("title") or opportunity.get("title", "")
        opportunity["reason"] = parsed.get("reason") or opportunity.get("reason", "")
        opportunity["summary"] = parsed.get("summary", "")
        opportunity["catalyst"] = parsed.get("catalyst", "")
        opportunity["risks"] = parsed.get("risks", "")
        opportunity["target_timing"] = parsed.get("target_timing", "")
        opportunity["conviction"] = int(parsed.get("conviction") or 70)
        return opportunity
    except Exception as e:
        print(f"  [AI enrich error] {ticker}: {e}")
        return opportunity


def evaluate_existing(opp, current_price_data):
    """True se ancora valida, False se obsoleta."""
    ticker = opp["ticker"]
    old_price = float(opp.get("current_price") or 0)
    current = current_price_data.get(ticker)
    if not current or not current.get("price"):
        return False
    new_price = current["price"]
    if old_price > 0:
        delta_pct = ((new_price - old_price) / old_price) * 100
    else:
        delta_pct = 0
    category = opp["category"]
    if category == "crolli" and delta_pct >= 15:
        return False
    if category == "sottovalutati" and delta_pct >= 12:
        return False
    if category == "beneficiari" and delta_pct >= 20:
        return False
    # Cap massimo a 60 giorni per evitare opportunità "zombie"
    try:
        created = opp.get("created_at")
        if isinstance(created, str):
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        else:
            created_dt = created
        if created_dt and (datetime.now(timezone.utc) - created_dt).days >= 60:
            return False
    except Exception:
        pass
    return True


def compute_crolli(prices_data, news_text="", top_n=10):
    candidates = []
    for ticker, p in prices_data.items():
        ch_1d = p.get("change_1d") or 0
        ch_7d = p.get("change_7d")
        ch_30d = p.get("change_30d")
        crash_1d = ch_1d <= -10
        crash_7d = ch_7d is not None and ch_7d <= -15
        crash_30d = ch_30d is not None and ch_30d <= -25
        if not (crash_1d or crash_7d or crash_30d):
            continue
        values = [v for v in [ch_1d, ch_7d, ch_30d] if v is not None]
        worst = min(values) if values else 0
        score = min(100, 50 + int(abs(worst) * 2))
        if crash_1d:
            base_title = f"{ticker}: crollo {ch_1d:+.1f}% in 1 giorno"
        elif crash_7d:
            base_title = f"{ticker}: drawdown {ch_7d:+.1f}% in 7 giorni"
        else:
            base_title = f"{ticker}: deep correction {ch_30d:+.1f}% in 30 giorni"
        candidates.append({
            "category": "crolli", "ticker": ticker,
            "current_price": p["price"], "currency": p["currency"],
            "change_pct_1d": ch_1d, "change_pct_7d": ch_7d, "change_pct_30d": ch_30d,
            "score": score, "title": base_title, "reason": "",
            "expected_move": "Recovery 10-25%" if abs(worst) > 20 else "Rimbalzo 5-15%",
            "time_horizon": "short" if crash_1d else "medium",
            "risk": "HIGH" if abs(worst) >= 25 else "MED",
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    enriched = []
    for c in candidates[:top_n]:
        enriched.append(enrich_with_ai(c, news_text))
    return enriched


def compute_beneficiari(news_list, prices_data, top_n=6):
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

    prompt = f"""Sei un analista finanziario senior. Seleziona SOLO opportunità con CONVICTION ALTA.

NEWS RECENTI (ultime 72h):
{news_text}

ASSET DISPONIBILI: {', '.join(available_tickers[:200])}

REGOLE:
1. Suggerisci asset SOLO con CONVICTION ALTA (score >= 70) che possa beneficiare di eventi concreti.
2. NON suggerire generici. Solo driver chiaro e dimostrabile dalle news.
3. NON usare frasi vuote tipo "rationale solido", "endorsement major player". Usa fatti concreti.
4. Se nessuna news ha eventi impattanti, RISPONDI con [].

Per ogni asset, JSON in ITALIANO con:
- ticker (nella lista)
- title: max 70 caratteri descrittivo e SPECIFICO
- summary: 1-2 frasi max 200 char
- reason: 3-5 frasi max 600 char. Struttura: COSA è successo (news) → COME impatta l'asset → PERCHÉ è un'occasione
- catalyst: 1-2 frasi max 250 char con eventi/condizioni future SPECIFICHE
- risks: 1-2 frasi max 250 char con rischi SPECIFICI
- news_driver: la news specifica che genera l'opportunità (max 100 char)
- target_timing: orizzonte (es. "1-3 mesi")
- expected_move: "+X-Y%"
- time_horizon: "short" | "medium" | "long"
- risk: "LOW" | "MED" | "HIGH"
- score: 70-95
- conviction: 70-95

Rispondi SOLO con JSON array. Se niente, []."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
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
            if score < 70: continue
            if not ticker or ticker not in prices_data: continue
            p = prices_data[ticker]
            results.append({
                "category": "beneficiari", "ticker": ticker,
                "current_price": p["price"], "currency": p["currency"],
                "change_pct_1d": p.get("change_1d"),
                "change_pct_7d": p.get("change_7d"),
                "change_pct_30d": p.get("change_30d"),
                "score": score,
                "title": item.get("title") or f"Opportunità su {ticker}",
                "summary": item.get("summary", ""),
                "reason": item.get("reason", ""),
                "catalyst": item.get("catalyst", ""),
                "risks": item.get("risks", ""),
                "news_driver": item.get("news_driver", ""),
                "target_timing": item.get("target_timing", ""),
                "expected_move": item.get("expected_move") or "+5-10%",
                "time_horizon": item.get("time_horizon") or "medium",
                "risk": item.get("risk") or "MED",
                "conviction": int(item.get("conviction") or 75),
            })
        return results
    except Exception as e:
        print(f"[beneficiari AI error] {e}")
        return []


def compute_sottovalutati(prices_data, news_text="", top_n=6):
    candidates = []
    for ticker, p in prices_data.items():
        ch_1d = p.get("change_1d") or 0
        ch_7d = p.get("change_7d")
        ch_30d = p.get("change_30d")
        if ch_30d is None: continue
        if not (-25 <= ch_30d <= -10): continue
        if (ch_7d or 0) < -10: continue
        if ch_1d < -5: continue
        score = 65 + min(25, int(abs(ch_30d)))
        candidates.append({
            "category": "sottovalutati", "ticker": ticker,
            "current_price": p["price"], "currency": p["currency"],
            "change_pct_1d": ch_1d, "change_pct_7d": ch_7d, "change_pct_30d": ch_30d,
            "score": score,
            "title": f"{ticker}: sottovalutato {ch_30d:+.1f}% in 30 giorni",
            "reason": "", "expected_move": "+8-18%",
            "time_horizon": "medium", "risk": "MED",
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    enriched = []
    for c in candidates[:top_n]:
        enriched.append(enrich_with_ai(c, news_text))
    return enriched


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("[opportunities] Inizio analisi (persistenza + scadute + AI rigorosa)...")

    prices_data = fetch_prices_with_history(supabase)
    print(f"[opportunities] Caricati {len(prices_data)} ticker")

    news = fetch_recent_news(supabase, hours=72, limit=30)
    print(f"[opportunities] Caricate {len(news)} news")
    news_lines = []
    for n in news[:10]:
        t = n.get("title_it") or n.get("title", "")
        s = (n.get("summary_it") or n.get("summary", ""))[:200]
        if t: news_lines.append(f"- {t}\n  {s}")
    news_text = "\n".join(news_lines)

    # STEP 1: pulizia scadute oltre 30 giorni
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    try:
        supabase.table("opportunities").delete().eq("status", "expired").lt("expired_at", cutoff_30d).execute()
        print(f"[opportunities] Pulite scadute oltre 30gg")
    except Exception as e:
        print(f"  [cleanup expired error] {e}")

    # STEP 2: valuta opportunità ATTIVE esistenti
    existing = fetch_existing_opportunities(supabase)
    active_existing = [o for o in existing if o.get("status", "active") == "active"]
    print(f"[opportunities] Esistenti attive: {len(active_existing)}")
    now_iso = datetime.now(timezone.utc).isoformat()
    to_keep = []
    to_expire_ids = []
    for opp in active_existing:
        if evaluate_existing(opp, prices_data):
            to_keep.append(opp)
        else:
            to_expire_ids.append(opp["id"])
    print(f"[opportunities] Da mantenere attive: {len(to_keep)}, da scadere: {len(to_expire_ids)}")

    for opp_id in to_expire_ids:
        try:
            supabase.table("opportunities").update({
                "status": "expired",
                "expired_at": now_iso,
            }).eq("id", opp_id).execute()
        except Exception as e:
            print(f"  [expire error] {opp_id}: {e}")

    # STEP 3: genera nuove
    existing_tickers_by_cat = {}
    for opp in to_keep:
        cat = opp["category"]
        existing_tickers_by_cat.setdefault(cat, set()).add(opp["ticker"])

    crolli = compute_crolli(prices_data, news_text=news_text, top_n=10)
    crolli = [c for c in crolli if c["ticker"] not in existing_tickers_by_cat.get("crolli", set())]
    print(f"[opportunities] Crolli nuovi: {len(crolli)}")

    beneficiari = compute_beneficiari(news, prices_data, top_n=6)
    beneficiari = [b for b in beneficiari if b["ticker"] not in existing_tickers_by_cat.get("beneficiari", set())]
    print(f"[opportunities] Eventi favorevoli nuovi: {len(beneficiari)}")

    sottovalutati = compute_sottovalutati(prices_data, news_text=news_text, top_n=6)
    sottovalutati = [s for s in sottovalutati if s["ticker"] not in existing_tickers_by_cat.get("sottovalutati", set())]
    print(f"[opportunities] Sottovalutati nuovi: {len(sottovalutati)}")

    all_new = crolli + beneficiari + sottovalutati

    saved = 0
    for op in all_new:
        try:
            op["status"] = "active"
            supabase.table("opportunities").insert(op).execute()
            saved += 1
        except Exception as e:
            print(f"  [insert error] {op.get('ticker')}: {e}")

    print(f"[opportunities] Salvate {saved} nuove.")
    print(f"[opportunities] Totale attive in DB: {len(to_keep) + saved}.")


if __name__ == "__main__":
    main()
