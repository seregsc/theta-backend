"""
generate_opportunities.py — VERSIONE 5 CATEGORIE MUTUAMENTE ESCLUSIVE

Ogni asset finisce in UNA SOLA categoria, scelta secondo questa priorità:

  1. crollo_lampo       — change_1d  <= -5%
  2. crolli_recenti     — change_30d <= -15% (e non è già crollo lampo)
  3. eventi_macro       — ticker nei winners di scenari attivi recenti
  4. news_positive      — sentiment medio positivo nelle news 7gg, almeno 1 news
  5. sotto_radar        — score AI >= 75 ma <= 1 news nelle ultime 2 settimane

Per ciascuna categoria, le opportunità sono arricchite via Haiku con analisi
in italiano semplice (titolo, summary, reason, catalyst, risks).

Le scadute restano visibili 30 giorni.
"""

import os
import json
from datetime import datetime, timezone, timedelta
from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL = "claude-haiku-4-5-20251001"

# Limiti per categoria (per controllo costi e qualità)
TOP_CROLLO_LAMPO = 15
TOP_CROLLI_RECENTI = 15
TOP_EVENTI_MACRO = 10
TOP_NEWS_POSITIVE = 10
TOP_SOTTO_RADAR = 8


# ════════════════════════════════════════════════════════════════
# UTILS
# ════════════════════════════════════════════════════════════════

def normalize_sentiment_score(raw):
    """Ritorna un float tra -1 e 1, o None se non interpretabile."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("positive", "pos", "bullish"):
            return 0.5
        if s in ("negative", "neg", "bearish"):
            return -0.5
        if s in ("neutral", "mixed"):
            return 0.0
        try:
            return float(s)
        except Exception:
            return None
    return None


def normalize_tickers(raw):
    if isinstance(raw, list):
        return [str(t).strip().upper() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(t).strip().upper() for t in parsed if str(t).strip()]
        except Exception:
            pass
        return [t.strip().upper() for t in s.split(",") if t.strip()]
    return []


def parse_json_loose(text):
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3].strip()
        elif "```" in text:
            text = text[:text.rfind("```")].strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
        return None


# ════════════════════════════════════════════════════════════════
# FETCH DATI
# ════════════════════════════════════════════════════════════════

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


def fetch_news_window(supabase, hours, limit=400):
    """News in una finestra temporale (per legare news ai ticker)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        res = (supabase.table("news")
               .select("id, title, title_it, summary, summary_it, tickers, sentiment, published_at, category")
               .gte("published_at", since)
               .order("published_at", desc=True)
               .limit(limit)
               .execute())
        return res.data or []
    except Exception as e:
        print(f"[news fetch error] {e}")
        return []


def build_news_index(news_list):
    """Indice ticker -> lista news, con score medio sentiment."""
    by_ticker = {}
    for n in news_list:
        tks = normalize_tickers(n.get("tickers"))
        sent_raw = n.get("sentiment")
        sent_score = normalize_sentiment_score(sent_raw)
        title = n.get("title_it") or n.get("title") or ""
        summary = n.get("summary_it") or n.get("summary") or ""
        for t in tks:
            by_ticker.setdefault(t, []).append({
                "title": title,
                "summary": summary[:300],
                "sentiment_score": sent_score,
                "published_at": n.get("published_at"),
                "category": n.get("category"),
            })
    return by_ticker


def fetch_active_scenarios_winners(supabase, limit=10):
    """Ritorna set di ticker presenti nei `winners` degli scenari recenti."""
    try:
        res = (supabase.table("scenarios_live")
               .select("id, title, winners, generated_at")
               .order("generated_at", desc=True)
               .limit(limit)
               .execute())
    except Exception as e:
        print(f"[scenarios fetch error] {e}")
        return set(), {}

    winner_tickers = set()
    ticker_to_scenario = {}
    for s in (res.data or []):
        winners = s.get("winners") or []
        if isinstance(winners, str):
            try:
                winners = json.loads(winners)
            except Exception:
                winners = []
        for w in winners:
            if isinstance(w, dict):
                t = (w.get("ticker") or "").strip().upper()
                if t:
                    winner_tickers.add(t)
                    if t not in ticker_to_scenario:
                        ticker_to_scenario[t] = {
                            "scenario_title": s.get("title"),
                            "expected_move": w.get("expected_move"),
                            "why": w.get("why"),
                        }
    return winner_tickers, ticker_to_scenario


