"""
Backfill news analysis.
Riprocessa SOLO le news con analisi/categoria mancante.
Usa Haiku per risparmio.
"""
import os
import re
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5-20251001"

CATEGORIES = ["azioni", "macroeconomia", "geopolitica", "materie_prime", "generica"]


def parse_sections(text):
    keys = ["CATEGORIA", "TITOLO", "SOMMARIO", "IMPATTO", "STRATEGIA"]
    sections = {k: None for k in keys}
    pattern = r"(?:^|\n)\s*(?:#+\s*)?(?:\*\*)?(CATEGORIA|TITOLO|SOMMARIO|IMPATTO|STRATEGIA)(?:\*\*)?\s*[:\-—]\s*"
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
    prompt = f"""Sei un analista finanziario senior italiano che scrive per UN consulente specifico (l'utente di Theta). Devi tradurre + arricchire + classificare la notizia.

REGOLE
- Italiano professionale, lessico finanziario.
- Solo informazioni dal testo originale. Niente invenzioni.
- Tono neutro nei primi blocchi.
- Nella STRATEGIA al TU al consulente («valuta», «monitora»). Mai «i consulenti potrebbero».
- NON usare markdown.

CATEGORIE (sceglierne UNA):
- azioni: società quotate, earnings, M&A, IPO
- macroeconomia: PIL, inflazione, banche centrali, tassi
- geopolitica: conflitti, sanzioni, elezioni, politica estera
- materie_prime: petrolio, gas, oro, metalli, agricoltura
- generica: altro

INPUT
Titolo originale (EN): {title_en}
Sommario originale (EN): {summary_en or "—"}
Ticker citati: {tickers_str}

OUTPUT — rispondi ESATTAMENTE in questo formato, senza altro testo:

CATEGORIA: <una sola tra: azioni, macroeconomia, geopolitica, materie_prime, generica>

TITOLO: <titolo italiano, max 110 caratteri, riformulato>

SOMMARIO: <riassunto 4-6 frasi (600-900 caratteri)>

IMPATTO: <3-4 frasi (350-550 caratteri)>

STRATEGIA: <3-4 frasi (350-550 caratteri) al TU>"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        sections = parse_sections(text)
        cat = (sections.get("CATEGORIA") or "generica").lower().strip()
        if cat not in CATEGORIES:
            cat = "generica"
        return (
            cat,
            sections.get("TITOLO"),
            sections.get("SOMMARIO"),
            sections.get("IMPATTO"),
            sections.get("STRATEGIA"),
            text,
        )
    except Exception as e:
        print(f"  errore Claude: {e}")
        return None, None, None, None, None, None


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    result = supabase.table("news") \
        .select("id, title, summary, tickers, title_it, summary_it, impact_it, strategy_it, category") \
        .execute()

    all_rows = result.data or []
    rows_to_process = [
        r for r in all_rows
        if not r.get("title_it") or not r.get("summary_it")
        or not r.get("impact_it") or not r.get("strategy_it")
        or not r.get("category")
    ]

    print(f"Database: {len(all_rows)} news totali.")
    print(f"Da riprocessare (incomplete o senza categoria): {len(rows_to_process)}\n")

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

        cat, title_it, summary_it, impact_it, strategy_it, raw_text = generate_full_analysis(
            client, title_en, summary_en, tickers
        )

        if not impact_it or not strategy_it:
            failed += 1
            print(f"  parsing parziale")
            if raw_text:
                print(f"  Prime 200 lettere: {raw_text[:200]}")
            continue

        try:
            supabase.table("news").update({
                "category": cat,
                "title_it": title_it,
                "summary_it": summary_it,
                "impact_it": impact_it,
                "strategy_it": strategy_it,
            }).eq("id", row["id"]).execute()
            updated += 1
            print(f"  ok [{cat}]")
        except Exception as e:
            print(f"  errore update: {e}")

    print(f"\nFatto: {updated}/{len(rows_to_process)} recuperate, {failed} fallite.")


if __name__ == "__main__":
    main()
