"""
generate_alerts.py
Rileva eventi estremi nelle news recenti e genera alert critici
per ogni consulente, analizzando i portafogli reali dei clienti.

Da eseguire ogni 1-2 ore via GitHub Actions.
È un sistema che produce alert RARI: nel 95% delle esecuzioni non trova nulla
di abbastanza critico e termina senza generare alert.
"""

import os
import json
from datetime import datetime, timedelta, timezone
from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL = "claude-sonnet-4-5"

# Auto-archive alert più vecchi di N giorni
ARCHIVE_AFTER_HOURS = 72

# Finestra news da valutare
NEWS_WINDOW_HOURS = 12


def fetch_recent_high_news(supabase):
    """News con priority HIGH degli ultimi NEWS_WINDOW_HOURS"""
    since = (datetime.now(timezone.utc) - timedelta(hours=NEWS_WINDOW_HOURS)).isoformat()
    try:
        res = supabase.table("news") \
            .select("id, title_it, summary_it, impact_it, strategy_it, source, published_at, tickers, sentiment, category, priority") \
            .gte("created_at", since) \
            .eq("priority", "HIGH") \
            .order("created_at", desc=True) \
            .limit(40) \
            .execute()
        return res.data or []
    except Exception as e:
        print(f"[news error] {e}")
        return []


def fetch_existing_alert_news_ids(supabase):
    """ID delle news per cui abbiamo già generato un alert (per non duplicare)"""
    since = (datetime.now(timezone.utc) - timedelta(hours=NEWS_WINDOW_HOURS * 4)).isoformat()
    try:
        res = supabase.table("live_alerts") \
            .select("trigger_news_id") \
            .gte("triggered_at", since) \
            .execute()
        return {r["trigger_news_id"] for r in (res.data or []) if r.get("trigger_news_id")}
    except Exception as e:
        return set()


def fetch_all_users_with_clients(supabase):
    """Tutti gli user_id distinti che hanno clienti"""
    try:
        res = supabase.table("clients").select("user_id").execute()
        return list({r["user_id"] for r in (res.data or []) if r.get("user_id")})
    except Exception as e:
        print(f"[users error] {e}")
        return []


def fetch_user_clients_with_holdings(supabase, user_id):
    try:
        clients_res = supabase.table("clients").select("*").eq("user_id", user_id).execute()
        clients = clients_res.data or []
        for c in clients:
            h_res = supabase.table("holdings").select("*").eq("client_id", c["id"]).execute()
            c["holdings"] = h_res.data or []
        return clients
    except Exception as e:
        print(f"  [user clients error] {e}")
        return []


def fetch_prices(supabase):
    try:
        res = supabase.table("prices").select("ticker, price").execute()
        return {p["ticker"]: float(p["price"]) for p in (res.data or []) if p.get("price")}
    except Exception:
        return {}


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3].strip()
        elif "```" in text:
            text = text[:text.rfind("```")].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except Exception as e:
        print(f"  parse error: {e}")
        print(f"  first 300: {text[:300]}")
        return None


def is_extreme_event(anthropic_client, news_item):
    """PRIMO FILTRO: l'AI giudica se la news è un evento veramente estremo.
    Threshold MOLTO alto: di default ritorna False."""

    title = news_item.get("title_it") or ""
    summary = news_item.get("summary_it") or ""
    impact = news_item.get("impact_it") or ""

    prompt = f"""Sei un analista finanziario senior. Devi giudicare se la seguente news rappresenta un EVENTO ECCEZIONALMENTE CRITICO che richiede un'allerta IMMEDIATA ai consulenti finanziari.

═══════════════════════════════════
NEWS DA VALUTARE
═══════════════════════════════════
TITOLO: {title}

SUMMARY: {summary}

IMPACT: {impact}

═══════════════════════════════════
CRITERIO DI VALUTAZIONE
═══════════════════════════════════
Un evento è "ECCEZIONALMENTE CRITICO" SOLO se è qualcosa che potrebbe muovere i mercati GLOBALI di oltre il 3-5% in poche ore, come:

✓ Conflitto militare aperto (es. Cina invade Taiwan, escalation nucleare, attentato a leader G7)
✓ Default sistemico di una grande economia o banca too-big-to-fail
✓ Crash di mercato in corso (>5% in un giorno su S&P/MSCI World)
✓ Attentato/morte di un capo di stato di paese importante
✓ Crisi finanziaria sistemica (es. Lehman 2008, crisi euro 2011)
✓ Blocco fisico di infrastruttura critica (es. chiusura stretto di Hormuz, attacco a TSMC)
✓ Emergenza pandemica globale (lockdown OMS)
✓ Sanzioni economiche estreme tra grandi blocchi (es. USA-Cina hard decoupling)

NON sono eventi critici:
✗ Earnings sotto attese di una big tech
✗ Movimenti azionari quotidiani anche larghi
✗ Decisioni delle banche centrali in linea con aspettative
✗ Eventi politici domestici (elezioni, dimissioni governi normali)
✗ Tensioni geopolitiche ricorrenti (sanzioni minori, dichiarazioni)
✗ News negative settoriali normali

SE NON SEI SICURO AL 90%, RISPONDI "NO". Gli alert devono essere RARI.

═══════════════════════════════════
RISPOSTA (SOLO JSON)
═══════════════════════════════════
{{
  "is_extreme": true | false,
  "severity": "HIGH" | "MED",
  "rationale": "1-2 frasi sul perché è/non è un evento estremo"
}}"""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_json(response.content[0].text.strip())
    except Exception as e:
        print(f"  [extreme check error] {e}")
        return None


