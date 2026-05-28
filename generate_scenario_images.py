"""
Genera un'immagine AI per gli scenari di `scenarios_live` che non ne hanno ancora una,
usando Pollinations.ai (GRATUITO, senza chiave API), la carica su Supabase Storage
(bucket pubblico) e salva l'URL nel campo image_url.

Di default elabora solo lo scenario PIÙ RECENTE senza immagine (quello del giorno).
Per il "backfill" di più scenari vecchi, aumenta MAX_IMAGES.

Servono solo:
  SUPABASE_URL, SUPABASE_KEY   -> già presenti (nessuna chiave a pagamento!)

Su Supabase serve:
  1) una colonna `image_url` (tipo text) nella tabella scenarios_live
  2) un bucket di Storage PUBBLICO chiamato come BUCKET_NAME (sotto)
"""

import os
import sys
import time
import urllib.parse
import requests
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Quanti scenari (più recenti senza immagine) elaborare per esecuzione.
# 1 = solo quello nuovo del giorno. Aumenta temporaneamente per il backfill.
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "1"))

# Nome del bucket pubblico su Supabase Storage da creare a mano una volta.
BUCKET_NAME = "scenario-images"

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠ SUPABASE_URL o SUPABASE_KEY non configurati")
    sys.exit(1)


def build_prompt(scenario):
    """Costruisce un prompt per un'immagine editoriale astratta dello scenario."""
    title = (scenario.get("title") or "").strip()
    desc = (scenario.get("description") or "").strip()
    severity = (scenario.get("severity") or "").strip()
    desc_short = desc[:200]

    palette = {
        "Estrema": "dark crimson and deep charcoal, ominous mood",
        "Severa": "amber and burnt orange, tense mood",
        "Alta": "warm orange and slate grey, cautious mood",
    }.get(severity, "cool blue and slate grey, measured mood")

    return (
        f"Editorial conceptual illustration of a financial market risk scenario. "
        f"Theme: {title}. Context: {desc_short}. "
        f"Cinematic, abstract, sophisticated, dramatic lighting, macro economy and markets feeling, "
        f"{palette}. No text, no words, no letters, no numbers, no charts, no logos. "
        f"Wide cinematic banner, premium financial magazine aesthetic."
    )


def seed_from_id(scenario_id):
    """Seed deterministico dall'id, così l'immagine è stabile e riproducibile."""
    s = str(scenario_id)
    return abs(sum((i + 1) * ord(ch) for i, ch in enumerate(s))) % 1000000


def generate_image_bytes(prompt, seed):
    """Chiama Pollinations.ai (gratuito) e ritorna i byte dell'immagine."""
    encoded = urllib.parse.quote(prompt, safe="")
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1280&height=720&nologo=true&model=flux&seed={seed}"
    )
    # Pollinations genera l'immagine al volo: può richiedere parecchi secondi.
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 200 and r.content and len(r.content) > 1000:
                return r.content
            last_err = f"HTTP {r.status_code}, {len(r.content) if r.content else 0} byte"
        except Exception as e:
            last_err = str(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Pollinations non ha risposto: {last_err}")


def upload_to_storage(supabase, scenario_id, image_bytes):
    """Carica i byte su Supabase Storage e ritorna l'URL pubblico."""
    path = f"scenario-{scenario_id}.jpg"
    storage = supabase.storage.from_(BUCKET_NAME)
    try:
        storage.upload(
            path,
            image_bytes,
            {"content-type": "image/jpeg", "upsert": "true"},
        )
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


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("=" * 60)
    print("GENERAZIONE IMMAGINI SCENARI (Pollinations - gratuito)")
    print("=" * 60)

    res = (
        supabase.table("scenarios_live")
        .select("id, title, description, severity, image_url, generated_at")
        .is_("image_url", "null")
        .order("generated_at", desc=True)
        .limit(MAX_IMAGES)
        .execute()
    )
    rows = res.data or []

    if not rows:
        print("Nessuno scenario da elaborare (tutti hanno già un'immagine).")
        return

    print(f"Scenari da elaborare: {len(rows)}\n")

    done = 0
    for s in rows:
        sid = s.get("id")
        title = s.get("title") or "—"
        print(f"  → [{sid}] {title[:50]}")
        try:
            prompt = build_prompt(s)
            img = generate_image_bytes(prompt, seed_from_id(sid))
            url = upload_to_storage(supabase, sid, img)
            if not url:
                print("    ✗ URL pubblico vuoto, salto")
                continue
            supabase.table("scenarios_live").update({"image_url": url}).eq("id", sid).execute()
            print(f"    ✓ immagine salvata: {url[:70]}...")
            done += 1
            time.sleep(1)
        except Exception as e:
            print(f"    ✗ errore: {str(e)[:200]}")

    print(f"\n✓ Completato. Immagini generate: {done}/{len(rows)}")


if __name__ == "__main__":
    main()
