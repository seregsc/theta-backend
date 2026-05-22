import os
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"


def generate_full_analysis(client, title_en, summary_en, tickers_csv):
    tickers_str = tickers_csv if tickers_csv else "—"
    prompt = f"""Sei un analista finanziario senior italiano che scrive per consulenti professionisti. Riceverai una notizia in inglese e devi produrre un'analisi completa in italiano fluido e professionale.

REGOLE
- Scrivi in italiano corretto, lessico finanziario professionale.
- Usa SOLO informazioni presenti nel testo originale. Non inventare numeri, date, eventi.
- Tono: neutro, informativo, mai sensazionalistico.
- Non dare consigli espliciti di acquisto/vendita.

INPUT
Titolo originale (EN): {title_en}
Sommario originale (EN): {summary_en or "—"}
Ticker citati: {tickers_str}

OUTPUT — formato esatto, 4 blocchi etichettati:

TITOLO: <titolo italiano, max 110 caratteri, riformulato non tradotto letterale>

SOMMARIO: <riassunto in italiano, 4-6 frasi (600-900 caratteri). Contesto, dati chiave, attori, significato per il mercato. Stile articolo giornalistico breve. Se l'originale è povero, espandi con contesto settoriale plausibile ma SENZA inventare fatti specifici.>

IMPATTO: <analisi 3-4 frasi (350-550 caratteri) sull'impatto previsto. Quali settori/asset coinvolti, in che direzione, quali correlazioni di mercato. Concreto.>

STRATEGIA: <suggerimento operativo 3-4 frasi (350-550 caratteri) per un consulente. Cosa monitorare, riallocazioni settoriali, hedging, attese. Mai categorico, sempre condizionale. Cita ticker se rilevanti.>"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        sections = {"TITOLO": None, "SOMMARIO": None, "IMPATTO": None, "STRATEGIA": None}
        current_key = None
        current_lines = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                if current_key and current_lines:
                    sections[current_key] = " ".join(current_lines).strip()
                    current_lines = []
                continue
            matched_header = False
            for key in sections.keys():
                if line.startswith(f"{key}:"):
                    if current_key and current_lines:
                        sections[current_key] = " ".join(current_lines).strip()
                    current_key = key
                    rest = line[len(key) + 1:].strip()
                    current_lines = [rest] if rest else []
                    matched_header = True
                    break
            if not matched_header and current_key:
                current_lines.append(line)
        if current_key and current_lines:
            sections[current_key] = " ".join(current_lines).strip()
        return sections["TITOLO"], sections["SOMMARIO"], sections["IMPATTO"], sections["STRATEGIA"]
    except Exception as e:
        print(f"  errore Claude: {e}")
        return None, None, None, None


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    result = supabase.table("news") \
        .select("id, title, summary, tickers, title_it, summary_it, impact_it, strategy_it") \
        .execute()

    all_rows = result.data or []
    rows = [r for r in all_rows if not r.get("impact_it") or not r.get("strategy_it")]
    print(f"Database: {len(all_rows)} news totali, {len(rows)} senza analisi completa.")

    if not rows:
        print("Tutte le news sono gia state analizzate. Nulla da fare.")
        return

    updated = 0
    for i, row in enumerate(rows, 1):
        title_en = row.get("title")
        summary_en = row.get("summary")
        tickers = row.get("tickers")
        print(f"[{i}/{len(rows)}] {(title_en or '')[:60]}")

        title_it, summary_it, impact_it, strategy_it = generate_full_analysis(
            client, title_en, summary_en, tickers
        )

        if not title_it:
            print(f"  saltata: parsing fallito")
            continue

        try:
            supabase.table("news").update({
                "title_it": title_it,
                "summary_it": summary_it,
                "impact_it": impact_it,
                "strategy_it": strategy_it,
            }).eq("id", row["id"]).execute()
            updated += 1
            print(f"  ok aggiornata")
        except Exception as e:
            print(f"  errore update: {e}")

    print(f"\nFatto: {updated}/{len(rows)} news aggiornate.")


if __name__ == "__main__":
    main()
