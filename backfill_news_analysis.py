"""
Script di backfill: trova news nel database che hanno title_it ma NON impact_it o strategy_it,
e rigenera l'analisi completa via Claude. Eseguilo una tantum dopo il deploy del nuovo prompt.
"""
import os
from supabase import create_client
from anthropic import Anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-4-5"


def generate_full_analysis(client, title_en, summary_en, tickers_csv):
    tickers_str = tickers_csv if tickers_csv else "—"
    prompt = f"""Sei un analista finanziario senior italiano che scrive per consulenti professionisti. Riceverai una notizia in inglese e devi produrre un'analisi completa in italiano fluido e professionale, da pubblicare sull'app Theta.

REGOLE GENERALI
- Scrivi in italiano corretto, sintassi naturale, lessico finanziario professionale.
- Sii fattuale: usa SOLO informazioni presenti nel testo originale. Non inventare numeri, date, eventi, o citazioni.
- Tono: neutro, informativo, mai sensazionalistico.
- Non dare consigli espliciti di acquisto/vendita.

INPUT
Titolo originale (EN): {title_en}
Sommario originale (EN): {summary_en or "—"}
Ticker citati: {tickers_str}

OUTPUT — rispondi ESATTAMENTE in questo formato, con i 4 blocchi etichettati TITOLO, SOMMARIO, IMPATTO, STRATEGIA. Niente preamboli, niente conclusioni:

TITOLO: <titolo in italiano, max 110 caratteri. Riformulato, non tradotto letterale. Informativo e neutro.>

SOMMARIO: <riassunto sostanzioso in italiano, 4-6 frasi (600-900 caratteri). Includi: il contesto della notizia, i numeri/dati chiave presenti nell'originale, gli attori coinvolti, il significato per il mercato. Scrivi come un articolo giornalistico breve, non come bullet point. Se l'originale è povero di dettagli, espandi con il contesto settoriale plausibile (es. "il comparto X è sotto pressione per…") ma SENZA inventare fatti specifici.>

IMPATTO: <analisi di 3-4 frasi (350-550 caratteri) sull'impatto previsto. Indica: quali settori/asset sono direttamente coinvolti, in che direzione potrebbero muoversi, quali correlazioni di mercato sono rilevanti (es. tassi, dollaro, oro, oil). Concreto e ragionato.>

STRATEGIA: <suggerimento operativo di 3-4 frasi (350-550 caratteri) per un consulente che gestisce portafogli. Cosa potrebbe fare: monitorare un asset specifico, considerare riallocazione settoriale, valutare hedging, attendere conferme tecniche. Mai categorico ("compra X"), sempre condizionale ("se il segnale si conferma…", "per clienti con profilo Y…"). Cita ticker concreti se rilevanti.>"""

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
        for line in text.
