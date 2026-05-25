"""
generate_scenario_impacts.py
Per OGNI scenario attivo + OGNI cliente, fa un'analisi AI personalizzata:
- Quali asset del cliente sono impattati DIRETTAMENTE o INDIRETTAMENTE dallo scenario?
- Storia narrativa "Cosa succede a [cliente]" su misura per quel cliente.
- Azioni suggerite specifiche.
- Salva in client_scenario_impacts (UPSERT su client_id+scenario_id).

OTTIMIZZAZIONE COSTI: usa Haiku per analisi.
Il task è strutturato (mappare ticker→impact con regole chiare) e Haiku ce la fa bene.
Risparmio ~65% rispetto a Sonnet.
"""

import os
import json
from datetime import datetime, timedelta, timezone
from supabase import create_client
import anthropic

print("[boot] generate_scenario_impacts.py avvio", flush=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

print(f"[boot] env: SUPABASE_URL={'OK' if SUPABASE_URL else 'MANCANTE'}, "
      f"SUPABASE_KEY={'OK' if SUPABASE_KEY else 'MANCANTE'}, "
      f"ANTHROPIC_API_KEY={'OK' if ANTHROPIC_API_KEY else 'MANCANTE'}", flush=True)

MODEL = "claude-haiku-4-5-20251001"
AI_TIMEOUT = 60

SCENARIOS_PER_CLIENT = 3


def fetch_all_clients(supabase):
    try:
        res = supabase.table("clients").select("*").execute()
        return res.data or []
    except Exception as e:
        print(f"[clients error] {e}", flush=True)
        return []


def fetch_client_holdings(supabase, client_id):
    try:
        res = supabase.table("holdings").select("*").eq("client_id", client_id).execute()
        return res.data or []
    except Exception as e:
        print(f"  [holdings error] {e}", flush=True)
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
        print(f"[scenarios error] {e}", flush=True)
        return []


def enrich_holdings(holdings, prices):
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
        print(f"  parse JSON error: {e}", flush=True)
        print(f"  first 300: {text[:300]}", flush=True)
        return None


def analyze_impact(anthropic_client, client, scenario, holdings_enriched, total_value):
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

    profile_map = {"conservativo": "prudente", "moderato": "moderato", "aggressivo": "aggressivo"}
    profile = profile_map.get(client.get("risk_profile"), "moderato")
    first_name = (client.get('name', '') or '').split(' ')[0] or "il cliente"

    prompt = f"""Sei un risk analyst senior italiano. Devi analizzare un PORTAFOGLIO SPECIFICO contro uno SCENARIO DI STRESS TEST e produrre:
1. La lista degli asset impattati (diretti + indiretti)
2. Una STORIA personalizzata su misura per questo cliente
3. Azioni suggerite specifiche al cliente

═══════════════════════════════════
SCENARIO
═══════════════════════════════════
TITOLO: {scenario.get('title', '—')}
DESCRIZIONE: {scenario.get('description', '—')}

WINNERS:
{winners_str if winners_str else "  (nessuno)"}

LOSERS:
{losers_str if losers_str else "  (nessuno)"}

IMPATTI PER SETTORE:
{sectors_str}

═══════════════════════════════════
CLIENTE
═══════════════════════════════════
Nome: {client.get('name', '—')}
Profilo di rischio: {profile}
Obiettivo: {client.get('objective', 'crescita')}
Orizzonte temporale: {client.get('time_horizon', 'medio')}
Età: {client.get('age', 'n/d')} anni
Valore portafoglio: €{total_value:.0f}

ASSET DETENUTI:
{holdings_str}

═══════════════════════════════════
ISTRUZIONI CRITICHE
═══════════════════════════════════

PARTE 1 — IMPATTI ASSET
Per OGNI asset del portafoglio, decidi se è IMPATTATO da questo scenario.
- DIRETTO: l'asset è esplicitamente in winners/losers (match esatto del ticker)
- INDIRETTO: l'asset NON è in winners/losers ma è equivalente (es. XMME.MI ≈ EEM) o legato al settore impattato
- NON IMPATTATO: salta

Per ogni asset impattato fornisci percentuale precisa (es. -22.5%) basata sull'expected_move dello scenario:
- Match esatto: 100% del movimento
- ETF equivalente diretto (stesso indice): 90-100%
- ETF tematico settoriale: 70-85%
- Single-stock pure-play settoriale: 90-100%

PARTE 2 — STORIA PERSONALIZZATA
Scrivi una descrizione su misura per {first_name} (3-5 frasi, 400-700 caratteri).
La storia deve essere SPECIFICA per questo portafoglio:
- Cita per NOME 2-3 asset reali del cliente che sono coinvolti
- Spiega in italiano semplice come quegli specifici asset reagirebbero
- Menziona l'impatto percentuale sul portafoglio totale
- Considera il profilo {profile} e l'obiettivo del cliente
- Dai un'idea della gravità (es. "lieve scossone", "perdita significativa", "danno serio") in modo onesto

NIENTE jargon vuoto tipo: "esposizione strutturale", "tesi compromessa", "rationale solido", "outlook deteriorato", "fondamentali solidi".
Lessico semplice, frasi brevi (max 25 parole).

Esempio buono:
✓ "Per Serena la crisi idrica si traduce in un calo dell'11.5% sul portafoglio totale. L'asset più colpito è ACWI.MI (l'ETF globale), che subirebbe il taglio maggiore in valore assoluto: circa €1.850 in meno. Anche XMME.MI sui mercati emergenti soffre, mentre AI4U.MI sull'intelligenza artificiale cala del 30% per le nuove regole sui consumi idrici dei data center."
✗ "Il portafoglio è strutturalmente esposto agli asset penalizzati e richiede un repricing tattico."

PARTE 3 — AZIONI SUGGERITE
3-5 azioni operative SPECIFICHE per questo cliente. Non template generici.
Ogni azione:
- Cita un asset reale del cliente
- Specifica numericamente cosa fare (es. "ridurre del 30%", "non incrementare")
- Spiega in 1 frase il perché
- Considera la situazione del cliente (profilo, obiettivo)

═══════════════════════════════════
FORMATO OUTPUT (SOLO JSON)
═══════════════════════════════════
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
      "reasoning": "Xtrackers MSCI Emerging Markets replica lo stesso indice MSCI EM dell'EEM citato come loser. Impatto pari al 100%."
    }}
  ],
  "story_text": "Storia personalizzata 3-5 frasi che cita asset reali del cliente...",
  "suggested_actions": [
    {{
      "title": "Ridurre XMME.MI del 30%",
      "ticker": "XMME.MI",
      "description": "Vendere circa €3.750 di XMME.MI riduce l'esposizione ai mercati emergenti che subiranno il maggior impatto. La liquidità può essere reinvestita in oro o cash."
    }}
  ]
}}

Se nessun asset è impattato, restituisci:
{{
  "affected_holdings": [],
  "story_text": "Nessun asset del portafoglio di {first_name} è direttamente coinvolto in questo scenario. [breve spiegazione personalizzata di perché il portafoglio è ben isolato dallo scenario, citando 1-2 asset effettivamente detenuti dal cliente come elementi che danno protezione]",
  "suggested_actions": []
}}"""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=3500,
            messages=[{"role": "user", "content": prompt}],
            timeout=AI_TIMEOUT,
        )
        text = response.content[0].text.strip()
        return parse_response(text)
    except Exception as e:
        print(f"  AI error: {e}", flush=True)
        return None