def analyze_alert_for_user(anthropic_client, news_item, clients, prices):
    """SECONDO STEP: per ogni utente con clienti reali, l'AI compone l'alert completo
    con azioni specifiche per cliente."""

    # Prepara il portafoglio aggregato del consulente
    clients_summary = []
    for c in clients:
        holdings_list = []
        total_value = 0
        for h in (c.get("holdings") or []):
            ticker = h.get("ticker")
            qty = float(h.get("quantity") or 0)
            avg = float(h.get("avg_price") or 0)
            current = prices.get(ticker, avg)
            value = qty * current
            total_value += value
            holdings_list.append({
                "ticker": ticker,
                "name": h.get("name") or ticker,
                "value": value,
                "asset_type": h.get("asset_type") or "Equity",
            })
        if total_value < 100:
            continue
        clients_summary.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "profile": c.get("risk_profile") or "moderato",
            "objective": c.get("objective") or "crescita",
            "age": c.get("age"),
            "total_value": total_value,
            "holdings": holdings_list,
        })

    if not clients_summary:
        return None

    clients_str = json.dumps(clients_summary, ensure_ascii=False, indent=2)

    prompt = f"""Sei un risk analyst senior. È appena successo un EVENTO ECCEZIONALMENTE CRITICO. Devi generare un'ALLERTA OPERATIVA per il consulente finanziario, con AZIONI SPECIFICHE per ogni suo cliente.

═══════════════════════════════════
EVENTO CRITICO
═══════════════════════════════════
TITOLO: {news_item.get('title_it')}

SUMMARY: {news_item.get('summary_it')}

IMPACT: {news_item.get('impact_it')}

STRATEGIA: {news_item.get('strategy_it') or '—'}

SOURCE: {news_item.get('source', '—')}

═══════════════════════════════════
PORTAFOGLI DEI CLIENTI
═══════════════════════════════════
{clients_str}

═══════════════════════════════════
ISTRUZIONI
═══════════════════════════════════
Genera UN solo alert operativo che:

1. SUMMARY: spiegazione operativa di cosa è successo, perché è grave, cosa probabilmente succederà nelle prossime 24-72h
2. SETTORI COLPITI: lista ID settori (ai, auto, defense, gold, energy, ecc.)
3. SETTORI BENEFICIARI: lista settori che potrebbero salire
4. ASSET TRIGGERATI (negativi/positivi): ticker che ti aspetti calino/salgano
5. AZIONI BOOK-LEVEL: 3-5 azioni generiche da considerare su tutto il book
6. AZIONI CLIENTE PER CLIENTE: per OGNI cliente analizza la sua specifica esposizione e proponi azioni operative numeriche, ognuna con tipo (vendere/comprare/monitorare/coprire), descrizione, importo EUR stimato, e ragionamento.

Cita per nome asset reali dei clienti. Le azioni devono essere CONCRETE (numeri).

NIENTE jargon vuoto. Lessico semplice. Frasi corte.

═══════════════════════════════════
FORMATO OUTPUT (SOLO JSON)
═══════════════════════════════════
{{
  "title": "Titolo conciso dell'allerta (max 90 caratteri)",
  "summary": "3-5 frasi spiegazione di cosa succede e perché è critico",
  "sources": ["Reuters", "Bloomberg"],
  "affected_sectors": ["ai", "auto", "tech"],
  "beneficiary_sectors": ["defense", "gold", "energy"],
  "triggered_assets_negative": ["TSM", "NVDA", "ASML"],
  "triggered_assets_positive": ["LMT", "GLD.MI", "DFEN.MI"],
  "book_impact": {{
    "exposure_at_risk_eur": 420000,
    "exposure_at_risk_pct": -3.2,
    "worst_case_eur": -680000,
    "best_case_after_actions_eur": -85000
  }},
  "book_level_actions": [
    "Ridurre esposizione semiconduttori taiwanesi 50-70% nelle prossime 48h",
    "Aumentare difesa europea al 4-6% per chi è sotto"
  ],
  "client_actions": [
    {{
      "client_id": "uuid-del-cliente",
      "client_name": "Nome Cognome",
      "severity": "HIGH",
      "current_exposure": "Tech 38% (TSM 6%, NVDA 12%, ...)",
      "estimated_loss_eur": -28500,
      "estimated_loss_pct": -5.6,
      "actions": [
        {{
          "type": "vendere",
          "text": "Ridurre TSM dal 6% al 2% del portafoglio",
          "amount_eur": -20400,
          "reason": "Esposizione diretta a Taiwan, alto rischio immediato"
        }},
        {{
          "type": "comprare",
          "text": "Aggiungere oro fisico (GLD.MI) al 5%",
          "amount_eur": 25500,
          "reason": "Hedge geopolitico classico"
        }}
      ],
      "post_action_loss_eur": -6800,
      "notes": "Profilo growth con esposizione tech elevata. Le azioni preservano la tesi long su qualità tech ma riducono il rischio idiosincratico."
    }}
  ]
}}"""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=4500,
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_json(response.content[0].text.strip())
    except Exception as e:
        print(f"  [analysis error] {e}")
        return None


