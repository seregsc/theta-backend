"""
generate_card_meta.py
─────────────────────────────────────────────────────────────────────────────
Genera, per IPO e Occasioni, i metadati delle CARD-COPERTINA stile Netflix:
  • card_title    → titolo editoriale breve e d'impatto (es. "Klarna pronta al lancio")
  • card_subtitle → frase contestuale sotto (es. "Il colosso BNPL svedese punta a Wall Street")
  • image_url     → immagine di copertina generata con Pollinations.ai (gratis),
                    caricata su Supabase Storage.

Tabelle:  ipos  +  opportunities
Colonne richieste su ENTRAMBE le tabelle (aggiungile se mancano):
    card_title    text
    card_subtitle text
    image_url     text         (se "opportunities" non ce l'ha già)
Bucket Storage pubblico:  card-images

Variabili d'ambiente:  SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY
Esecuzione consigliata (cron): ogni 6 ore.

NOTE
• Elabora solo le righe che NON hanno ancora card_title o image_url (idempotente).
• Le immagini sono illustrazioni concettuali editoriali (niente testo/loghi),
  così riempiono la card come una locandina senza problemi di copyright sui marchi.
• Se la generazione immagine fallisce, il titolo viene comunque salvato e la card
  userà il fallback gradiente+logo lato app.
"""

import os
import sys
import json
import time
import re
import urllib.parse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from supabase import create_client
import anthropic

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MODEL = "claude-haiku-4-5-20251001"
BUCKET_NAME = "card-images"

# Quante righe per tabella per esecuzione, e parallelismo immagini
MAX_ITEMS = int(os.environ.get("MAX_CARD_ITEMS", "20"))
PARALLEL = int(os.environ.get("PARALLEL", "5"))

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠ SUPABASE_URL o SUPABASE_KEY non configurati")
    sys.exit(1)


# ════════════════════════════════════════════════════════════════
# UTILITÀ
# ════════════════════════════════════════════════════════════════

def parse_json_loose(text):
    """Estrae il primo oggetto JSON da una risposta, tollerante a backtick/preamboli."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    # prova diretta
    try:
        return json.loads(text)
    except Exception:
        pass
    # prova a isolare le graffe
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def seed_from_id(any_id):
    s = str(any_id)
    return abs(sum((i + 1) * ord(ch) for i, ch in enumerate(s))) % 1000000


# ════════════════════════════════════════════════════════════════
# TITOLO EDITORIALE (AI)
# ════════════════════════════════════════════════════════════════

def make_titles_ipo(client, ipo):
    name = ipo.get("name") or ipo.get("ticker") or "Azienda"
    status = ipo.get("status") or ""
    sector = ipo.get("sector") or ""
    headline = ipo.get("headline") or ""
    summary = ipo.get("summary") or ipo.get("business") or ""

    prompt = f"""Sei l'editor di un'app finanziaria italiana. Scrivi il titolo di copertina per la card di una IPO, in stile rivista (come le copertine Netflix).

AZIENDA: {name}
SETTORE: {sector}
STATO IPO: {status}
DESCRIZIONE: {headline}. {summary[:300]}

Regole:
- card_title: MOLTO breve e d'impatto (max 28 caratteri, idealmente 2-3 parole). Può essere il nome dell'azienda o una frase incisiva. Esempi: "Klarna pronta al lancio", "Rumors su SpaceX", "Revolut verso la quotazione".
- card_subtitle: una frase contestuale (max 70 caratteri) che spiega cosa sta succedendo, concreta. Esempi: "Il colosso BNPL svedese punta a Wall Street", "Indiscrezioni su una quotazione da 150 miliardi".
- Italiano semplice, niente gergo, niente virgolette nei testi.

