"""
generate_client_ideas.py
Genera idee personalizzate per OGNI cliente nel DB, una volta a settimana (lunedì 7:00 IT).

Per ogni cliente:
- Carica holdings, allocazioni, profilo, obiettivo, liquidità
- Carica news recenti (7 giorni)
- Carica scenari di stress test attivi
- Carica opportunities del mercato
- Chiama Claude Sonnet 4.5 per generare 3-6 idee strutturate
- Salva risultato in client_ai_ideas (UPSERT su user_id+client_id)

Costo stimato: ~$0.15-0.25 per cliente.
"""

import os
import json
from datetime import datetime, timezone, timedelta
from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"


def fetch_all_clients(supabase):
    """Carica tutti i clienti del sistema (tutti gli utenti)."""
    try:
        res = supabase.table("clients").select("*").execute()
        return res.data or []
    except Exception as e:
        print(f"[clients fetch error] {e}")
        return []


def fetch_client_holdings(supabase, client_id):
    try:
        res = supabase.table("holdings").select("*").eq("client_id", client_id).execute()
        return res.data or []
    except Exception as e:
        print(f"  [holdings fetch error] {e}")
        return []


def fetch_prices(supabase):
    """Carica tutti i prezzi correnti, mappa per ticker."""
    try:
        res = supabase.table("prices").select("*").execute()
        return {p["ticker"]: p for p in (res.data or [])}
    except Exception as e:
        print(f"[prices fetch error] {e}")
        return {}


def fetch_recent_news(supabase, days=7, limit=20):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        res = supabase.table("news") \
            .select("title_it, summary_it, impact_it, strategy_it, tickers, category, sentiment, published_at") \
            .gte("published_at", since) \
            .not_.is_("title_it", "null") \
            .order("published_at", desc=True) \
            .limit(limit) \
            .execute()
        return res.data or []
    except Exception as e:
        print(f"[news fetch error] {e}")
        return []


def fetch_active_scenarios(supabase, limit=5):
    try:
        res = supabase.table("scenarios_live") \
            .select("title, description, severity, probability, time_horizon, winners, losers, hedge_strategy") \
            .order("generated_at", desc=True) \
            .limit(limit) \
            .execute()
        return res.data or []
    except Exception as e:
        print(f"[scenarios fetch error] {e}")
        return []


def fetch_active_opportunities(supabase, limit=15):
    try:
        res = supabase.table("opportunities") \
            .select("category, ticker, current_price, currency, title, summary, reason, catalyst, risks, target_timing, score, conviction, risk, time_horizon, expected_move") \
            .eq("status", "active") \
            .order("score", desc=True) \
            .limit(limit) \
            .execute()
        return res.data or []
    except Exception as e:
        print(f"[opportunities fetch error] {e}")
        return []


def enrich_holdings_with_prices(holdings, prices):
    """Aggiunge current_price, value, pnl, weight a ogni holding."""
    enriched = []
    total_value = 0
    for h in holdings:
        ticker = h.get("ticker")
        qty = float(h.get("quantity") or 0)
        avg = float(h.get("avg_price") or 0)
        p_info = prices.get(ticker)
        current_price = float(p_info.get("price")) if p_info and p_info.get("price") else avg
        value = qty * current_price
        invested = qty * avg
        pnl = value - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0
        total_value += value
        enriched.append({
            "ticker": ticker,
            "name": h.get("name") or ticker,
            "quantity": qty,
            "avg_price": avg,
            "current_price": current_price,
            "value": value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "asset_type": h.get("asset_type") or "Equity",
        })
    # Calcolo peso
    for e in enriched:
        e["weight_pct"] = (e["value"] / total_value * 100) if total_value > 0 else 0
    return enriched, total_value


