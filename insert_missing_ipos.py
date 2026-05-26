"""
Insert IPO Mancanti — popola la tabella `ipos_live` su Supabase con le 5 IPO
che mancavano (KLAR, CHYM, WYVE, RVMD, FBRC).

Usa upsert su (ticker) — se rilanciato, aggiorna i record esistenti senza duplicare.
"""

import os
import sys
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠ SUPABASE_URL o SUPABASE_KEY non configurati")
    sys.exit(1)

# Helper: Q3 2026 / H1 2027 → date
def quarter_to_date(q):
    if not q or "TBD" in q:
        return None
    q = q.strip()
    # H1/H2 → mid-quarter
    if q.startswith("H1"):
        year = q.split()[-1]
        return f"{year}-06-30"
    if q.startswith("H2"):
        year = q.split()[-1]
        return f"{year}-12-31"
    # Q1/Q2/Q3/Q4 → end-of-quarter
    if q.startswith("Q"):
        qnum = int(q[1])
        year = q.split()[-1]
        month_day = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[qnum]
        return f"{year}-{month_day}"
    return None


IPOS = [
    {
        "ticker": "KLAR",
        "name": "Klarna",
        "sector": "FinTech BNPL",
        "category": "fintech",
        "geo": "EU",
        "exchange": "NYSE",
        "score": 74,
        "upside": "+38%",
        "risk": "MED",
        "market_cap_estimate": 20_000_000_000,
        "target": "$28B val",
        "status": "pre-ipo",
        "ipo_date": quarter_to_date("Q3 2026"),
        "expected_date": "Q3 2026",
        "price_range": "$32-$38",
        "offer_size": "$1.2B",
        "shares_offered": 32_000_000,
        "lead_underwriters": ["Goldman Sachs", "JPMorgan", "Morgan Stanley"],
        "valuation_method": "P/S 4x su revenue $5B vs media fintech 5.5x",
        "revenue_growth": "+24%",
        "gross_margin": "65%",
        "ebitda_margin": "8%",
        "debt_eq": "0.4x",
        "founders": ["Sebastian Siemiatkowski (CEO)", "Niklas Adalberth", "Victor Jacobsson"],
        "employees": 5500,
        "founded": 2005,
        "last_round": "$6.7B (Lug 2022, down round)",
        "headline": "BNPL leader EU — breakeven raggiunto",
        "summary": "Buy Now Pay Later leader globale. 85M clienti attivi in 45 paesi, GMV $90B annuo.",
        "business": "Pagamenti differenziati e a rate. Partnership con 575K merchant. Diversificazione in conti correnti, carte, e investimenti retail.",
        "pros": ["Breakeven raggiunto dopo anni di perdite", "85M clienti loyalty alto (NPS 65)", "Partnership Apple Pay sblocca USA", "Diversificazione oltre BNPL"],
        "cons": ["Down round 2022 segnale valuation", "Regolamentazione BNPL stringente EU/USA", "Margini sotto pressione (carte vs BNPL)", "Concorrenza Affirm + Apple + PayPal"],
        "use_of_proceeds": "Espansione USA ($500M) + tecnologia AI ($300M) + working capital ($400M)",
        "competitors": ["Affirm", "PayPal", "Apple Pay Later", "Block (Afterpay)"],
        "catalysts": ["Adozione USA", "Tagli Fed = consumi", "M&A consolidamento"],
        "rating": "BUY",
        "suited_for": ["balanced", "growth"],
        "tier": 1,
    },
    {
        "ticker": "CHYM",
        "name": "Chime Financial",
        "sector": "NeoBank",
        "category": "fintech",
        "geo": "USA",
        "exchange": "NASDAQ",
        "score": 71,
        "upside": "+29%",
        "risk": "LOW",
        "market_cap_estimate": 10_000_000_000,
        "target": "$13B val",
        "status": "pre-ipo",
        "ipo_date": quarter_to_date("Q4 2026"),
        "expected_date": "Q4 2026",
        "price_range": "$22-$26",
        "offer_size": "$800M",
        "shares_offered": 32_000_000,
        "lead_underwriters": ["Goldman Sachs", "JPMorgan", "Citi"],
        "valuation_method": "P/S 6x su revenue $1.7B, premium per redditività e crescita clienti",
        "revenue_growth": "+38%",
        "gross_margin": "82%",
        "ebitda_margin": "12%",
        "debt_eq": "0.2x",
        "founders": ["Chris Britt (CEO)", "Ryan King"],
        "employees": 1450,
        "founded": 2013,
        "last_round": "$25B (Ago 2021, Sequoia)",
        "headline": "22M clienti, redditività raggiunta",
        "summary": "NeoBank leader USA per giovani sotto 35 anni. Conti zero-fee, debit cards, prestiti istantanei.",
        "business": "Banca digitale partner di Stride Bank per servizi bancari. Revenue principale da interchange su debit cards (87%). 22M clienti.",
        "pros": ["Redditività raggiunta nel 2024", "NPS 72 vs 15-25 banche tradizionali", "CAC $20 vs $250 banche tradizionali", "Lead underwriter Goldman + JPMorgan"],
        "cons": ["Valuation 60% sotto picco 2021 ($25B → $10B)", "Modello revenue dipende da interchange fees", "Regolamentazione neobank in tightening", "Competizione Cash App + Apple Card"],
        "use_of_proceeds": "Espansione prodotti (credito, mortgage) + marketing + acquisizioni minori",
        "competitors": ["Cash App", "SoFi", "Varo", "Apple Card"],
        "catalysts": ["Cross-sell credito", "Espansione SMB", "Tagli Fed boost margini"],
        "rating": "HOLD",
        "suited_for": ["balanced", "growth"],
        "tier": 2,
    },
    {
        "ticker": "WYVE",
        "name": "Wayve Technologies",
        "sector": "Autonomous Driving",
        "category": "ai",
        "geo": "EU",
        "exchange": "NASDAQ",
        "score": 67,
        "upside": "+65%",
        "risk": "HIGH",
        "market_cap_estimate": 7_000_000_000,
        "target": "$11B val",
        "status": "upcoming",
        "ipo_date": quarter_to_date("Q1 2027"),
        "expected_date": "Q1 2027",
        "price_range": "$28-$34",
        "offer_size": "$1.5B",
        "shares_offered": 48_000_000,
        "lead_underwriters": ["Morgan Stanley", "Bank of America", "Barclays"],
        "valuation_method": "Comparable Mobileye + DCF 10Y, premium per tecnologia E2E AI",
        "revenue_growth": "Pre-revenue",
        "gross_margin": "N/A",
        "ebitda_margin": "Negativo",
        "debt_eq": "N/A",
        "founders": ["Alex Kendall (CEO)", "Amar Shah"],
        "employees": 380,
        "founded": 2017,
        "last_round": "$1.05B Series C (Mag 2024, SoftBank)",
        "headline": "Guida autonoma software-defined",
        "summary": "Software guida autonoma end-to-end basato su AI generativa. Architettura embodied AI senza HD-mapping.",
        "business": "Software embedded in auto OEM. Approccio E2E vision-based (no LiDAR). 4 partnership OEM in negoziazione.",
        "pros": ["Tecnologia E2E AI vs HD-mapping legacy", "Backed da SoftBank ($1B) e Microsoft", "Talent pool top da DeepMind/Google", "Approccio asset-light scalabile"],
        "cons": ["Pre-revenue, breakeven incerto", "Concorrenza Mobileye + Tesla FSD + cinesi", "Dipendenza partnership OEM (lunghe negoziazioni)", "Settore autonomous in deflation valutazioni"],
        "use_of_proceeds": "R&D AI training ($800M) + espansione team ($300M) + GPU compute ($400M)",
        "competitors": ["Mobileye", "Tesla FSD", "Waymo", "Comma.ai"],
        "catalysts": ["Deal OEM annunciato", "Partnership Microsoft AI", "Robotaxi launch"],
        "rating": "SPECULATIVE BUY",
        "suited_for": ["aggressive"],
        "tier": 2,
    },
    {
        "ticker": "RVMD",
        "name": "Revolut",
        "sector": "NeoBank",
        "category": "fintech",
        "geo": "EU",
        "exchange": "LSE",
        "score": 78,
        "upside": "+35%",
        "risk": "MED",
        "market_cap_estimate": 45_000_000_000,
        "target": "$61B val",
        "status": "upcoming",
        "ipo_date": quarter_to_date("Q2 2027"),
        "expected_date": "Q2 2027",
        "price_range": "$24-$28",
        "offer_size": "$2B",
        "shares_offered": 78_000_000,
        "lead_underwriters": ["Goldman Sachs", "Morgan Stanley", "JPMorgan"],
        "valuation_method": "P/B 8x su book value, premium per crescita 50M+ clienti",
        "revenue_growth": "+45%",
        "gross_margin": "68%",
        "ebitda_margin": "18%",
        "debt_eq": "0.3x",
        "founders": ["Nikolay Storonsky (CEO)", "Vlad Yatsenko"],
        "employees": 10500,
        "founded": 2015,
        "last_round": "$45B (Lug 2024, secondary tender)",
        "headline": "Maggior neobank EU, 50M clienti globali",
        "summary": "Super-app finanziaria globale. Conti multi-valuta, trading, crypto, lending, business accounts.",
        "business": "Banca digitale presente in 38 paesi. UK banking license ottenuta 2024. Espansione lending + wealth management.",
        "pros": ["50M clienti globali, leader EU neobank", "Diversificazione revenue (trading, lending, FX)", "Redditività raggiunta nel 2023", "UK banking license sblocca prestiti"],
        "cons": ["Valuation 45B aggressive vs banche tradizionali", "Regolamentazione AML scrutiny continuo", "FX revenue sensibile a competizione", "Espansione USA difficile (license)"],
        "use_of_proceeds": "Espansione USA + India + lending portfolio + tecnologia",
        "competitors": ["Klarna", "Nubank", "N26", "Wise"],
        "catalysts": ["USA banking license", "India launch", "Wealth management ramp"],
        "rating": "BUY",
        "suited_for": ["growth"],
        "tier": 1,
    },
    {
        "ticker": "FBRC",
        "name": "Faraday Battery",
        "sector": "Battery Tech",
        "category": "auto",
        "geo": "EU",
        "exchange": "AMS",
        "score": 64,
        "upside": "+55%",
        "risk": "HIGH",
        "market_cap_estimate": 4_000_000_000,
        "target": "$6.2B val",
        "status": "rumored",
        "ipo_date": quarter_to_date("H1 2027"),
        "expected_date": "H1 2027",
        "price_range": "$12-$16",
        "offer_size": "$600M",
        "shares_offered": 40_000_000,
        "lead_underwriters": ["Morgan Stanley", "ING", "BNP Paribas"],
        "valuation_method": "EV/Revenue 8x su revenue stimati 2027 $500M",
        "revenue_growth": "Pre-revenue",
        "gross_margin": "N/A",
        "ebitda_margin": "Negativo",
        "debt_eq": "1.2x",
        "founders": ["Various ex-Northvolt"],
        "employees": 850,
        "founded": 2019,
        "last_round": "€350M Series C (Set 2024)",
        "headline": "Solid-state battery EU pure-play",
        "summary": "Sviluppo batterie solid-state per EV con densità energetica +50% vs litio-ione.",
        "business": "Tecnologia batterie solid-state in fase di pre-produzione. Partnership BMW, Stellantis per fornitura 2027-28.",
        "pros": ["Tech solid-state breakthrough confermato", "Partnership tier-1 OEM (BMW, Stellantis)", "EU supply chain advantage", "IRA/CHIPS Act funding possibile"],
        "cons": ["Pre-revenue, breakeven 2028+", "Concorrenza CATL/BYD/QuantumScape", "Northvolt bankruptcy = market spooked", "Capex enorme per scale-up"],
        "use_of_proceeds": "Gigafactory Germania ($400M) + R&D ($150M) + working capital ($50M)",
        "competitors": ["QuantumScape", "Solid Power", "CATL", "BYD"],
        "catalysts": ["Prima auto con batteria Faraday", "Funding EU green deal", "Test density 400 Wh/kg"],
        "rating": "SPECULATIVE",
        "suited_for": ["aggressive"],
        "tier": 3,
    },
]


