"""
insert_test_alert.py
Inserisce UNA allerta live di TEST per vedere come appare e si comporta in Theta
(banner lampeggiante sulla landing + pagina Allerte Live).

Genera client_actions sui clienti REALI di ogni utente, così il layout è popolato
in modo realistico. NON usa l'AI: è tutto hardcoded, costo zero, immediato.

Per RIMUOVERE l'allerta di test dopo i controlli:
  - su Supabase SQL Editor:  DELETE FROM live_alerts WHERE trigger_news_id = -1;
  - oppure rilancia con CLEANUP=1 (cancella solo le allerte di test, non quelle vere)

Variabili: SUPABASE_URL, SUPABASE_KEY
"""

import os
import sys
from datetime import datetime, timezone
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
CLEANUP = os.environ.get("CLEANUP", "0") == "1"

TEST_MARKER = -1  # id news fittizio (i veri id sono positivi), per riconoscere/rimuovere i test

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠ SUPABASE_URL o SUPABASE_KEY non configurati")
    sys.exit(1)


def main():
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # CLEANUP: rimuove solo le allerte di test
    if CLEANUP:
        supabase.table("live_alerts").delete().eq("trigger_news_id", TEST_MARKER).execute()
        print("✓ Allerte di test rimosse.")
        return

    # Utenti con clienti
    clients_res = supabase.table("clients").select("*").execute()
    all_clients = clients_res.data or []
    user_ids = list({c["user_id"] for c in all_clients if c.get("user_id")})
    if not user_ids:
        print("⚠ Nessun utente con clienti trovato.")
        return
    print(f"Utenti con clienti: {len(user_ids)}")

    now = datetime.now(timezone.utc).isoformat()
    created = 0

    for uid in user_ids:
        user_clients = [c for c in all_clients if c.get("user_id") == uid]

        # Costruisce client_actions di esempio sui primi 3 clienti reali
        client_actions = []
        for c in user_clients[:3]:
            name = c.get("name") or "Cliente"
            client_actions.append({
                "client_id": c.get("id"),
                "client_name": name,
                "severity": "HIGH",
                "current_exposure": "Tech 34% (TSM 6%, NVDA 11%), Semiconduttori sovrappesati",
                "estimated_loss_eur": -28500,
                "estimated_loss_pct": -5.6,
                "actions": [
                    {
                        "type": "vendere",
                        "text": "Ridurre TSM dal 6% al 2% del portafoglio",
                        "amount_eur": -20400,
                        "reason": "Esposizione diretta a Taiwan, epicentro dello shock",
                    },
                    {
                        "type": "comprare",
                        "text": "Aprire posizione difesa europea (Rheinmetall) al 3%",
                        "amount_eur": 15000,
                        "reason": "Settore beneficiario in scenari di escalation geopolitica",
                    },
                    {
                        "type": "monitorare",
                        "text": "Tenere d'occhio l'oro come copertura, valutare +2% se sale la tensione",
                        "amount_eur": 0,
                        "reason": "Bene rifugio classico, da incrementare se l'evento peggiora",
                    },
                ],
                "post_action_loss_eur": -6800,
                "notes": "Profilo growth: le azioni preservano la tesi di lungo periodo ma riducono il rischio idiosincratico su Taiwan.",
            })

        row = {
            "user_id": uid,
            "status": "active",
            "severity": "HIGH",
            "trigger_news_id": TEST_MARKER,
            "title": "[TEST] Escalation militare Cina-Taiwan: blocco navale in corso",
            "summary": (
                "Questa è un'allerta di TEST per verificare il layout. "
                "La Cina ha avviato un blocco navale attorno a Taiwan, schierando la marina "
                "nello Stretto e bloccando di fatto il traffico commerciale. È l'escalation più "
                "grave dell'ultimo decennio. I mercati asiatici aprono in forte ribasso e il rischio "
                "è una paralisi della catena di fornitura globale dei semiconduttori: Taiwan, tramite "
                "TSMC, produce circa il 65% dei chip mondiali e oltre il 90% di quelli più avanzati. "
                "Perché è grave: un blocco prolungato fermerebbe la produzione di chip per auto, "
                "smartphone, data center e AI, con effetti a catena su quasi tutti i settori. "
                "Nelle prossime 24-72h è atteso un forte aumento della volatilità, vendite su tech e "
                "semiconduttori, e una corsa verso beni rifugio (oro, treasury) e titoli della difesa."
            ),
            "possible_next_events": [
                {
                    "title": "Taiwan chiede aiuto militare agli USA",
                    "description": "Probabile richiesta formale di supporto a Washington. Una risposta americana diretta aumenterebbe drasticamente il rischio di confronto tra grandi potenze.",
                    "likelihood": "Alta",
                },
                {
                    "title": "Sanzioni economiche reciproche USA-Cina",
                    "description": "Possibile blocco delle esportazioni di chip e contromisure cinesi sulle terre rare, con ulteriore frammentazione delle catene di fornitura.",
                    "likelihood": "Alta",
                },
                {
                    "title": "Intervento delle banche centrali",
                    "description": "Fed e BCE potrebbero intervenire con liquidità di emergenza se la volatilità minaccia la stabilità finanziaria.",
                    "likelihood": "Media",
                },
                {
                    "title": "Picco del prezzo dell'oro e del petrolio",
                    "description": "Corsa ai beni rifugio e timori sulle rotte energetiche asiatiche potrebbero spingere oro e greggio su nuovi massimi.",
                    "likelihood": "Media",
                },
            ],
            "sources": ["Reuters", "Bloomberg"],
            "affected_sectors": ["ai", "semis", "auto", "tech"],
            "beneficiary_sectors": ["defense", "gold", "energy"],
            "triggered_assets_negative": ["TSM", "NVDA", "AAPL", "ASML"],
            "triggered_assets_positive": ["LMT", "RHM.DE", "GLD"],
            "book_impact": {
                "exposure_at_risk_eur": 420000,
                "exposure_at_risk_pct": -3.2,
                "worst_case_eur": -680000,
                "best_case_after_actions_eur": -85000,
            },
            "book_level_actions": [
                "Ridurre l'esposizione ai semiconduttori taiwanesi del 50-70% nelle prossime 48h",
                "Aumentare la difesa europea al 4-6% per i clienti che ne sono sprovvisti",
                "Incrementare oro/beni rifugio del 2-3% sui profili difensivi",
                "Aumentare la liquidità al 10-15% per cogliere occasioni post-correzione",
            ],
            "client_actions": client_actions,
            "triggered_at": now,
            "generated_at": now,
        }

        try:
            supabase.table("live_alerts").insert(row).execute()
            created += 1
            print(f"  ✓ Allerta test inserita per user {uid[:8]} ({len(client_actions)} clienti)")
        except Exception as e:
            print(f"  ✗ errore per user {uid[:8]}: {str(e)[:200]}")

    print(f"\n✓ Completato. Allerte di test create: {created}")
    print("Per rimuoverle: rilancia con CLEANUP=1 oppure esegui in SQL:")
    print(f"  DELETE FROM live_alerts WHERE trigger_news_id = {TEST_MARKER};")


if __name__ == "__main__":
    main()
