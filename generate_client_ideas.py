"""
generate_client_ideas.py
Genera idee personalizzate per OGNI cliente nel DB, una volta a settimana (lunedì 7:00 IT).

Per ogni cliente:
- Carica holdings, allocazioni, profilo, obiettivo, liquidità
- Carica news recenti (7 giorni)
- Carica scenari di stress test attivi
- Carica opportunities del mercato
- Chiama Claude Sonnet 4.5 per generare 3-6 idee strutturate, facendo un CROSS-CHECK TOTALE:
  profilo di rischio + obiettivo + orizzonte + allerte live + news macro che toccano gli asset del cliente
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
            "sector": h.get("sector") or "",
            "geo": h.get("geo") or "",
        })
    # Calcolo peso
    for e in enriched:
        e["weight_pct"] = (e["value"] / total_value * 100) if total_value > 0 else 0
    return enriched, total_value


def filter_relevant_news(news, holdings):
    """Trova le news che toccano direttamente i ticker o i settori del cliente.
    Match esatto sul ticker (CSV) e match sul settore nel testo."""
    if not news or not holdings:
        return []
    client_tickers = {h["ticker"].upper() for h in holdings if h.get("ticker")}
    client_sectors = {(h.get("sector") or "").lower() for h in holdings if h.get("sector")}
    client_sectors.discard("")

    relevant = []
    for n in news:
        # Match ticker: tickers è CSV "AAPL,TSLA"
        news_tickers = {t.strip().upper() for t in (n.get("tickers") or "").split(",") if t.strip()}
        ticker_hit = bool(news_tickers & client_tickers)
        # Match settore nel testo
        text = f"{n.get('title_it', '')} {n.get('summary_it', '')} {n.get('impact_it', '')}".lower()
        sector_hit = any(sec in text for sec in client_sectors if len(sec) > 3)
        if ticker_hit or sector_hit:
            matched = list(news_tickers & client_tickers)
            relevant.append({**n, "_matched_tickers": matched, "_match_type": "ticker" if ticker_hit else "sector"})
    return relevant


def build_prompt(client, holdings_enriched, total_value, cash_avail, news, opportunities,
                 relevant_news):
    """Costruisce il prompt per Claude Sonnet con CROSS-CHECK su profilo + obiettivo + news reali.
    NB: gli scenari di stress test NON sono inclusi: sono eventi ipotetici, non fatti."""
    profile_map = {"conservativo": "prudente", "moderato": "moderato", "aggressivo": "aggressivo"}
    profile = profile_map.get(client.get("risk_profile"), "moderato")
    objective = client.get("objective", "crescita")

    holdings_str = "\n".join([
        f"  - {h['ticker']} ({h['name']}): {h['quantity']:.2f} unità, valore €{h['value']:.0f}, peso {h['weight_pct']:.1f}%, settore {h.get('sector') or 'n/d'}, P&L {h['pnl_pct']:+.1f}% ({h['pnl']:+.0f}€)"
        for h in holdings_enriched
    ]) if holdings_enriched else "  (nessuna posizione)"

    news_str = "\n".join([
        f"  - [{n.get('category', 'gen')}] {n.get('title_it', '')[:120]} | {(n.get('impact_it') or '')[:120]}"
        for n in news[:10]
    ]) if news else "  (nessuna news recente)"

    opportunities_str = "\n".join([
        f"  - [{o.get('category')}] {o.get('ticker')}: {(o.get('title') or '')[:100]} | conviction {o.get('conviction')}/100 | orizzonte {o.get('target_timing', 'n/d')}"
        for o in opportunities[:10]
    ]) if opportunities else "  (nessuna opportunità attiva)"

    # ── News REALI che toccano DIRETTAMENTE questo cliente ──
    relevant_news_str = "\n".join([
        f"  - [{n.get('sentiment', 'neutro')}] {n.get('title_it', '')[:130]}"
        f"{(' | tocca: ' + ', '.join(n['_matched_tickers'])) if n.get('_matched_tickers') else ' | tocca un settore in portafoglio'}"
        f" | impatto: {(n.get('impact_it') or '')[:120]}"
        for n in relevant_news[:8]
    ]) if relevant_news else "  (nessuna news recente tocca direttamente gli asset di questo cliente)"

    prompt = f"""Sei un consulente finanziario senior italiano. Devi generare IDEE PERSONALIZZATE per UN cliente specifico, da rivedere col cliente alla prossima riunione.