Rispondi SOLO con JSON: {{"card_title": "...", "card_subtitle": "..."}}"""

    return _ask_titles(client, prompt, fallback_title=name)


def make_titles_uv(client, opp):
    name = opp.get("name") or opp.get("ticker") or "Asset"
    ticker = opp.get("ticker") or ""
    cat = opp.get("category") or ""
    dd = opp.get("change_pct_1d") or opp.get("change_pct_7d") or opp.get("change_pct_30d") or ""
    headline = opp.get("title") or ""
    summary = opp.get("summary") or ""
    reason = opp.get("reason") or ""

    cat_label = {
        "crolli": "calo recente, possibile occasione di ingresso",
        "beneficiari": "possibile beneficiario di eventi di mercato",
        "sottovalutati": "ribasso moderato, possibile valore non riconosciuto",
    }.get(cat, "possibile occasione di valore")

    prompt = f"""Sei l'editor di un'app finanziaria italiana. Scrivi il titolo di copertina per la card di un'occasione d'investimento, in stile rivista (come le copertine Netflix).

ASSET: {name} ({ticker})
TIPO OCCASIONE: {cat_label}
VARIAZIONE: {dd}
CONTESTO: {headline}. {summary[:200]} {reason[:200]}

Regole:
- card_title: MOLTO breve e d'impatto (max 28 caratteri, idealmente 2-3 parole). Di solito il nome o ticker, oppure una frase incisiva. Esempi: "NVDA crolla del 5%", "Stellantis a P/E 4x", "Occasione su BMW".
- card_subtitle: una frase contestuale (max 70 caratteri) concreta che spiega perché guardarla. Esempi: "Perde il 5% in un giorno dopo la trimestrale", "Value play estremo nell'automotive premium".
- Italiano semplice, niente gergo, niente virgolette nei testi.