def save_impact(supabase, user_id, client_id, scenario_id, total_value, analysis):
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
        "story_text": analysis.get("story_text") or "",
        "suggested_actions": analysis.get("suggested_actions") or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("client_scenario_impacts").upsert(row, on_conflict="client_id,scenario_id").execute()
        return True
    except Exception as e:
        print(f"    save error: {e}", flush=True)
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print(f"[scenario_impacts] Modello: {MODEL}", flush=True)
    print("[scenario_impacts] Avvio analisi AI cliente-scenario...", flush=True)

    prices = fetch_prices(supabase)
    print(f"[scenario_impacts] {len(prices)} prezzi caricati", flush=True)

    clients = fetch_all_clients(supabase)
    print(f"[scenario_impacts] {len(clients)} clienti trovati", flush=True)

    scenarios = fetch_recent_scenarios(supabase, limit=SCENARIOS_PER_CLIENT)
    print(f"[scenario_impacts] {len(scenarios)} scenari recenti da analizzare", flush=True)

    if not clients or not scenarios:
        print("[scenario_impacts] Nulla da elaborare.", flush=True)
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
            print(f"  [{client.get('name')}] portafoglio vuoto, salto", flush=True)
            continue

        holdings, total_value = enrich_holdings(holdings_raw, prices)
        if total_value < 100:
            print(f"  [{client.get('name')}] valore troppo basso, salto", flush=True)
            continue

        for scenario in scenarios:
            scenario_id = scenario.get("id")
            if not scenario_id:
                continue
            total_runs += 1
            print(f"  → {client.get('name')} × {scenario.get('title', '')[:60]}", flush=True)

            analysis = analyze_impact(anthropic_client, client, scenario, holdings, total_value)
            if not analysis:
                failed += 1
                continue

            if save_impact(supabase, user_id, client_id, scenario_id, total_value, analysis):
                success += 1
                n = len(analysis.get("affected_holdings") or [])
                print(f"    OK: {n} asset impattati, story={'sì' if analysis.get('story_text') else 'no'}", flush=True)
            else:
                failed += 1

    print(f"\n[scenario_impacts] Completato: {success}/{total_runs} successi, {failed} fallimenti.", flush=True)


if __name__ == "__main__":
    main()
