"""
Genera immagini AI per le news che non ne hanno ancora una (campo ai_image_url),
usando Pollinations.ai (GRATUITO). Le carica su Supabase Storage e salva l'URL.

VERSIONE VELOCE: genera le immagini IN PARALLELO (più news insieme), così
le news appena caricate ottengono l'immagine in pochi secondi invece che in minuti.
Elabora prima le PIÙ RECENTI.

Servono: SUPABASE_URL, SUPABASE_KEY
Su Supabase: colonna `ai_image_url` (text) nella tabella news + bucket pubblico `news-images`.
"""

import os
import sys
import time
import urllib.parse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Quante news elaborare per esecuzione (le più recenti senza immagine).
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "12"))
# Quante generare in parallelo.
PARALLEL = int(os.environ.get("PARALLEL", "6"))

BUCKET_NAME = "news-images"

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠ SUPABASE_URL o SUPABASE_KEY non configurati")
    sys.exit(1)


def build_prompt(news):
    title = (news.get("title_it") or news.get("title") or "").strip()
    summary = (news.get("summary_it") or news.get("summary") or "").strip()
    category = (news.get("category") or "").strip()
    summary_short = summary[:200]
    theme = {
        "azioni": "stock market, corporate finance",
        "macroeconomia": "macro economy, central banks, global finance",
        "geopolitica": "geopolitics, world map, international tension",
        "materie_prime": "commodities, energy, raw materials",
    }.get(category, "financial markets")
    return (
        f"Editorial conceptual illustration for a financial news article. "
        f"Theme: {title}. Context: {summary_short}. Field: {theme}. "
        f"Cinematic, abstract, sophisticated, dramatic lighting, premium financial magazine aesthetic, "
        f"muted professional color palette. "
        f"No text, no words, no letters, no numbers, no charts, no logos. "
        f"Wide cinematic banner composition."
    )


def seed_from_id(news_id):
    s = str(news_id)
    return abs(sum((i + 1) * ord(ch) for i, ch in enumerate(s))) % 1000000


def generate_image_bytes(prompt, seed):
    encoded = urllib.parse.quote(prompt, safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1280&height=720&nologo=true&model=flux&seed={seed}"
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


def upload_to_storage(supabase, news_id, image_bytes):
    path = f"news-{news_id}.jpg"
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


def process_one(supabase, n):
    nid = n.get("id")
    title = (n.get("title_it") or n.get("title") or "—")[:50]
    try:
        prompt = build_prompt(n)
        img = generate_image_bytes(prompt, seed_from_id(nid))
        url = upload_to_storage(supabase, nid, img)
        if not url:
            return (nid, title, False, "URL vuoto")
        supabase.table("news").update({"ai_image_url": url}).eq("id", nid).execute()
        return (nid, title, True, "")
    except Exception as e:
        return (nid, title, False, str(e)[:120])


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("=" * 60)
    print(f"IMMAGINI NEWS (parallelo x{PARALLEL}, max {MAX_IMAGES})")
    print("=" * 60)

    res = (
        supabase.table("news")
        .select("id, title_it, title, summary_it, summary, category, ai_image_url, created_at")
        .not_.is_("title_it", "null")
        .is_("ai_image_url", "null")
        .order("created_at", desc=True)
        .limit(MAX_IMAGES)
        .execute()
    )
    rows = res.data or []
    if not rows:
        print("Nessuna news da elaborare.")
        return

    print(f"News da elaborare: {len(rows)}\n")
    done = 0
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futures = [ex.submit(process_one, supabase, n) for n in rows]
        for f in as_completed(futures):
            nid, title, ok, err = f.result()
            if ok:
                done += 1
                print(f"  ✓ [{nid}] {title}")
            else:
                print(f"  ✗ [{nid}] {title} — {err}")

    print(f"\n✓ Completato. Immagini generate: {done}/{len(rows)}")


if __name__ == "__main__":
    main()