def fetch_existing_opportunities(supabase):
    try:
        res = supabase.table("opportunities").select("*").execute()
        return res.data or []
    except Exception as e:
        print(f"[existing fetch error] {e}")
        return []


# ════════════════════════════════════════════════════════════════
# CLASSIFICAZIONE (mutuamente esclusiva, in ordine di priorità)
# ════════════════════════════════════════════════════════════════

def classify_ticker(ticker, price_data, news_for_ticker, macro_winner_info):
    """Ritorna (categoria, dati_categoria) o (None, None) se non rientra in nessuna."""
    ch_1d = price_data.get("change_1d") or 0
    ch_30d = price_data.get("change_30d")

    # 1) Crollo lampo
    if ch_1d <= -5:
        return ("crollo_lampo", {"trigger": "calo intraday >= 5%", "drop_1d": ch_1d})

    # 2) Crolli recenti
    if ch_30d is not None and ch_30d <= -15:
        return ("crolli_recenti", {"trigger": "calo 30gg >= 15%", "drop_30d": ch_30d})

    # 3) Eventi macro favorevoli
    if macro_winner_info:
        return ("eventi_macro", macro_winner_info)

    # 4) News positive
    if news_for_ticker:
        sentiments = [n.get("sentiment_score") for n in news_for_ticker if n.get("sentiment_score") is not None]
        if sentiments:
            avg = sum(sentiments) / len(sentiments)
            if avg >= 0.3 and len(news_for_ticker) >= 1:
                return ("news_positive", {
                    "avg_sentiment": round(avg, 2),
                    "news_count": len(news_for_ticker),
                    "top_news_title": news_for_ticker[0]["title"],
                })

    # 5) Sotto i radar — score "AI" è da decidere; qui usiamo un proxy:
    #    un asset con drawdown molto piccolo (-7..+5) e poche news = "stabile e sotto radar"
    #    non perfetto, ma onesto coi dati che abbiamo. Verrà migliorato quando avremo P/E.
    news_count_14d = len(news_for_ticker) if news_for_ticker else 0
    if ch_30d is not None and -10 <= ch_30d <= 5 and news_count_14d <= 1:
        # Solo se ha un movimento 1d non negativo per evitare di prendere asset in declino lento
        if ch_1d >= -2:
            return ("sotto_radar", {
                "news_count_14d": news_count_14d,
                "stability": "movimento contenuto + bassa copertura mediatica",
            })

    return (None, None)


# ════════════════════════════════════════════════════════════════
# ARRICCHIMENTO AI
# ════════════════════════════════════════════════════════════════

CATEGORY_LABELS = {
    "crollo_lampo": "Crollo lampo (calo intraday significativo)",
    "crolli_recenti": "Crollo recente (correzione strutturale 30gg)",
    "eventi_macro": "Beneficiario di scenari macro recenti",
    "news_positive": "Catalyst da news positive recenti",
    "sotto_radar": "Asset stabile con poca copertura mediatica",
}

CATEGORY_BRIEF = {
    "crollo_lampo": "questo asset ha avuto un calo intraday del 5% o più nelle ultime ore",
    "crolli_recenti": "questo asset ha perso il 15% o più nell'ultimo mese, è in correzione",
    "eventi_macro": "questo asset è tra i beneficiari attesi di uno scenario di mercato attivo",
    "news_positive": "questo asset ha avuto news con tono positivo negli ultimi 7 giorni",
    "sotto_radar": "questo asset è stabile e poco coperto dalla stampa: potrebbe essere un'opportunità non ancora prezzata dal mercato",
}


def build_news_context_for_ticker(news_for_ticker, max_items=4):
    if not news_for_ticker:
        return "Nessuna news specifica recente."
    lines = []
    for n in news_for_ticker[:max_items]:
        lines.append(f"- {n['title']}\n  {n['summary'][:200]}")
    return "\n".join(lines)