def archive_old_alerts(supabase):
    """Archivia automaticamente alert vecchi"""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ARCHIVE_AFTER_HOURS)).isoformat()
    try:
        supabase.table("live_alerts") \
            .update({"status": "archived"}) \
            .eq("status", "active") \
            .lt("triggered_at", cutoff) \
            .execute()
    except Exception as e:
        print(f"[archive error] {e}")


def save_alert(supabase, user_id, news_item, analysis, severity):
    row = {
        "user_id": user_id,
        "status": "active",
        "severity": severity,
        "trigger_news_id": news_item.get("id"),
        "title": analysis.get("title") or news_item.get("title_it"),
        "summary": analysis.get("summary") or news_item.get("summary_it"),
        "sources": analysis.get("sources") or [news_item.get("source", "—")],
        "affected_sectors": analysis.get("affected_sectors") or [],
        "beneficiary_sectors": analysis.get("beneficiary_sectors") or [],
        "triggered_assets_negative": analysis.get("triggered_assets_negative") or [],
        "triggered_assets_positive": analysis.get("triggered_assets_positive") or [],
        "book_impact": analysis.get("book_impact") or {},
        "book_level_actions": analysis.get("book_level_actions") or [],
        "client_actions": analysis.get("client_actions") or [],
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("live_alerts").insert(row).execute()
        return True
    except Exception as e:
        print(f"  [save error] {e}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("[alerts] Avvio scansione eventi critici...")

    # 1. Auto-archive alert vecchi
    archive_old_alerts(supabase)

    # 2. Fetch news HIGH recenti
    news_list = fetch_recent_high_news(supabase)
    print(f"[alerts] {len(news_list)} news HIGH negli ultimi {NEWS_WINDOW_HOURS}h")

    if not news_list:
        print("[alerts] Nessuna news rilevante. Termino.")
        return

    # 3. Filtra fuori news già trasformate in alert
    existing_ids = fetch_existing_alert_news_ids(supabase)
    candidates = [n for n in news_list if n.get("id") not in existing_ids]
    print(f"[alerts] {len(candidates)} news candidate dopo deduplica")

    if not candidates:
        print("[alerts] Tutte le news HIGH recenti hanno già un alert. Termino.")
        return

    # 4. PRIMO FILTRO AI: trova le news estreme
    extreme_news = []
    for n in candidates[:10]:  # Limito a 10 per non sprecare API
        title_short = (n.get("title_it") or "")[:80]
        print(f"  → Valuto: {title_short}...")
        check = is_extreme_event(anthropic_client, n)
        if check and check.get("is_extreme"):
            severity = check.get("severity") or "HIGH"
            print(f"    ⚠️  EVENTO ESTREMO ({severity}): {check.get('rationale', '')[:120]}")
            extreme_news.append((n, severity))
        else:
            rationale = (check or {}).get("rationale", "—")[:80]
            print(f"    OK (non critico): {rationale}")

    if not extreme_news:
        print("[alerts] Nessun evento veramente estremo. Termino.")
        return

    # 5. SECONDO STEP: per ogni utente, genera l'alert dettagliato
    user_ids = fetch_all_users_with_clients(supabase)
    print(f"[alerts] {len(user_ids)} utenti da processare per {len(extreme_news)} alert(s)")

    prices = fetch_prices(supabase)
    success = 0
    total = 0

    for news_item, severity in extreme_news:
        for user_id in user_ids:
            total += 1
            clients = fetch_user_clients_with_holdings(supabase, user_id)
            if not clients:
                continue
            print(f"  → Analizzo {news_item.get('title_it', '')[:60]} per user {user_id[:8]}...")
            analysis = analyze_alert_for_user(anthropic_client, news_item, clients, prices)
            if not analysis:
                continue
            if save_alert(supabase, user_id, news_item, analysis, severity):
                success += 1
                print(f"    ✅ Alert salvato")

    print(f"\n[alerts] Completato: {success}/{total} alert generati.")


if __name__ == "__main__":
    main()
