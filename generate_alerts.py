"""
generate_alerts.py
Rileva eventi estremi nelle news recenti e genera alert critici
per ogni consulente, analizzando i portafogli reali dei clienti.

Da eseguire ogni 3 ore via GitHub Actions.
È un sistema che produce alert RARI: nel 95% delle esecuzioni non trova nulla
di abbastanza critico e termina senza generare alert.

OTTIMIZZAZIONE COSTI:
- PRIMO FILTRO (is_extreme_event): usa Haiku, modello veloce ed economico (~$0.001/chiamata)
  per giudicare se un evento è critico. È un sì/no, non serve modello potente.
- SECONDO STEP (analyze_alert_for_user): usa Sonnet, modello potente per generare
  l'alert completo con azioni numeriche per cliente. Costoso ma scatta raramente.
"""

import os
import json
from datetime import datetime, timedelta, timezone
from supabase import create_client
import anthropic

print("[boot] generate_alerts.py avvio", flush=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

print(f"[boot] env: SUPABASE_URL={'OK' if SUPABASE_URL else 'MANCANTE'}, "
      f"SUPABASE_KEY={'OK' if SUPABASE_KEY else 'MANCANTE'}, "
      f"ANTHROPIC_API_KEY={'OK' if ANTHROPIC_API_KEY else 'MANCANTE'}", flush=True)

# Modelli differenziati per ottimizzare costi
MODEL_FILTER = "claude-haiku-4-5-20251001"   # primo filtro: economico e veloce
MODEL_ANALYSIS = "claude-sonnet-4-5"          # analisi dettagliata: qualità superiore

# Auto-archive alert più vecchi di N ore
ARCHIVE_AFTER_HOURS = 72

# Finestra news da valutare (allargata da 12 a 36h perché ora giriamo ogni 3h invece di 1h)
NEWS_WINDOW_HOURS = 36

# Limite news valutate per ogni esecuzione (per controllo costi)
MAX_NEWS_TO_EVALUATE = 8


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
        print(f"[news error] {e}", flush=True)
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
        print(f"[users error] {e}", flush=True)
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
        print(f"  [user clients error] {e}", flush=True)
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
        print(f"  parse error: {e}", flush=True)
        print(f"  first 300: {text[:300]}", flush=True)
        return None


def is_extreme_event(anthropic_client, news_item):
    """PRIMO FILTRO con Haiku: giudica se la news è veramente estrema.
    Output minimal (~200 token), pochissimi cent."""

    title = news_item.get("title_it") or ""
    summary = news_item.get("summary_it") or ""
    impact = news_item.get("impact_it") or ""

    prompt = f"""Sei un risk analyst. Giudica se questa news è un EVENTO ECCEZIONALMENTE CRITICO che richiede allerta IMMEDIATA ai consulenti finanziari.

NEWS:
TITOLO: {title}
SUMMARY: {summary}
IMPACT: {impact}

CRITERIO:
"Eccezionalmente critico" = qualcosa che POTREBBE muovere i mercati globali del 3-5%+ in poche ore.

SÌ se è uno di:
✓ Conflitto militare aperto (es. Cina invade Taiwan, escalation nucleare)
✓ Default sistemico grande economia o banca too-big-to-fail
✓ Crash di mercato in corso (>5% in un giorno su S&P/MSCI World)
✓ Attentato/morte capo di stato G7 o paese chiave
✓ Crisi finanziaria sistemica (tipo Lehman 2008)
✓ Blocco infrastruttura critica (Hormuz, TSMC, oleodotti chiave)
✓ Emergenza pandemica globale (lockdown OMS)
✓ Sanzioni economiche estreme (USA-Cina hard decoupling)

NO se è:
✗ Earnings sotto attese di big tech
✗ Movimenti azionari quotidiani anche larghi
✗ Decisioni banche centrali in linea
✗ Eventi politici domestici normali
✗ Tensioni geopolitiche ricorrenti
✗ News negative settoriali normali

REGOLA D'ORO: SE NON SEI SICURO AL 90%, RISPONDI "NO". Gli alert devono essere RARI.

OUTPUT (SOLO JSON):
{{
  "is_extreme": true | false,
  "severity": "HIGH" | "MED",
  "rationale": "1 frase breve sul perché"
}}"""

    try:
        response = anthropic_client.messages.create(
            model=MODEL_FILTER,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_json(response.content[0].text.strip())
    except Exception as e:
        print(f"  [filter error] {e}", flush=True)
        return None


def analyze_alert_for_user(anthropic_client, news_item, clients, prices):
    """SECONDO STEP con Sonnet: alert completo con azioni per cliente.
    Costoso ma scatta raramente (solo quando filter dice SÌ)."""

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

    prompt = f"""Sei un risk analyst senior. È appena successo un EVENTO ECCEZIONALMENTE CRITICO. Genera un'ALLERTA OPERATIVA per il consulente con AZIONI SPECIFICHE per ogni cliente.

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
Genera UN solo alert operativo:

1. SUMMARY: spiegazione APPROFONDITA e in linguaggio semplice di cosa è successo, perché è grave, e cosa succederà nelle prossime 24-72h. 4-6 frasi.
2. POSSIBILI SVILUPPI: 3-5 scenari concreti di cosa potrebbe accadere ORA come conseguenza (es. "Taiwan chiede aiuto militare agli USA", "sanzioni reciproche", "intervento banche centrali"). Per ciascuno: titolo breve, descrizione di 1 frase, e probabilità (Alta/Media/Bassa).
3. SETTORI COLPITI: lista ID (ai, auto, defense, gold, energy, ecc.)
4. SETTORI BENEFICIARI: lista settori che potrebbero salire
5. ASSET TRIGGERATI (negativi/positivi): ticker che caleranno/saliranno
6. AZIONI BOOK-LEVEL: 3-5 azioni generiche
7. AZIONI PER CLIENTE: per OGNI cliente analizza esposizione e proponi azioni operative numeriche (tipo vendere/comprare/coprire, descrizione, importo EUR, ragionamento). Usa linguaggio semplice e comprensibile.

Cita asset REALI dei clienti per nome. Azioni CONCRETE con numeri. Lessico semplice.

═══════════════════════════════════
FORMATO OUTPUT (SOLO JSON)
═══════════════════════════════════
{{
  "title": "Titolo conciso max 90 char",
  "summary": "4-6 frasi sul cosa, perché è grave e cosa succederà",
  "possible_next_events": [
    {{
      "title": "Cosa potrebbe succedere ora (breve)",
      "description": "1 frase di spiegazione",
      "likelihood": "Alta | Media | Bassa"
    }}
  ],
  "sources": ["Reuters", "Bloomberg"],
  "affected_sectors": ["ai", "auto"],
  "beneficiary_sectors": ["defense", "gold"],
  "triggered_assets_negative": ["TSM", "NVDA"],
  "triggered_assets_positive": ["LMT", "GLD.MI"],
  "book_impact": {{
    "exposure_at_risk_eur": 420000,
    "exposure_at_risk_pct": -3.2,
    "worst_case_eur": -680000,
    "best_case_after_actions_eur": -85000
  }},
  "book_level_actions": [
    "Ridurre semiconduttori taiwanesi 50-70% nelle prossime 48h",
    "Aumentare difesa europea al 4-6% per chi è sotto"
  ],
  "client_actions": [
    {{
      "client_id": "uuid",
      "client_name": "Nome Cognome",
      "severity": "HIGH",
      "current_exposure": "Tech 38% (TSM 6%, NVDA 12%)",
      "estimated_loss_eur": -28500,
      "estimated_loss_pct": -5.6,
      "actions": [
        {{
          "type": "vendere",
          "text": "Ridurre TSM dal 6% al 2%",
          "amount_eur": -20400,
          "reason": "Esposizione diretta a Taiwan"
        }}
      ],
      "post_action_loss_eur": -6800,
      "notes": "Profilo growth, le azioni preservano la tesi long ma riducono rischio idiosincratico"
    }}
  ]
}}"""

    try:
        response = anthropic_client.messages.create(
            model=MODEL_ANALYSIS,
            max_tokens=4500,
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_json(response.content[0].text.strip())
    except Exception as e:
        print(f"  [analysis error] {e}", flush=True)
        return None


def archive_old_alerts(supabase):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ARCHIVE_AFTER_HOURS)).isoformat()
    try:
        supabase.table("live_alerts") \
            .update({"status": "archived"}) \
            .eq("status", "active") \
            .lt("triggered_at", cutoff) \
            .execute()
    except Exception as e:
        print(f"[archive error] {e}", flush=True)


def save_alert(supabase, user_id, news_item, analysis, severity):
    row = {
        "user_id": user_id,
        "status": "active",
        "severity": severity,
        "trigger_news_id": news_item.get("id"),
        "title": analysis.get("title") or news_item.get("title_it"),
        "summary": analysis.get("summary") or news_item.get("summary_it"),
        "possible_next_events": analysis.get("possible_next_events") or [],
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
        print(f"  [save error] {e}", flush=True)
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print(f"[alerts] Avvio scansione eventi critici (filter={MODEL_FILTER}, analysis={MODEL_ANALYSIS})", flush=True)

    # 1. Auto-archive vecchi
    archive_old_alerts(supabase)

    # 2. Fetch news HIGH
    news_list = fetch_recent_high_news(supabase)
    print(f"[alerts] {len(news_list)} news HIGH negli ultimi {NEWS_WINDOW_HOURS}h", flush=True)

    if not news_list:
        print("[alerts] Nessuna news rilevante. Termino.", flush=True)
        return

    # 3. Dedup
    existing_ids = fetch_existing_alert_news_ids(supabase)
    candidates = [n for n in news_list if n.get("id") not in existing_ids]
    print(f"[alerts] {len(candidates)} news candidate dopo deduplica", flush=True)

    if not candidates:
        print("[alerts] Tutte già processate. Termino.", flush=True)
        return

    # 4. PRIMO FILTRO con Haiku (economico)
    extreme_news = []
    for n in candidates[:MAX_NEWS_TO_EVALUATE]:
        title_short = (n.get("title_it") or "")[:80]
        print(f"  → Valuto (Haiku): {title_short}...", flush=True)
        check = is_extreme_event(anthropic_client, n)
        if check and check.get("is_extreme"):
            severity = check.get("severity") or "HIGH"
            print(f"    ⚠️  EVENTO ESTREMO ({severity}): {check.get('rationale', '')[:120]}", flush=True)
            extreme_news.append((n, severity))
        else:
            rationale = (check or {}).get("rationale", "—")[:80]
            print(f"    OK (non critico): {rationale}", flush=True)

    if not extreme_news:
        print("[alerts] Nessun evento veramente estremo. Termino.", flush=True)
        return

    # 5. SECONDO STEP con Sonnet (qualità su evento confermato)
    user_ids = fetch_all_users_with_clients(supabase)
    print(f"[alerts] {len(user_ids)} utenti × {len(extreme_news)} alert(s) da generare", flush=True)

    prices = fetch_prices(supabase)
    success = 0
    total = 0

    for news_item, severity in extreme_news:
        for user_id in user_ids:
            total += 1
            clients = fetch_user_clients_with_holdings(supabase, user_id)
            if not clients:
                continue
            print(f"  → (Sonnet) {news_item.get('title_it', '')[:60]} per user {user_id[:8]}...", flush=True)
            analysis = analyze_alert_for_user(anthropic_client, news_item, clients, prices)
            if not analysis:
                continue
            if save_alert(supabase, user_id, news_item, analysis, severity):
                success += 1
                print(f"    ✅ Alert salvato", flush=True)

    print(f"\n[alerts] Completato: {success}/{total} alert generati.", flush=True)


if __name__ == "__main__":
    main()