def build_prompt(client, holdings_enriched, total_value, cash_avail, news, scenarios, opportunities):
    """Costruisce il prompt per Claude Sonnet."""
    profile_map = {"conservativo": "prudente", "moderato": "moderato", "aggressivo": "aggressivo"}
    profile = profile_map.get(client.get("risk_profile"), "moderato")

    holdings_str = "\n".join([
        f"  - {h['ticker']} ({h['name']}): {h['quantity']:.2f} unità, valore €{h['value']:.0f}, peso {h['weight_pct']:.1f}%, P&L {h['pnl_pct']:+.1f}% ({h['pnl']:+.0f}€)"
        for h in holdings_enriched
    ]) if holdings_enriched else "  (nessuna posizione)"

    news_str = "\n".join([
        f"  - [{n.get('category', 'gen')}] {n.get('title_it', '')[:120]} | {(n.get('impact_it') or '')[:120]}"
        for n in news[:10]
    ]) if news else "  (nessuna news recente)"

    scenarios_str = "\n".join([
        f"  - {s.get('title', '')[:80]} (severità {s.get('severity')}, prob {s.get('probability')})"
        for s in scenarios[:3]
    ]) if scenarios else "  (nessuno scenario attivo)"

    opportunities_str = "\n".join([
        f"  - [{o.get('category')}] {o.get('ticker')}: {(o.get('title') or '')[:100]} | conviction {o.get('conviction')}/100 | orizzonte {o.get('target_timing', 'n/d')}"
        for o in opportunities[:10]
    ]) if opportunities else "  (nessuna opportunità attiva)"

    prompt = f"""Sei un consulente finanziario senior italiano. Devi generare IDEE PERSONALIZZATE per UN cliente specifico, da rivedere col cliente alla prossima riunione.

═══════════════════════════════════
DATI DEL CLIENTE
═══════════════════════════════════
Nome: {client.get('name', '—')}
Età: {client.get('age', 'n/d')} anni
Profilo di rischio: {profile}
Obiettivo: {client.get('objective', 'crescita')}
Orizzonte temporale: {client.get('time_horizon', 'medio')}
Patrimonio totale investito: €{total_value:.0f}
Liquidità sul conto: €{cash_avail:.0f}
Persone a carico: {client.get('dependents', 0)}

═══════════════════════════════════
PORTAFOGLIO ATTUALE
═══════════════════════════════════
{holdings_str}

═══════════════════════════════════
NEWS RECENTI DEL MERCATO (ultimi 7 giorni)
═══════════════════════════════════
{news_str}

═══════════════════════════════════
SCENARI DI STRESS TEST ATTIVI
═══════════════════════════════════
{scenarios_str}

═══════════════════════════════════
OPPORTUNITÀ DEL MERCATO (asset interessanti adesso)
═══════════════════════════════════
{opportunities_str}

═══════════════════════════════════
ISTRUZIONI
═══════════════════════════════════
Genera 3-6 idee TOTALI, divise in 2 categorie:

CATEGORIA A — MODIFICHE AL PORTAFOGLIO ATTUALE:
Azioni concrete sui titoli già detenuti dal cliente (vendi parziale, incrementa, alleggerisci, mantieni).
Solo se ha senso: non forzare un'idea se il portafoglio è ben bilanciato.

CATEGORIA B — NUOVE OPPORTUNITÀ:
Asset NON ancora in portafoglio del cliente che potrebbe valutare di aggiungere.
Devono essere coerenti con: profilo di rischio + obiettivo + diversificazione attuale + liquidità disponibile.
Usa preferibilmente asset dalla lista "OPPORTUNITÀ DEL MERCATO" se rilevanti, altrimenti suggerisci asset notori.

REGOLE IMPORTANTI:
1. Lessico SEMPLICE, frasi brevi (max 25 parole). Niente jargon: "drawdown" → "calo dal massimo"; "guidance" → "previsioni dell'azienda"; "buyback" → "riacquisto azioni proprie".
2. Sii CONCRETO e SPECIFICO: numeri, percentuali, nomi di asset, motivazioni precise.
3. NON suggerire idee generiche tipo "diversifica di più". Devono essere actionable: "vendi 30% di NVDA per ridurre concentrazione tech dal 45% al 32%".
4. Considera SEMPRE: la liquidità disponibile (se serve cash per comprare), e se l'asset esiste già in portafoglio.
5. Se non hai idee di qualità per una categoria, restituisci array vuoto. Meglio 2 idee buone che 6 mediocri.

═══════════════════════════════════
FORMATO OUTPUT
═══════════════════════════════════
Rispondi SOLO con JSON, nessun preambolo:

{{
  "modifiche": [
    {{
      "ticker": "NVDA",
      "asset_name": "NVIDIA",
      "action": "vendi_parziale",
      "title": "Vendi 30% di NVDA per bloccare il guadagno",
      "description": "NVDA è cresciuto del +45% e pesa ora il 22% del portafoglio. Vendere 30% blocca €X di profitto e riduce la concentrazione tech dal 22% al 15%. Mantiene comunque esposizione al tema AI.",
      "reasoning": "Concentrazione singolo titolo > 20% rischiosa per profilo {profile}. Conti trimestrali in arrivo (data: ...) aggiungono volatilità.",
      "priority": 1,
      "estimated_impact": "Riduce volatilità portafoglio del 15%. Cash liberato: €X.",
      "risks": "NVDA potrebbe continuare a salire post-Q. Stop loss su parte restante: -8%."
    }}
  ],
  "nuove_opportunita": [
    {{
      "ticker": "GLD",
      "asset_name": "SPDR Gold Shares",
      "action": "acquista_nuovo",
      "title": "Aggiungi 5% in oro come protezione",
      "description": "Lo scenario 'Iran/Hormuz' (severità Alta) avrebbe impatto -25% sul portafoglio attuale concentrato in tech. L'oro è storicamente decorrelato e sale nei momenti di stress geopolitico.",
      "reasoning": "Il portafoglio è esposto al 78% in equity USA tech. Mancanza di asset rifugio. GLD aggiunge protezione coerente con profilo {profile}.",
      "priority": 2,
      "estimated_position_pct": 5,
      "estimated_amount_eur": 5000,
      "covered_by_cash": true,
      "risks": "GLD non rende interessi. In scenari risk-on può sottoperformare."
    }}
  ]
}}

Genera ORA le idee, ricordando: il cliente è {client.get('name', '—')}, profilo {profile}. Sii specifico, non generico."""

    return prompt


