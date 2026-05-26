"""
generate_scenario_impacts.py
Per OGNI scenario attivo + OGNI cliente, fa un'analisi AI personalizzata:
- Quali asset del cliente sono impattati DIRETTAMENTE o INDIRETTAMENTE dallo scenario?
- Storia narrativa "Cosa succede a [cliente]" su misura per quel cliente.
- Azioni suggerite specifiche.
- Salva in client_scenario_impacts (UPSERT su client_id+scenario_id).

Modello: Haiku con prompt enterprise-grade (più dettagliato e narrativo).
"""

import os
import json
from datetime import datetime, timedelta, timezone
from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5-20251001"

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
            "sector": h.get("sector") or None,
            "geo": h.get("geo") or None,
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
    # Holdings con peso percentuale (aiuta l'AI a focalizzarsi sui pesi reali)
    holdings_lines = []
    for h in holdings_enriched:
        pct = (h["value"] / total_value * 100) if total_value > 0 else 0
        sector_str = f", settore {h['sector']}" if h.get("sector") else ""
        geo_str = f", area {h['geo']}" if h.get("geo") else ""
        holdings_lines.append(
            f"  - {h['ticker']} ({h['name']}): €{h['value']:,.0f} = {pct:.1f}% del portafoglio | {h['asset_type']}{sector_str}{geo_str}"
        )
    holdings_str = "\n".join(holdings_lines) if holdings_lines else "  (nessuna posizione)"

    # Winners / Losers / Sectors con più dettaglio
    winners_str = "\n".join([
        f"  - {w.get('ticker')} ({w.get('name', '—')}): {w.get('expected_move', '—')} | {w.get('why', '')[:200]}"
        for w in (scenario.get("winners") or [])
    ]) if scenario.get("winners") else "  (nessun beneficiario diretto)"

    losers_str = "\n".join([
        f"  - {l.get('ticker')} ({l.get('name', '—')}): {l.get('expected_move', '—')} | {l.get('why', '')[:200]}"
        for l in (scenario.get("losers") or [])
    ]) if scenario.get("losers") else "  (nessun perdente diretto)"

    sector_impact = scenario.get("sector_impact") or {}
    sectors_str = "\n".join([
        f"  - Settore '{k}': {v.get('expected_move', '—')} | {v.get('why', '')[:200]}"
        for k, v in sector_impact.items()
    ]) if sector_impact else "  (nessun impatto settoriale specificato)"

    # Profilo cliente esteso
    profile_map = {
        "conservativo": ("prudente", "evita perdite, accetta rendimenti bassi"),
        "moderato": ("moderato", "bilancia crescita e protezione"),
        "aggressivo": ("aggressivo", "cerca rendimenti alti, tollera la volatilità"),
        "balanced": ("bilanciato", "bilancia crescita e protezione"),
        "growth": ("growth", "cerca crescita, tollera oscillazioni"),
    }
    profile_raw = client.get("risk_profile") or "moderato"
    profile, profile_desc = profile_map.get(profile_raw, ("moderato", "bilancia crescita e protezione"))
    first_name = (client.get('name', '') or '').split(' ')[0] or "il cliente"
    age = client.get("age")
    age_str = f"{age} anni" if age else "età non specificata"
    objective = client.get("objective") or "crescita patrimoniale"
    horizon = client.get("time_horizon") or "medio termine"
    hedge_strategy = scenario.get("hedge_strategy") or ""

    prompt = f"""Sei un risk analyst senior italiano con 20 anni di esperienza in private banking. Devi produrre un'analisi PROFESSIONALE e NARRATIVA per un consulente che la userà direttamente col cliente.

═══════════════════════════════════════════════════════════════════
SCENARIO DI STRESS
═══════════════════════════════════════════════════════════════════
TITOLO: {scenario.get('title', '—')}
SEVERITÀ: {scenario.get('severity', '—')}
PROBABILITÀ: {scenario.get('probability', '—')}
ORIZZONTE: {scenario.get('time_horizon', '—')}

DESCRIZIONE COMPLETA:
{scenario.get('description', '—')}

TESI DI INVESTIMENTO:
{scenario.get('thesis', '—')}

STRATEGIA DI COPERTURA SUGGERITA DAL SISTEMA:
{hedge_strategy[:800] if hedge_strategy else '(non disponibile)'}

WINNERS (asset che salgono):
{winners_str}

LOSERS (asset che scendono):
{losers_str}

IMPATTI PER SETTORE:
{sectors_str}

═══════════════════════════════════════════════════════════════════
PROFILO CLIENTE
═══════════════════════════════════════════════════════════════════
Nome completo: {client.get('name', '—')}
Età: {age_str}
Profilo di rischio: {profile} ({profile_desc})
Obiettivo dichiarato: {objective}
Orizzonte temporale: {horizon}
Valore portafoglio: €{total_value:,.0f}

ASSET DETENUTI (con peso percentuale reale):
{holdings_str}

═══════════════════════════════════════════════════════════════════
ISTRUZIONI CRITICHE
═══════════════════════════════════════════════════════════════════

PARTE 1 — IMPATTI ASSET (catena causale esplicita)
Per OGNI asset del portafoglio, decidi se è impattato dallo scenario:

• DIRETTO: il ticker dell'asset è ESATTAMENTE in winners/losers
• INDIRETTO: l'asset non è in winners/losers ma è equivalente o correlato. Casi tipici:
  - ETF su stesso indice (es. XMME.MI ≈ EEM, ACWI.MI ≈ ACWI, VWCE.DE ≈ MSCI ACWI)
  - ETF settoriale che ricalca un loser/winner (es. SOXX se ASML è loser)
  - Single-stock pure-play in settore impattato (es. ENI se petrolio è impattato)
  - SAFE HAVEN: oro (GLD, IAU, PHAU, IGLN, SGLD, etc.), argento, treasuries lunghi (TLT, VGLT, EGOV), franchi/yen — questi SALGONO in scenari risk-off anche se non menzionati esplicitamente come winners
• NON IMPATTATO: salta

Per ogni asset impattato fornisci percentuale precisa basata sull'expected_move dello scenario:
- Match esatto ticker: 100% del movimento
- ETF equivalente diretto (stesso indice): 90-100%
- ETF tematico settoriale: 70-85%
- Single-stock pure-play settoriale: 90-100%
- Safe haven in scenari risk-off: +8% a +15% per oro, +3% a +7% per treasuries lunghi, +20%+ per volatility

CRITICO: il REASONING di ogni asset deve essere una CATENA CAUSALE chiara di 2-3 frasi:
"Lo scenario X causa Y che impatta il settore Z. {{ticker}} è un ETF su Z, quindi subirà -W%."
Non frasi vuote tipo "esposizione settoriale rilevante". Catene di cause concrete.

PARTE 2 — STORIA PERSONALIZZATA (narrativa, 600-900 caratteri)
Scrivi una storia su misura per {first_name} che il consulente leggerà al cliente come fosse un report di un risk analyst senior.

STRUTTURA OBBLIGATORIA (3 atti):

ATTO 1 (1-2 frasi): l'apertura. Spiega in italiano semplice cosa succede al portafoglio. Cita il numero specifico (impatto % totale). Esempio: "Lo scenario X colpisce in modo significativo il portafoglio di Mario: la perdita stimata è del 12.3%, circa €18.500 sul totale di €150.000."

ATTO 2 (2-4 frasi): il cuore. CITA PER NOME 2-3 asset specifici del portafoglio e spiega CONCRETAMENTE come quegli asset reagirebbero. Devi essere PRECISO e tecnico:
- "Il maggior contributo alla perdita viene da XMME.MI (mercati emergenti): -28% perché le tensioni geopolitiche cinesi colpiscono direttamente quell'indice."
- "Anche ACWI.MI soffre del -15% trascinato dalla componente emerging che pesa il 12% dell'indice globale."
- "Per fortuna {{nome_safe_haven}} compensa parzialmente con un +12% atteso, ammorbidendo la caduta complessiva."
Mostra IL LEGAME tra scenario e asset, non descrizioni generiche.

ATTO 3 (1-2 frasi): conclusione e raccomandazione operativa per il consulente.
- Sii ONESTO sulla gravità: "lieve scossone" / "perdita significativa ma gestibile" / "danno serio che richiede azione immediata"
- Considera profilo {profile} e età {age_str}: "Per il profilo {profile} di {first_name} questa volatilità è dentro la sua tolleranza" oppure "Per il suo profilo prudente è oltre il livello accettabile"
- Suggerisci tipo di conversazione col cliente: "Consiglio di anticipare questa scossa con una telefonata proattiva" / "Inutile allarmare {first_name}, l'impatto è dentro la normalità"

REGOLE DI STILE:
✗ NIENTE jargon vuoto: "esposizione strutturale", "tesi compromessa", "rationale solido", "outlook deteriorato", "fondamentali solidi", "repricing tattico"
✓ Italiano scorrevole, frasi brevi (max 25 parole), termini concreti
✓ Sempre numeri precisi (% e €), nomi reali degli asset, settori specifici
✓ Tono professionale ma umano: ti rivolgi a un consulente che lavora con persone vere

ESEMPI DI BUONO STILE:
✓ "Lo scenario crisi idrica europea colpisce duramente il portafoglio di Mario con una perdita stimata dell'11.5% (€8.450 su €73.500). Il maggior contributo arriva da AI4U.MI (-30%): le nuove regole UE sul consumo idrico dei data center penalizzeranno proprio l'industria AI. Anche ACWI.MI scende del 6% trascinato dalla sua componente tech globale. La posizione in oro PHAU.MI dovrebbe invece guadagnare il 12%, attenuando la perdita complessiva di circa €1.800. Per il profilo aggressivo di Mario questa volatilità è gestibile, ma vale la pena anticipare la conversazione con una telefonata: spiegargli la logica dello scenario lo aiuta a non farsi prendere dal panico quando i prezzi inizieranno effettivamente a scendere."

✗ "Il portafoglio risulta strutturalmente esposto agli asset più colpiti, con un impatto in linea con la tesi macro. Si consiglia un repricing tattico."

PARTE 3 — AZIONI SUGGERITE (operative, 3-5 azioni numeriche)
Ogni azione DEVE:
- Citare un asset reale del cliente per ticker
- Specificare un'AZIONE QUANTITATIVA: "vendere €X", "ridurre dal Y% al Z%", "aggiungere €W"
- Spiegare in 1 frase il motivo specifico (non generico)
- Considerare il profilo {profile} (es. non suggerire vendite drastiche a un cliente prudente per scenari poco probabili)
- Includere ALMENO una raccomandazione DIFENSIVA se lo scenario è negativo (es. aumentare oro, treasuries, cash)

═══════════════════════════════════════════════════════════════════
FORMATO OUTPUT (SOLO JSON, niente preamble)
═══════════════════════════════════════════════════════════════════
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
      "reasoning": "Lo scenario di tensione Cina-Taiwan colpisce direttamente i mercati emergenti asiatici. XMME.MI replica l'indice MSCI EM dove Cina e Taiwan pesano il 38%, quindi subisce impatto pieno -28%, equivalente al loser EEM citato nello scenario."
    }},
    {{
      "ticker": "PHAU.MI",
      "name": "WisdomTree Physical Gold",
      "value": 8000,
      "impact_type": "indirect",
      "equivalent_to": "GLD",
      "expected_move_pct": 12.0,
      "expected_move_eur": 960,
      "type": "winner",
      "reasoning": "PHAU.MI è un ETC su oro fisico, classico safe haven in scenari geopolitici risk-off. Storicamente in crisi simili (2008, 2020, 2022) l'oro è salito 8-15%, qui stimiamo +12%."
    }}
  ],
  "story_text": "Storia personalizzata 3 atti come da istruzioni sopra (600-900 caratteri)...",
  "suggested_actions": [
    {{
      "title": "Ridurre XMME.MI del 40%",
      "ticker": "XMME.MI",
      "description": "Vendere circa €5.000 di XMME.MI per ridurre l'esposizione ai mercati emergenti che subiranno il maggior impatto nello scenario. La liquidità può essere reinvestita in oro o cash."
    }},
    {{
      "title": "Aumentare PHAU.MI di €3.000",
      "ticker": "PHAU.MI",
      "description": "Portare la posizione in oro al 12% del portafoglio (da 8% attuale). Storicamente l'oro guadagna in scenari geopolitici e bilancia la perdita degli asset rischiosi."
    }}
  ]
}}

Se nessun asset è impattato (caso raro), restituisci:
{{
  "affected_holdings": [],
  "story_text": "Storia personalizzata che spiega perché {first_name} è ben isolato dallo scenario, citando 2-3 asset reali che danno protezione (es. oro, treasuries, qualità non ciclica). Devi essere SPECIFICO sui motivi del basso impatto.",
  "suggested_actions": []
}}"""

    try:
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=5000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        return parse_response(text)
    except Exception as e:
        print(f"  AI error: {e}")
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
        print(f"    save error: {e}")
        return False


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("[scenario_impacts] Avvio analisi AI cliente-scenario (Haiku, prompt esteso)...")

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
                story_len = len(analysis.get("story_text") or "")
                print(f"    OK: {n} asset, story={story_len} char")
            else:
                failed += 1

    print(f"\n[scenario_impacts] Completato: {success}/{total_runs} successi, {failed} fallimenti.")


if __name__ == "__main__":
    main()
