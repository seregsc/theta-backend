"""
generate_scenario_impacts.py
Per OGNI scenario attivo + OGNI cliente, fa un'analisi AI personalizzata:
- Quali asset del cliente sono impattati DIRETTAMENTE o INDIRETTAMENTE dallo scenario?
- Per ogni asset, calcola il movimento atteso (% e €).
- Salva in client_scenario_impacts (UPSERT su client_id+scenario_id).

Gira 1 volta al giorno (dopo generate_scenarios). Costo: ~$0.05-0.10 per cliente per scenario.
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

# Quanti scenari recenti analizzare per ogni cliente (i più recenti)
SCENARIOS_PER_CLIENT = 3


def fetch_all_clients(supabase):
    try:
        res = supabase.table("clients").select("*").execute()
        return res.data or []
    except Exception as e:
        print(f"[clients error] {e}")
        return []


def fetch_client_holdings(supabase, client_id):
    try:
        res = supabase.table("holdings").select("*").eq("client_id", client_id).execute()
        return res.data or []
    except Exception as e:
        print(f"  [holdings error] {e}")
        return []


def fetch_prices(supabase):
    try:
        res = supabase.table("prices").select("*").execute()
        return {p["ticker"]: p for p in (res.data or [])}
    except Exception as e:
        return {}


def fetch_recent_scenarios(supabase, limit=SCENARIOS_PER_CLIENT):
    try:
        res = supabase.table("scenarios_live") \
            .select("*") \
            .order("generated_at", desc=True) \
            .limit(limit) \
            .execute()
        return res.data or []
    except Exception as e:
        print(f"[scenarios error] {e}")
        return []


def enrich_holdings(holdings, prices):
    """Calcola valore corrente per ogni holding."""
    enriched = []
    total_value = 0
    for h in holdings:
        ticker = h.get("ticker")
        qty = float(h.get("quantity") or 0)
        avg = float(h.get("avg_price") or 0)
        p_info = prices.get(ticker)
        current = float(p_info.get("price")) if p_info and p_info.get("price") else avg
        value = qty * current
        total_value += value
        enriched.append({
            "ticker": ticker,
            "name": h.get("name") or ticker,
            "asset_type": h.get("asset_type") or "Equity",
            "quantity": qty,
            "current_price": current,
            "value": value,
        })
    return enriched, total_value


def parse_response(text):
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
        print(f"  parse JSON error: {e}")
        print(f"  first 300: {text[:300]}")
        return None


def analyze_impact(anthropic_client, client, scenario, holdings_enriched, total_value):
    """Chiede a Claude un'analisi profonda: per ogni holding del cliente, è impattato dallo scenario?"""

    holdings_str = "\n".join([
        f"  - {h['ticker']} ({h['name']}): valore €{h['value']:.0f}, tipo {h['asset_type']}"
        for h in holdings_enriched
    ]) if holdings_enriched else "  (nessuna posizione)"

    winners_str = "\n".join([
        f"  - {w.get('ticker')} ({w.get('name', '—')}): {w.get('expected_move', '—')} | {w.get('why', '')[:120]}"
        for w in (scenario.get("winners") or [])
    ])
    losers_str = "\n".join([
        f"  - {l.get('ticker')} ({l.get('name', '—')}): {l.get('expected_move', '—')} | {l.get('why', '')[:120]}"
        for l in (scenario.get("losers") or [])
    ])
    sector_impact = scenario.get("sector_impact") or {}
    sectors_str = "\n".join([
        f"  - Settore '{k}': {v.get('expected_move', '—')} | {v.get('why', '')[:120]}"
        for k, v in sector_impact.items()
    ]) if sector_impact else "  (nessun impatto settoriale specificato)"

    prompt = f"""Sei un risk analyst senior italiano. Devi analizzare un PORTAFOGLIO SPECIFICO contro uno SCENARIO DI STRESS TEST e identificare ogni asset impattato.

═══════════════════════════════════
SCENARIO DI STRESS TEST
═══════════════════════════════════
TITOLO: {scenario.get('title', '—')}
DESCRIZIONE: {scenario.get('description', '—')}

WINNERS (asset che salgono):
{winners_str if winners_str else "  (nessuno)"}

LOSERS (asset che scendono):
{losers_str if losers_str else "  (nessuno)"}

IMPATTI PER SETTORE:
{sectors_str}

═══════════════════════════════════
PORTAFOGLIO DEL CLIENTE
═══════════════════════════════════
Cliente: {client.get('name', '—')}
Valore totale: €{total_value:.0f}

ASSET DETENUTI:
{holdings_str}

═══════════════════════════════════
ISTRUZIONI CRITICHE
═══════════════════════════════════
Per OGNI asset del portafoglio, decidi se è IMPATTATO da questo scenario, e come.

REGOLE:
1. IMPATTO DIRETTO: l'asset è esplicitamente nella lista winners/losers (match esatto del ticker).
2. IMPATTO INDIRETTO: l'asset NON è in winners/losers ma è SOSTANZIALMENTE EQUIVALENTE a uno di questi, oppure è strettamente esposto al settore impattato. Esempi reali:
   - Scenario menziona "EEM" (iShares EM ETF), cliente ha "XMME.MI" (Xtrackers MSCI EM): SONO LO STESSO INDICE, impatto pari (100%)
   - Scenario menziona "QQQ" (Nasdaq), cliente ha "EQQQ.MI" (Invesco QQQ UCITS): SONO LO STESSO INDICE, impatto pari (100%)
   - Scenario impatta settore "ai", cliente ha "AI4U.MI" (Nvidia-tracker AI thematic ETF): impatto al 70-80% del movimento settoriale (ETF tematico diversificato)
   - Scenario impatta settore "semis", cliente ha "TSM": impatto al 100% (è puramente quel settore)
   - Scenario impatta "gold", cliente ha "GLD.MI" o "PHAU.L": stesso oro, impatto 90-100%
3. NON IMPATTATO: l'asset non è correlato in modo significativo (es. scenario su crisi idrica, cliente ha NVDA → NVDA non è impattata direttamente né per settore; non includere).
4. Sii CONSERVATIVO: solo asset realmente impattati. Meglio omettere che inventare.
5. Per ogni asset impattato, fornisci una PERCENTUALE precisa (es. -22.5%) basata sull'expected_move dello scenario + il peso (1.0 per match esatto, 0.7-0.9 per ETF equivalenti, 0.5-0.7 per esposizione parziale).

═══════════════════════════════════
FORMATO OUTPUT (SOLO JSON)
═══════════════════════════════════
Rispondi SOLO con JSON puro, nessun preambolo:

{{
  "affected_holdings": [
    {{
      "ticker": "XMME.MI",
      "name": "Xtrackers MSCI EM UCITS",
      "value": 12500,
      "impact_type": "indirect",
      "equivalent_to": "EEM",
      "expected_move_pct": -28.0,
      "expected_move_eur": -3500,
      "type": "loser",
      "reasoning": "Xtrackers MSCI Emerging Markets UCITS replica lo stesso indice MSCI EM dell'iShares EEM citato nello scenario. La penalizzazione attesa è praticamente identica."
    }},
    {{
      "ticker": "NVDA",
      "name": "NVIDIA",
      "value": 8000,
      "impact_type": "direct",
      "expected_move_pct": -32.0,
      "expected_move_eur": -2560,
      "type": "loser",
      "reasoning": "NVDA è esplicitamente nello scenario come loser."
    }}
  ]
}}

Se nessun asset è impattato, restituisci:
{{
  "affected_holdings": []
}}"""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return parse_response(text)
    except Exception as e:
        print(f"  AI error: {e}")
        return None