Rispondi SOLO con JSON: {{"card_title": "...", "card_subtitle": "..."}}"""

    return _ask_titles(client, prompt, fallback_title=name)


def _ask_titles(client, prompt, fallback_title):
    if not client:
        return {"card_title": fallback_title[:28], "card_subtitle": ""}
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = parse_json_loose(msg.content[0].text)
        if isinstance(parsed, dict) and parsed.get("card_title"):
            return {
                "card_title": str(parsed["card_title"])[:32].strip(),
                "card_subtitle": str(parsed.get("card_subtitle", ""))[:80].strip(),
            }
    except Exception as e:
        print(f"  [AI title error] {e}")
    return {"card_title": fallback_title[:28], "card_subtitle": ""}


# ════════════════════════════════════════════════════════════════
# IMMAGINE (Pollinations.ai → Supabase Storage)
# ════════════════════════════════════════════════════════════════

def build_image_prompt(kind, item):
    if kind == "ipo":
        subject = item.get("name") or item.get("ticker") or ""
        sector = item.get("sector") or "finance"
        theme = f"{subject}, {sector} industry, upcoming stock market listing, IPO, Wall Street"
        mood = "ambitious, forward-looking, premium"
    else:
        subject = item.get("name") or item.get("ticker") or ""
        cat = item.get("live_category") or ""
        mood = "dramatic" if "crollo" in cat or "crolli" in cat else "opportunity, hopeful"
        theme = f"{subject}, financial markets, investment opportunity"
    return (
        f"Editorial conceptual illustration for a premium financial app cover card. "
        f"Theme: {theme}. Mood: {mood}. "
        f"Cinematic, abstract, sophisticated, dramatic lighting, vertical poster composition, "
        f"premium financial magazine aesthetic, muted professional color palette. "
        f"No text, no words, no letters, no numbers, no charts, no logos, no brand marks."
    )


def generate_image_bytes(prompt, seed):
    encoded = urllib.parse.quote(prompt, safe="")
    # Formato verticale (poster) per le card-copertina
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=720&height=1080&nologo=true&model=flux&seed={seed}"
    )
    last_err = None
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=90)
            if r.status_code == 200 and r.content and len(r.content) > 1000:
                return r.content
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(3)
    raise RuntimeError(f"Pollinations ko: {last_err}")


def upload_to_storage(supabase, kind, item_id, image_bytes):
    path = f"{kind}-{item_id}.jpg"
    storage = supabase.storage.from_(BUCKET_NAME)
    try:
        storage.upload(path, image_bytes, {"content-type": "image/jpeg", "upsert": "true"})
    except Exception as e:
        msg = str(e)
        if "Duplicate" in msg or "already exists" in msg:
            try:
                storage.remove([path])
            except Exception:
                pass
            storage.upload(path, image_bytes, {"content-type": "image/jpeg"})
        else:
            raise
    public = storage.get_public_url(path)
    if isinstance(public, dict):
        public = public.get("publicUrl") or public.get("public_url") or ""
    return public


# ════════════════════════════════════════════════════════════════
# PIPELINE PER TABELLA
# ════════════════════════════════════════════════════════════════

def process_table(supabase, client, table, kind, select_cols):
    print("=" * 60)
    print(f"TABELLA: {table}  (tipo card: {kind})")
    print("=" * 60)

    # Prende le righe senza card_title OPPURE senza image_url
    try:
        res = (
            supabase.table(table)
            .select(select_cols)
            .limit(500)
            .execute()
        )
    except Exception as e:
        print(f"  ⚠ Impossibile leggere {table}: {e}")
        return

    rows = res.data or []
    # Filtra lato client: status attivo (se la colonna esiste) e card incompleta
    todo = []
    for r in rows:
        status = (r.get("status") or "").lower()
        if status in ("expired", "archived"):
            continue
        if not r.get("card_title") or not r.get("image_url"):
            todo.append(r)
    todo = todo[:MAX_ITEMS]

    if not todo:
        print("  Niente da elaborare.\n")
        return

    print(f"  Righe da elaborare: {len(todo)}\n")

    # 1) Titoli (sequenziale: chiamate AI brevi)
    for r in todo:
        if not r.get("card_title"):
            titles = make_titles_ipo(client, r) if kind == "ipo" else make_titles_uv(client, r)
            r["_titles"] = titles

    # 2) Immagini (parallelo)
    def work(r):
        rid = r.get("id")
        label = (r.get("name") or r.get("ticker") or str(rid))[:40]
        update = {}
        if r.get("_titles"):
            update["card_title"] = r["_titles"]["card_title"]
            update["card_subtitle"] = r["_titles"]["card_subtitle"]
        if not r.get("image_url"):
            try:
                prompt = build_image_prompt(kind, r)
                img = generate_image_bytes(prompt, seed_from_id(rid))
                url = upload_to_storage(supabase, kind, rid, img)
                if url:
                    update["image_url"] = url
            except Exception as e:
                print(f"  ✗ immagine [{rid}] {label} — {str(e)[:90]}")
        if update:
            try:
                supabase.table(table).update(update).eq("id", rid).execute()
                return (rid, label, True, list(update.keys()))
            except Exception as e:
                return (rid, label, False, str(e)[:90])
        return (rid, label, False, "nessun aggiornamento")

    done = 0
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futures = [ex.submit(work, r) for r in todo]
        for f in as_completed(futures):
            rid, label, ok, info = f.result()
            if ok:
                done += 1
                print(f"  ✓ [{rid}] {label} → {', '.join(info)}")
            else:
                print(f"  · [{rid}] {label} — {info}")

    print(f"\n  ✓ {table}: aggiornate {done}/{len(todo)} righe.\n")


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
    if not client:
        print("⚠ ANTHROPIC_API_KEY non configurata: genero solo le immagini, i titoli useranno il nome.")

    # Verifica che il bucket immagini esista (altrimenti le immagini falliranno tutte)
    try:
        test = supabase.storage.from_(BUCKET_NAME).list()
        print(f"✓ Bucket '{BUCKET_NAME}' raggiungibile.")
    except Exception as e:
        print(f"⚠ Bucket '{BUCKET_NAME}' NON raggiungibile: {str(e)[:140]}")
        print(f"  → Crea su Supabase un bucket PUBBLICO chiamato '{BUCKET_NAME}'. I titoli verranno comunque generati.")

    # IPO
    process_table(
        supabase, client,
        table="ipos_live",
        kind="ipo",
        select_cols="id, ticker, name, sector, status, headline, summary, business, card_title, image_url",
    )

    # Occasioni
    process_table(
        supabase, client,
        table="opportunities",
        kind="uv",
        select_cols="id, ticker, name, category, status, title, summary, reason, catalyst, change_pct_1d, change_pct_7d, change_pct_30d, expected_move, card_title, image_url",
    )

    print("✓ Completato.")


if __name__ == "__main__":
    main()