def enrich_with_ai(client, opp, news_for_ticker, category_extra):
    """Arricchisce con AI in italiano semplice. Restituisce opp modificata."""
    ticker = opp["ticker"]
    category = opp["category"]
    ch_1d = opp.get("change_pct_1d") or 0
    ch_7d = opp.get("change_pct_7d")
    ch_30d = opp.get("change_pct_30d")
    price = opp.get("current_price")
    currency = opp.get("currency", "USD")

    perf_parts = [f"oggi {ch_1d:+.2f}%"]
    if ch_7d is not None:
        perf_parts.append(f"a 7 giorni {ch_7d:+.2f}%")
    if ch_30d is not None:
        perf_parts.append(f"a 30 giorni {ch_30d:+.2f}%")
    perf_str = ", ".join(perf_parts)

    extra_str = ""
    if category == "eventi_macro" and category_extra:
        extra_str = (
            f"\nSCENARIO MACRO ATTIVO che lo favorisce:\n"
            f"- Titolo: {category_extra.get('scenario_title', '—')}\n"
            f"- Movimento atteso per questo asset: {category_extra.get('expected_move', '—')}\n"
            f"- Perché beneficia: {category_extra.get('why', '—')}"
        )
    elif category == "news_positive" and category_extra:
        extra_str = (
            f"\nNEWS POSITIVE recenti su questo asset:\n"
            f"- Sentiment medio: {category_extra.get('avg_sentiment')}\n"
            f"- News principale: {category_extra.get('top_news_title', '—')}"
        )
    elif category == "sotto_radar" and category_extra:
        extra_str = (
            f"\nPROFILO 'SOTTO I RADAR':\n"
            f"- News nelle ultime 2 settimane: {category_extra.get('news_count_14d')}\n"
            f"- Caratteristica: {category_extra.get('stability')}"
        )

    news_ctx = build_news_context_for_ticker(news_for_ticker)

    prompt = f"""Sei un analista finanziario senior italiano. Scrivi per un consulente che userà i testi col cliente (spesso non esperto).

DATI ASSET:
- Ticker: {ticker}
- Prezzo: {currency} {price}
- Performance: {perf_str}
- Categoria: {CATEGORY_LABELS[category]}
- Situazione: {CATEGORY_BRIEF[category]}
{extra_str}

NEWS RECENTI sull'asset:
{news_ctx}

LESSICO OBBLIGATORIO:
1. Italiano SEMPLICE, frasi brevi (max 25 parole).
2. NIENTE jargon vuoto: "rationale solido", "endorsement major player", "fondamentali solidi", "guidance robusta", "tesi intatta", "outlook positivo".
3. Sostituisci tecnicismi: "drawdown" → "calo dal massimo"; "buyback" → "acquisto azioni proprie"; "guidance" → "previsioni dell'azienda"; "EPS" → "utile per azione"; "earnings" → "trimestrale"; "P/E" → "rapporto prezzo/utili".
4. Usa sempre fatti concreti: numeri, date, nomi di prodotti.
5. Se non sai il driver specifico: dichiaralo onestamente (es. "Il prezzo è sceso senza una news pubblica chiara").

OUTPUT — solo JSON, niente preamboli, con questi campi:
- title: 60-80 char, descrittivo e specifico
- summary: 1-2 frasi semplici (max 200 char)
- reason: 3-5 frasi (300-600 char). Struttura: cosa è successo → perché → perché è un'occasione
- catalyst: 1-2 frasi (max 250 char), eventi futuri specifici, date se possibile
- risks: 1-2 frasi (max 250 char), rischi specifici
- target_timing: orizzonte realistico (es. "1-3 mesi", "6-12 mesi")
- expected_move: "+X-Y%" o "-X/-Y%"
- conviction: numero 60-95

Rispondi SOLO con JSON."""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        parsed = parse_json_loose(text)
        if not isinstance(parsed, dict):
            return opp
        opp["title"] = parsed.get("title") or opp.get("title", f"Opportunità su {ticker}")
        opp["summary"] = parsed.get("summary", "")
        opp["reason"] = parsed.get("reason", "")
        opp["catalyst"] = parsed.get("catalyst", "")
        opp["risks"] = parsed.get("risks", "")
        opp["target_timing"] = parsed.get("target_timing", "")
        opp["expected_move"] = parsed.get("expected_move") or opp.get("expected_move", "")
        opp["conviction"] = int(parsed.get("conviction") or 70)
        return opp
    except Exception as e:
        print(f"  [AI enrich error] {ticker}: {e}")
        return opp


# ════════════════════════════════════════════════════════════════
# PIPELINE
# ════════════════════════════════════════════════════════════════

