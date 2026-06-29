# forager-hubspot-existing-enrichment

A **backfill / "catch-up" tool** that enriches the companies and contacts **already
sitting in HubSpot** — the ones the live webhook service never touched because they
existed before it went live (~8k companies, ~10k contacts).

It is **completely separate** from the live webhook service (`forager-hubspot`).
That service reacts to one new record at a time; this one sweeps the whole CRM as a
controlled batch job. The proven enrichment logic is reused as a **frozen, unedited
snapshot** in [`core/`](./core) — see [CORE_SNAPSHOT.md](./CORE_SNAPSHOT.md).

> ⚠️ **Nothing spends credits unless you pass `--execute`.** The default is a dry-run.
> Do not run a full `--execute` backfill until the credit estimate is approved.

---

## How it works (plain English)

1. **Gets the list** — asks HubSpot for every company / contact (free, read-only).
2. **Skips what's done** — a company already has a Forager Org ID, or a contact is
   already marked `forager_enriched`? Skip it. (This is also what makes the job
   *resumable* — re-running just continues with what's left.)
3. **Enriches the rest** — runs the exact same per-record logic the live service uses:
   * **Companies:** firmographics (Forager) + Claude ICP/logo scoring + Tier 1/2.
     No new contacts are created. ~0 Forager credits (Claude usage applies; needs
     `ANTHROPIC_API_KEY`).
   * **Contacts:** the full live pipeline — Forager reveal of email/phone
     (Workflow 2) **then** Deepline work-email + phone (Workflow 3). ~25 Forager
     credits each; Deepline runs only when `DEEPLINE_API_KEY` is set (otherwise the
     contact still gets the Forager reveal and the Deepline step is skipped).
4. **Stops safely** — when a record cap, a credit cap, or a Forager "out of credits"
   (HTTP 402) is hit.

---

## Commands

```bash
# 1) FREE estimate — count records + project cost. Run this first; share with the client.
python -m bulk.cli estimate            # companies + contacts, with a paste-ready summary
python -m bulk.cli estimate companies
python -m bulk.cli estimate contacts

# 2) Dry-run a backfill (still spends nothing — shows what it WOULD do)
python -m bulk.cli run companies
python -m bulk.cli run contacts

# 3) Execute (spends credits) — always start small and capped
python -m bulk.cli run contacts  --execute --max-records 5      # <-- recommended FIRST test (5 contacts)
python -m bulk.cli run companies --execute --max-records 50
python -m bulk.cli run contacts  --execute --max-credits 5000
python -m bulk.cli run contacts  --execute --max-records 100 --sleep 0.5
```

| Flag | Meaning |
|------|---------|
| `--execute` | Actually enrich. Without it, dry-run (no credits). |
| `--max-records N` | Stop after N records. |
| `--max-credits N` | Stop before estimated Forager credits exceed N (contacts). |
| `--sleep S` | Pause S seconds between records (rate-limit throttle). |

---

## Environment

Needs the **same credentials as the live service** (copy `.env.example` → `.env`, or
set them in the Railway job env):

- **Required:** `FORAGER_API_KEY`, `FORAGER_ACCOUNT_ID`, `HUBSPOT_TOKEN`
- **For company scoring (ICP + logo + Tier):** `ANTHROPIC_API_KEY` (without it,
  companies still get firmographics but skip scoring)
- **For the Deepline step on contacts:** `DEEPLINE_API_KEY` (+ the per-provider BYOK
  keys and credits configured in the Deepline dashboard). If unset, contacts still get
  the Forager reveal and the Deepline step is simply skipped.

---

## Running it (recommended: Railway one-off job)

This is a CLI job, **not** a web service — it has no Procfile process, so Railway
won't auto-run it. Run it on demand in the same project/environment as the live
service so it inherits the env vars:

```bash
railway run python -m bulk.cli estimate
railway run python -m bulk.cli run contacts --execute --max-credits 5000
```

Because already-enriched records are skipped, you can stop a run and re-run it later —
it picks up where it left off. No local checkpoint file needed.

---

## Development / tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests are fully **offline** — the HubSpot iterators and the core enrich functions are
stubbed, so the suite asserts the safety behaviour (dry-run spends nothing, caps stop
the run, 402 halts it, already-enriched records are skipped) without any network or
credits.

---

## Layout

```
core/    FROZEN snapshot of the live service (do not edit — see CORE_SNAPSHOT.md)
bulk/
  hubspot_list.py  enumerate all companies/contacts + "already enriched?" predicates
  estimate.py      dry-run counter + credit/$ projection (zero spend)
  runner.py        batch engine: stream → skip done → throttle → caps → 402-stop → progress
  cli.py           `python -m bulk.cli ...`
tests/   offline mock tests
```
