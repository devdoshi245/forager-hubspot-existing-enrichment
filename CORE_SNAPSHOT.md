# core/ — frozen snapshot

The files in `core/` are an **unmodified copy** of the proven Forager × HubSpot
enrichment service. They are the source of truth for HOW a single record is
enriched; this repo only adds the **backfill layer** (`bulk/`) that drives them
in a loop over existing records.

- Source repo:   devdoshi245/forager-hubspot
- Source branch: claude/forager-hubspot-enrichment-k8sgr1
- Source commit: 948acd33540f39d5b635f03881f4b4a40c980140
- Service build: v3.28

Copied modules: forager.py, hubspot.py, scoring.py, deepline.py, enrichment.py,
buyer_committee.py, httpclient.py, auth.py, alerts.py

## Rules
1. **Do not edit anything in `core/`.** All new logic lives in `bulk/`.
2. If the source service changes and we need the update, RE-COPY the files and
   bump the commit/build above. Never hand-patch.
3. The web layer (main.py / Flask / gunicorn) is intentionally NOT copied — this
   tool is a CLI job, not a web service.