═══════════════════════════════════
DATI DEL CLIENTE
═══════════════════════════════════
Nome: {client.get('name', '—')}
Età: {client.get('age', 'n/d')} anni
Profilo di rischio: {profile}   ← VINCOLO INVALICABILE
Obiettivo: {objective}   ← da perseguire restando dentro il profilo di rischio
Orizzonte temporale: {client.get('time_horizon', 'medio')}
Patrimonio totale investito: €{total_value:.0f}
Liquidità sul conto: €{cash_avail:.0f}
Persone a carico: {client.get('dependents', 0)}

═══════════════════════════════════
PORTAFOGLIO ATTUALE
═══════════════════════════════════
{holdings_str}

═══════════════════════════════════
⚠ NEWS REALI CHE TOCCANO DIRETTAMENTE QUESTO CLIENTE (priorità massima)
═══════════════════════════════════
{relevant_news_str}

═══════════════════════════════════
CONTESTO GENERALE DI MERCATO (news ultimi 7 giorni)
═══════════════════════════════════
{news_str}

═══════════════════════════════════
OPPORTUNITÀ DEL MERCATO (asset interessanti adesso)
═══════════════════════════════════
{opportunities_str}

═══════════════════════════════════
ISTRUZIONI — CROSS-CHECK
═══════════════════════════════════
Prima di generare le idee, fai un controllo incrociato di profilo, obiettivo e news reali:

