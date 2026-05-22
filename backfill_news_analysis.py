
"""
Backfill news analysis.
Riprocessa SOLO le news che hanno analisi mancante o incompleta.
Pensato per girare in automatico ogni poche ore senza sprecare credit Claude.
"""
import os
import re
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"


def parse_sections(text):
    keys = ["TITOLO", "SOMMARIO", "IMPATTO", "STRATEGIA"]
    sections = {k: None for k in keys}
    pattern = r"(?:^|\n)\s*(?:#+\s*)?(?:\*\*)?(TITOLO|SOMMARIO|IMPATTO|STRATEGIA)(?:\*\*)?\s*[:\-—]\s*"
    matches = list(re.finditer(pattern, text, re.IGNORECASE))
    if not matches:
        return sections
    for i, m in enumerate(matches):
        key = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        content = re.sub(r"^[*#\s]+", "", content)
        content = re.sub(r"[*#\s]+$", "", content)
        sections[key] = content if content else None
    return sections


def generate_full_analysis(client, title_en, summary_en, tickers_csv):
    tickers_str = tickers_csv if tickers_csv else "—"
    prompt = f"""Sei un analista finanziario senior italiano che scrive direttamente per UN consulente finanziario specifico (l'utente di Theta). Riceverai una notizia in inglese e devi produrre un'analisi completa in italiano professionale.

REGOLE GENERALI
- Italiano corretto, lessico finanziario professionale.
- Usa SOLO informazioni presenti nel testo originale. Non inventare numeri, date, eventi.
- Tono neutro e informativo nei primi 3 blocchi (TITOLO, SOMMARIO, IMPATTO).
- Nella STRATEGIA parla direttamente al consulente al TU («valuta», «monitora», «considera», «alleggerisci»). Mai usare «i consulenti potrebbero», mai forme impersonali.
- Non dare consigli espliciti di acquisto/vendita: usa sempre forme condizionali («se il segnale si conferma...», «per i clienti con profilo X...»).
- NON usare markdown (no asterischi, no cancelletti, no grassetto). Solo testo semplice.

INPUT
Titolo originale (EN): {title_en}
Sommario originale (EN): {summary_en or "—"}
Ticker citati: {tickers_str}

OUTPUT — rispondi ESATTAMENTE in questo formato con 4 blocchi etichettati, senza altro testo prima o dopo:

TITOLO: <titolo italiano, max 110 caratteri, riformulato non tradotto letterale>

SOMMARIO: <riassunto 4-6 frasi (600-900 caratteri). Contesto, dati chiave, attori, significato per il mercato. Stile articolo giornalistico breve. Se l'originale è povero, espandi con contesto settoriale plausibile ma senza inventare fatti specifici.>

IMPATTO: <analisi 3-4 frasi (350-550 caratteri) sull'impatto previsto. Quali settori/asset coinvolti, in che direzione, quali correlazioni di mercato. Concreto.>

STRATEGIA: <suggerimento operativo 3-4 frasi (350-550 caratteri) rivolto direttamente al consulente al TU. Esempi di apertura: «Valuta di alleggerire...», «Monitora attentamente...», «Considera di proporre ai tuoi clienti...», «Se vedi questi segnali...». Mai «i consulenti potrebbero». Cita ticker se rilevanti.>"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        sections = parse_sections(text)
        return (
            sections.get("TITOLO"),
            sections.get("SOMMARIO"),
            sections.get("IMPATTO"),
            sections.get("STRATEGIA"),
            text,
        )
    except Exception as e:
        print(f"  errore Claude: {e}")
        return None, None, None, None, None


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # FILTRO INTELLIGENTE: solo news con analisi mancante o incompleta
    result = supabase.table("news") \
        .select("id, title, summary, tickers, title_it, summary_it, impact_it, strategy_it") \
        .execute()

    all_rows = result.data or []
    rows_to_process = [
        r for r in all_rows
        if not r.get("title_it") or not r.get("summary_it")
        or not r.get("impact_it") or not r.get("strategy_it")
    ]

    print(f"Database: {len(all_rows)} news totali.")
    print(f"Da riprocessare (analisi incompleta): {len(rows_to_process)}\n")

    if not rows_to_process:
        print("Tutte le news sono complete. Nessun lavoro da fare.")
        return

    updated = 0
    failed = 0
    for i, row in enumerate(rows_to_process, 1):
        title_en = row.get("title")
        summary_en = row.get("summary")
        tickers = row.get("tickers")
        print(f"[{i}/{len(rows_to_process)}] {(title_en or '')[:60]}")

        title_it, summary_it, impact_it, strategy_it, raw_text = generate_full_analysis(
            client, title_en, summary_en, tickers
        )

        if not impact_it or not strategy_it:
            failed += 1
            print(f"  parsing parziale (TITOLO={bool(title_it)}, SOMM={bool(summary_it)}, IMP={bool(impact_it)}, STR={bool(strategy_it)})")
            if raw_text:
                print(f"  Prime 200 lettere risposta Claude: {raw_text[:200]}")
            continue

        try:
            supabase.table("news").update({
                "title_it": title_it,
                "summary_it": summary_it,
                "impact_it": impact_it,
                "strategy_it": strategy_it,
            }).eq("id", row["id"]).execute()
            updated += 1
            print(f"  ok recuperata")
        except Exception as e:
            print(f"  errore update: {e}")

    print(f"\nFatto: {updated}/{len(rows_to_process)} recuperate, {failed} fallite.")


if __name__ == "__main__":
    main()