def parse_response(text):
    """Estrae JSON dalla risposta."""
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
        data = json.loads(text)
        return data
    except json.JSONDecodeError as e:
        print(f"  errore parsing JSON: {e}")
        print(f"  primi 300 char: {text[:300]}")
        return None


def generate_ideas_for_client(anthropic_client, supabase, client, prices):
    print(f"  → Elaboro cliente: {client.get('name', client['id'])}")

    holdings_raw = fetch_client_holdings(supabase, client["id"])
    holdings, total_value = enrich_holdings_with_prices(holdings_raw, prices)
    cash_avail = float(client.get("liquidity_value") or 0)

    if total_value < 100 and cash_avail < 100:
        print(f"    Cliente vuoto: salto.")
        return None

    # Carica dati di contesto (uguali per tutti i clienti, potremmo ottimizzare cacheando)
    news = fetch_recent_news(supabase, days=7, limit=20)
    scenarios = fetch_active_scenarios(supabase, limit=5)
    opportunities = fetch_active_opportunities(supabase, limit=15)

    prompt = build_prompt(client, holdings, total_value, cash_avail, news, scenarios, opportunities)

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=3500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        ideas = parse_response(text)
        if not ideas:
            print(f"    Generazione fallita.")
            return None

        # Conteggio
        n_mod = len(ideas.get("modifiche", []))
        n_new = len(ideas.get("nuove_opportunita", []))
        print(f"    OK: {n_mod} modifiche, {n_new} nuove opportunità")

        return ideas
    except Exception as e:
        print(f"    Errore Claude: {e}")
        return None


def save_ideas(supabase, user_id, client_id, ideas, context_snapshot):
    """UPSERT in client_ai_ideas: una riga per cliente, sovrascritta ogni settimana."""
    row = {
        "user_id": user_id,
        "client_id": client_id,
        "ideas": ideas,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "context_snapshot": context_snapshot,
    }
    try:
        # Upsert su unique constraint (user_id, client_id)
        supabase.table("client_ai_ideas").upsert(row, on_conflict="user_id,client_id").execute()
        return True
    except Exception as e:
        print(f"    Errore salvataggio: {e}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("[client_ideas] Avvio generazione settimanale...")

    # Carica prezzi UNA volta sola (riusata per tutti i clienti)
    prices = fetch_prices(supabase)
    print(f"[client_ideas] Caricati {len(prices)} prezzi")

    # Carica tutti i clienti
    clients = fetch_all_clients(supabase)
    print(f"[client_ideas] Trovati {len(clients)} clienti totali")

    if not clients:
        print("[client_ideas] Nessun cliente da elaborare.")
        return

    success = 0
    failed = 0
    for client in clients:
        ideas = generate_ideas_for_client(anthropic_client, supabase, client, prices)
        if ideas:
            snapshot = {
                "generated_for_holdings_count": len(fetch_client_holdings(supabase, client["id"])),
                "model": MODEL,
            }
            if save_ideas(supabase, client["user_id"], client["id"], ideas, snapshot):
                success += 1
            else:
                failed += 1
        else:
            failed += 1

    print(f"\n[client_ideas] Completato: {success} successi, {failed} fallimenti.")


if __name__ == "__main__":
    main()