def build_candidate(ticker, price_data, category, category_extra):
    ch_1d = price_data.get("change_1d") or 0
    ch_7d = price_data.get("change_7d")
    ch_30d = price_data.get("change_30d")

    # Severità grezza per ordinare
    if category == "crollo_lampo":
        score = min(100, 60 + int(abs(ch_1d) * 3))
        risk = "HIGH" if ch_1d <= -10 else "MED"
    elif category == "crolli_recenti":
        score = min(100, 55 + int(abs(ch_30d or 0)))
        risk = "HIGH" if (ch_30d or 0) <= -25 else "MED"
    elif category == "eventi_macro":
        score = 75
        risk = "MED"
    elif category == "news_positive":
        avg = (category_extra or {}).get("avg_sentiment") or 0
        score = 60 + int(min(30, avg * 60))
        risk = "MED"
    else:  # sotto_radar
        score = 65
        risk = "LOW"

    return {
        "category": category,                     # categoria interna
        "live_category": category,                # alias letto dal frontend
        "ticker": ticker,
        "current_price": price_data["price"],
        "currency": price_data["currency"],
        "change_pct_1d": ch_1d,
        "change_pct_7d": ch_7d,
        "change_pct_30d": ch_30d,
        "score": score,
        "risk": risk,
        "title": f"{ticker}",
        "reason": "",
        "status": "active",
    }


def evaluate_existing(opp, prices_data):
    """Ritorna True se l'opportunità è ancora valida con i dati attuali."""
    ticker = opp["ticker"]
    current = prices_data.get(ticker)
    if not current or not current.get("price"):
        return False
    new_price = current["price"]
    old_price = float(opp.get("current_price") or 0)
    if old_price > 0:
        delta_pct = ((new_price - old_price) / old_price) * 100
    else:
        delta_pct = 0
    cat = opp.get("live_category") or opp.get("category")
    # Soglie di "decadimento" per categoria
    if cat == "crollo_lampo" and delta_pct >= 10:
        return False
    if cat == "crolli_recenti" and delta_pct >= 15:
        return False
    if cat == "eventi_macro" and delta_pct >= 20:
        return False
    if cat == "news_positive" and delta_pct >= 15:
        return False
    if cat == "sotto_radar" and abs(delta_pct) >= 12:
        return False
    # Scadenza dura: 60gg
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