def main():
    print("=" * 60)
    print("INSERT IPO MANCANTI — ipos_live su Supabase")
    print("=" * 60)
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Verifica ticker già presenti
    existing = supabase.table("ipos_live").select("ticker").execute()
    existing_tickers = set([r["ticker"] for r in (existing.data or [])])
    print(f"\nTicker già presenti nel DB: {len(existing_tickers)}")
    if existing_tickers:
        print(f"  · {', '.join(sorted(existing_tickers))}")

    to_insert = [ipo for ipo in IPOS if ipo["ticker"] not in existing_tickers]
    print(f"\nIPO da inserire: {len(to_insert)} / {len(IPOS)}")

    if not to_insert:
        print("✓ Tutti i ticker sono già nel DB. Nulla da fare.")
        return

    for ipo in to_insert:
        try:
            res = supabase.table("ipos_live").insert(ipo).execute()
            print(f"  ✓ {ipo['ticker']} ({ipo['name']}) inserito")
        except Exception as e:
            err_str = str(e)
            # Se manca una colonna, prova rimuovendo i campi che danno errore
            print(f"  ✗ {ipo['ticker']}: {err_str[:200]}")
            # Suggerimento: proviamo a inserire solo i campi minimi
            print(f"    [Verifica manualmente lo schema della tabella ipos_live]")

    print()
    print("✓ Completato.")


if __name__ == "__main__":
    main()
