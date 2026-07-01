# core/ — frozen snapshot

The files in `core/` are an **unmodified copy** of the proven Forager × HubSpot
enrichment service. They are the source of truth for HOW a single record is
enriched; this repo only adds the **backfill layer** (`bulk/`) that drives them
in a loop over existing records.

- Source repo:   devdoshi245/forager-hubspot
- Source branch: claude/gracious-goldberg-5k994y
- Source commit: 8d40923bc63a30347c79c75d4bb400d0ff150f8f
- Service build: v3.46

Copied modules: forager.py, hubspot.py, scoring.py, deepline.py, enrichment.py,
buyer_committee.py, httpclient.py, auth.py, alerts.py

## Rules
1. **Do not edit anything in `core/`.** All new logic lives in `bulk/`.
2. If the source service changes and we need the update, RE-COPY the files and
   bump the commit/build above. Never hand-patch.
3. The web layer (main.py / Flask / gunicorn) is intentionally NOT copied — this
   tool is a CLI job, not a web service.

## Refresh history
- v3.28 (948acd3) → v3.46 (8d40923): brought the snapshot up to the current
  production logic. This pulls in everything that shipped after v3.28, notably:
  * Company funding via Crustdata's company-DB screener (crunchbase total
    investment / last round / date) + numeric amount for the Tier-1 rule.
  * All Deepline waterfall fixes: 422 self-heal, target-domain email guard +
    prefer-domain selection, phone-extractor hardening (international/E.164,
    camelCase/cell keys), Upcell/Wiza/Datagma payload fixes.
  * ZeroBounce and Trestle verdict-reading fixes (read the real verdict, not the
    job status / bare key).
  * Region-aware phone waterfalls (NAMER/Europe/MEA/APAC/LATAM).
  * HubSpot duplicate-email 400 recovery (a person discovered under two titles
    no longer aborts the whole contact write).
  The backfill entry points are unchanged — `enrichment.enrich_company(id)`
  (firmographics + scoring + funding + Tier, no contact discovery) and
  `enrichment.handle_contact_webhook(id)` (Forager reveal + Deepline) — so
  `bulk/` needs no changes.