def cap_per_category(candidates):
    """Limita il numero per categoria, ordinando per score."""
    limits = {
        "crollo_lampo": TOP_CROLLO_LAMPO,
        "crolli_recenti": TOP_CROLLI_RECENTI,
        "eventi_macro": TOP_EVENTI_MACRO,
        "news_positive": TOP_NEWS_POSITIVE,
        "sotto_radar": TOP_SOTTO_RADAR,
    }
    by_cat = {}
    for c in candidates:
        by_cat.setdefault(c["category"], []).append(c)
    out = []
    for cat, items in by_cat.items():
        items.sort(key=lambda x: x["score"], reverse=True)
        out.extend(items[:limits.get(cat, 10)])
    return out


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
    print("[opportunities] Inizio analisi (5 categorie mutuamente esclusive)...")

    # 1. Dati
    prices_data = fetch_prices_with_history(supabase)
    print(f"  - Prezzi caricati: {len(prices_data)} ticker")

    news_7d = fetch_news_window(supabase, hours=168)
    news_14d = fetch_news_window(supabase, hours=336)
    print(f"  - News 7gg: {len(news_7d)} | News 14gg: {len(news_14d)}")

    news_index_7d = build_news_index(news_7d)
    news_index_14d = build_news_index(news_14d)

    macro_tickers, macro_info = fetch_active_scenarios_winners(supabase, limit=10)
    print(f"  - Ticker beneficiari da scenari macro: {len(macro_tickers)}")

    # 2. Cleanup scadute oltre 30 giorni
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    try:
        supabase.table("opportunities").delete().eq("status", "expired").lt("expired_at", cutoff_30d).execute()
    except Exception as e:
        print(f"  [cleanup error] {e}")

    # 3. Valuta opportunità attive esistenti
    existing = fetch_existing_opportunities(supabase)
    active_existing = [o for o in existing if o.get("status", "active") == "active"]
    now_iso = datetime.now(timezone.utc).isoformat()
    to_keep = []
    to_expire_ids = []
    reclassified = 0
    for opp in active_existing:
        ticker = opp.get("ticker")
        price_data = prices_data.get(ticker)
        if not price_data:
            to_expire_ids.append(opp["id"])
            continue

        # 3a. Prova la categoria attuale: l'opportunità è ancora valida?
        still_valid = evaluate_existing(opp, prices_data)

        if still_valid:
            # 3b. Anche se valida, verifica che il ticker rientri ANCORA nella sua categoria.
            # Es. un crollo_lampo di ieri potrebbe non avere più change_1d <= -5 oggi:
            # in quel caso lo riclassifichiamo (es. in crolli_recenti se applicabile).
            macro_extra = macro_info.get(ticker) if ticker in macro_tickers else None
            news_for_ticker_7d = news_index_7d.get(ticker, [])
            news_for_ticker_14d = news_index_14d.get(ticker, [])
            current_cat, _extra = classify_ticker(
                ticker, price_data,
                news_for_ticker_7d if news_for_ticker_7d else news_for_ticker_14d,
                macro_extra,
            )
            if current_cat == "sotto_radar" and len(news_for_ticker_14d) > 1:
                current_cat = None

            old_cat = opp.get("live_category") or opp.get("category")
            if current_cat and current_cat != old_cat:
                # Riclassifica: aggiorna live_category invece di scadere
                try:
                    supabase.table("opportunities").update({
                        "live_category": current_cat,
                        "category": current_cat,
                    }).eq("id", opp["id"]).execute()
                    opp["live_category"] = current_cat
                    opp["category"] = current_cat
                    reclassified += 1
                except Exception as e:
                    print(f"  [reclass error] {ticker}: {e}")
            to_keep.append(opp)
        else:
            to_expire_ids.append(opp["id"])
    print(f"  - Esistenti: mantenute {len(to_keep)} ({reclassified} riclassificate), scadute {len(to_expire_ids)}")
    for opp_id in to_expire_ids:
        try:
            supabase.table("opportunities").update({
                "status": "expired",
                "expired_at": now_iso,
            }).eq("id", opp_id).execute()
        except Exception as e:
            print(f"  [expire error] {opp_id}: {e}")

    existing_tickers_active = set()
    for opp in to_keep:
        existing_tickers_active.add(opp.get("ticker"))

    # 4. Classifica ogni ticker (mutuamente esclusivo)
    print("[opportunities] Classifico ticker...")
    candidates = []
    for ticker, price_data in prices_data.items():
        # Non duplicare ticker già attivi in DB
        if ticker in existing_tickers_active:
            continue
        macro_extra = macro_info.get(ticker) if ticker in macro_tickers else None
        news_for_ticker_14d = news_index_14d.get(ticker, [])
        news_for_ticker_7d = news_index_7d.get(ticker, [])

        # Per news_positive uso solo le ultime 7gg; per sotto_radar uso 14gg
        # Quindi passo sia 7 che 14: classify_ticker decide
        # Lo passiamo come 7gg (i più recenti per news_positive),
        # ma per sotto_radar serve il conteggio 14gg
        # Trick: per news_positive uso 7gg, per sotto_radar guardo il count su 14gg
        # Implemento qui un piccolo wrapper:
        cat, extra = classify_ticker(
            ticker, price_data,
            news_for_ticker_7d if news_for_ticker_7d else news_for_ticker_14d,
            macro_extra,
        )
        # Sotto-radar dipende anche dal conteggio 14gg, ricontrollo
        if cat == "sotto_radar":
            extra = {**(extra or {}), "news_count_14d": len(news_for_ticker_14d)}
            # se in 14gg ci sono > 1 news, non è davvero "sotto radar"
            if len(news_for_ticker_14d) > 1:
                cat = None
                extra = None
        if not cat:
            continue
        candidates.append((ticker, price_data, cat, extra, news_for_ticker_7d))

    # 5. Cap per categoria
    just_cands = [build_candidate(t, p, c, e) for (t, p, c, e, _) in candidates]
    just_cands = cap_per_category(just_cands)
    print(f"[opportunities] Candidati dopo cap: {len(just_cands)}")
    cap_set = {(c["ticker"], c["category"]) for c in just_cands}
    # Filtro la lista originale ai soli capati
    final = [(t, p, c, e, n) for (t, p, c, e, n) in candidates if (t, c) in cap_set]

    # 6. Arricchimento AI + insert
    saved = 0
    by_cat_count = {}
    for ticker, price_data, cat, extra, news_for_ticker in final:
        candidate = build_candidate(ticker, price_data, cat, extra)
        if anthropic_client:
            candidate = enrich_with_ai(anthropic_client, candidate, news_for_ticker, extra)
        try:
            supabase.table("opportunities").insert(candidate).execute()
            saved += 1
            by_cat_count[cat] = by_cat_count.get(cat, 0) + 1
        except Exception as e:
            print(f"  [insert error] {ticker}: {e}")

    print(f"[opportunities] Salvate {saved} nuove opportunità.")
    for cat, n in by_cat_count.items():
        print(f"  · {cat}: {n}")
    print(f"[opportunities] Totale attive in DB: {len(to_keep) + saved}.")


if __name__ == "__main__":
    main()