def save_impact(supabase, user_id, client_id, scenario_id, total_value, analysis):
    """UPSERT su client_id+scenario_id."""
    affected = analysis.get("affected_holdings") or []
    direct_count = sum(1 for h in affected if h.get("impact_type") == "direct")
    indirect_count = sum(1 for h in affected if h.get("impact_type") == "indirect")
    total_impact = sum(float(h.get("expected_move_eur") or 0) for h in affected)
    impact_pct = (total_impact / total_value * 100) if total_value > 0 else 0

    row = {
        "user_id": user_id,
        "client_id": client_id,
        "scenario_id": scenario_id,
        "impact_pct": impact_pct,
        "total_impact": total_impact,
        "total_value": total_value,
        "affected_holdings": affected,
        "direct_count": direct_count,
        "indirect_count": indirect_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("client_scenario_impacts").upsert(row, on_conflict="client_id,scenario_id").execute()
        return True
    except Exception as e:
        print(f"    save error: {e}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("[scenario_impacts] Avvio analisi AI cliente-scenario...")

    prices = fetch_prices(supabase)
    print(f"[scenario_impacts] {len(prices)} prezzi caricati")

    clients = fetch_all_clients(supabase)
    print(f"[scenario_impacts] {len(clients)} clienti trovati")

    scenarios = fetch_recent_scenarios(supabase, limit=SCENARIOS_PER_CLIENT)
    print(f"[scenario_impacts] {len(scenarios)} scenari recenti da analizzare")

    if not clients or not scenarios:
        print("[scenario_impacts] Nulla da elaborare.")
        return

    total_runs = 0
    success = 0
    failed = 0

    for client in clients:
        client_id = client.get("id")
        user_id = client.get("user_id")
        if not client_id or not user_id:
            continue

        holdings_raw = fetch_client_holdings(supabase, client_id)
        if not holdings_raw:
            print(f"  [{client.get('name')}] portafoglio vuoto, salto")
            continue

        holdings, total_value = enrich_holdings(holdings_raw, prices)
        if total_value < 100:
            print(f"  [{client.get('name')}] valore troppo basso, salto")
            continue

        for scenario in scenarios:
            scenario_id = scenario.get("id")
            if not scenario_id:
                continue
            total_runs += 1
            print(f"  → {client.get('name')} × {scenario.get('title', '')[:60]}")

            analysis = analyze_impact(anthropic_client, client, scenario, holdings, total_value)
            if not analysis:
                failed += 1
                continue

            if save_impact(supabase, user_id, client_id, scenario_id, total_value, analysis):
                success += 1
                n = len(analysis.get("affected_holdings") or [])
                print(f"    OK: {n} asset impattati")
            else:
                failed += 1

    print(f"\n[scenario_impacts] Completato: {success}/{total_runs} successi, {failed} fallimenti.")


if __name__ == "__main__":
    main()