1. COERENZA PROFILO + OBIETTIVO (la regola più importante):
   - Il profilo di rischio "{profile}" è un VINCOLO: non proporre MAI mosse che lo abbassano sotto questo livello.
     Esempio: a un profilo AGGRESSIVO non si consiglia di aumentare la liquidità o comprare titoli di stato sicuri.
   - Quando obiettivo e profilo sono in tensione (es. obiettivo "reddito" ma profilo "aggressivo"), usa strumenti che soddisfano ENTRAMBI:
     reddito+aggressivo → azioni high-dividend growth, REIT, obbligazioni high-yield, bond emergenti, dividend aristocrats, MLP/infrastrutture.
     reddito+prudente → bond investment grade, governativi, ETF a distribuzione, azioni difensive ad alto dividendo.
     crescita+aggressivo → equity tematico, growth, emergenti, small cap, tecnologia.
   - NON contraddirti tra le due categorie (non consigliare di comprare un asset in una e venderlo nell'altra).

2. NEWS MACRO REALI CHE TOCCANO IL CLIENTE:
   - Se una news negativa recente colpisce un settore/asset che il cliente DETIENE, SEGNALALO.
     Esempio: news terribile sul settore AI + il cliente ha un ETF AI → segnala che quell'asset è "momentaneamente sotto pressione per motivi macro" e valuta se alleggerire o attendere.
   - Se una news positiva favorisce un asset del cliente, puoi suggerire di mantenere o incrementare.
   - Basati SOLO su news reali fornite, mai su eventi ipotetici.

3. CONCRETEZZA:
   - Lessico SEMPLICE, frasi brevi (max 25 parole). Niente jargon: "drawdown" → "calo dal massimo"; "guidance" → "previsioni dell'azienda"; "buyback" → "riacquisto azioni proprie".
   - Numeri, percentuali, nomi di asset, motivazioni precise. Niente "diversifica di più" generico.
   - Considera la liquidità disponibile (se serve cash per comprare) e se l'asset esiste già in portafoglio.
   - Se non hai idee di qualità per una categoria, restituisci array vuoto. Meglio 2 idee buone che 6 mediocri.

Genera 3-6 idee TOTALI, divise in 2 categorie:
- CATEGORIA A — MODIFICHE AL PORTAFOGLIO: azioni sui titoli già detenuti (vendi parziale, incrementa, alleggerisci, mantieni).
- CATEGORIA B — NUOVE OPPORTUNITÀ: asset NON ancora in portafoglio, coerenti con profilo + obiettivo + diversificazione + liquidità.

Quando un'idea è influenzata da una news reale, indicalo nel campo "macro_alert".

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
      "reasoning": "Concentrazione singolo titolo > 20% rischiosa per profilo {profile}. Conti trimestrali in arrivo aggiungono volatilità.",
      "macro_alert": "Notizia recente sul settore AI ha aumentato la volatilità: momento favorevole per alleggerire, non per incrementare.",
      "priority": 1,
      "estimated_impact": "Riduce volatilità portafoglio del 15%. Cash liberato: €X.",
      "risks": "NVDA potrebbe continuare a salire post-Q. Stop loss su parte restante: -8%."
    }}
  ],
  "nuove_opportunita": [
    {{
      "ticker": "REET",
      "asset_name": "iShares Global REIT ETF",
      "action": "acquista_nuovo",
      "title": "Aggiungi 6% in REIT globali per generare reddito",
      "description": "Il portafoglio rende poca cedola ma il profilo è aggressivo: i REIT distribuiscono reddito interessante mantenendo un profilo di rischio elevato. Coerente con obiettivo reddito senza ridurre il rischio.",
      "reasoning": "Obiettivo {objective} + profilo {profile}: servono asset che diano reddito SENZA abbassare il rischio. I REIT centrano entrambi.",
      "macro_alert": "",
      "priority": 2,
      "estimated_position_pct": 6,
      "estimated_amount_eur": 6000,
      "covered_by_cash": true,
      "risks": "I REIT soffrono quando i tassi salgono. Valutare ingresso graduale."
    }}
  ]
}}

Genera ORA le idee. Cliente: {client.get('name', '—')}, profilo {profile}, obiettivo {objective}. Cross-check su profilo, obiettivo e news reali. Sii specifico, mai generico, mai contraddire il profilo di rischio."""

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


def generate_ideas_for_client(anthropic_client, supabase, client, prices, news, opportunities):
    print(f"  → Elaboro cliente: {client.get('name', client['id'])}")

    holdings_raw = fetch_client_holdings(supabase, client["id"])
    holdings, total_value = enrich_holdings_with_prices(holdings_raw, prices)
    cash_avail = float(client.get("liquidity_value") or 0)

    if total_value < 100 and cash_avail < 100:
        print(f"    Cliente vuoto: salto.")
        return None

    # Filtra le news REALI rilevanti PER QUESTO cliente (cross-check mirato)
    relevant_news = filter_relevant_news(news, holdings)
    if relevant_news:
        print(f"    {len(relevant_news)} news toccano gli asset del cliente")

    prompt = build_prompt(client, holdings, total_value, cash_avail, news, opportunities,
                          relevant_news)

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

    # Carica contesto di mercato UNA volta sola (uguale per tutti)
    news = fetch_recent_news(supabase, days=7, limit=20)
    opportunities = fetch_active_opportunities(supabase, limit=15)
    print(f"[client_ideas] Contesto: {len(news)} news, {len(opportunities)} opportunità")

    # Carica tutti i clienti
    clients = fetch_all_clients(supabase)
    print(f"[client_ideas] Trovati {len(clients)} clienti totali")

    if not clients:
        print("[client_ideas] Nessun cliente da elaborare.")
        return

    success = 0
    failed = 0
    for client in clients:
        ideas = generate_ideas_for_client(anthropic_client, supabase, client, prices, news, opportunities)
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
