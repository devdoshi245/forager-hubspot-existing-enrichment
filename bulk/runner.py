"""
bulk.runner
-----------
The batch engine. Streams the not-yet-enriched records and runs the PROVEN core
enrichment on each one — we do not reinvent enrichment, we just drive it in a loop
with the safety rails a long backfill needs:

  * DRY-RUN BY DEFAULT — nothing spends a credit unless execute=True.
  * Hard ceilings — max_records and max_credits stop the run cleanly.
  * 402 stop — the moment Forager reports "out of credits", we stop and report,
    rather than hammering a dead account.
  * Throttle — a per-record sleep keeps us under HubSpot/Forager rate limits.
  * Resumable — because we only ever pull records still missing the enriched flag,
    re-running simply continues where a previous run stopped (the flag in HubSpot is
    the durable checkpoint; an ephemeral Railway one-off needs no local state).
  * Progress — a running tally is logged so you can watch it in Railway logs.

Credit accounting is an ESTIMATE (Forager bills the real amount): each successful
contact reveal counts CREDITS_PER_CONTACT toward the max_credits ceiling. Companies
count ~0 Forager credits (their cost is Claude usage), so use max_records for the
company job.
"""

import logging
import time

import requests

import enrichment  # from core/

from bulk import hubspot_list
from bulk.estimate import CREDITS_PER_CONTACT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HARD SAFETY CEILING — the absolute maximum number of records enriched in a
# SINGLE run, no matter what --max-records says (or even if it's omitted).
# This is a malfunction backstop: if anything loops or misbehaves, it can burn at
# most this many records' worth of credits before stopping — NOT the whole CRM.
# The job still completes the full backfill: re-run it and it resumes (already-
# enriched records are skipped), just 5 at a time. Raise this for client production
# runs once the tool is trusted.
HARD_MAX_PER_RUN = 5


def _capped(requested):
    """Clamp a requested max_records down to HARD_MAX_PER_RUN. A missing flag
    (None) also becomes the ceiling, so an unbounded run is impossible."""
    if requested is not None and requested > HARD_MAX_PER_RUN:
        logger.warning("Requested max_records=%s exceeds the hard safety cap; clamping to %d.",
                       requested, HARD_MAX_PER_RUN)
        return HARD_MAX_PER_RUN
    return HARD_MAX_PER_RUN if requested is None else requested


def _is_402(exc: Exception) -> bool:
    """True if this exception is Forager (or any upstream) reporting out-of-credits."""
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 402


def _record_label(props: dict) -> str:
    """A human-readable tag for a record so you can see WHICH one was processed:
    'iTechNotion (itechnotion.com)' for a company, 'Jane Doe (jane@acme.com)' for a
    contact. Falls back to whatever identifying field is present."""
    props = props or {}
    name = (props.get("name")
            or " ".join(p for p in (props.get("firstname"), props.get("lastname")) if p)
            or "").strip()
    ident = (props.get("domain") or props.get("email") or "").strip()
    if name and ident:
        return f"{name} ({ident})"
    return name or ident or "(no name/domain on record)"


class _Stats:
    def __init__(self):
        self.processed = 0      # records we attempted (or would attempt, in dry-run)
        self.enriched = 0       # core returned a non-skip result
        self.skipped = 0        # core decided there was nothing to do
        self.failed = 0         # core raised (non-402)
        self.est_credits = 0    # estimated Forager credits spent (contacts only)
        self.records = []       # [{id, label, outcome}] — exactly which records were touched
        self.stop_reason = "completed — no records left"

    def as_dict(self) -> dict:
        return {
            "processed": self.processed, "enriched": self.enriched,
            "skipped": self.skipped, "failed": self.failed,
            "estimated_forager_credits_spent": self.est_credits,
            "records": self.records,  # which exact records were processed, with outcome
            "stop_reason": self.stop_reason,
        }


def _run(object_label, iterator, enrich_fn, credits_each, execute,
         max_records, max_credits, sleep, log_every) -> dict:
    stats = _Stats()
    mode = "EXECUTE (spending credits)" if execute else "DRY-RUN (no credits)"
    logger.info("Backfill %s — %s | max_records=%s max_credits=%s sleep=%ss",
                object_label, mode, max_records, max_credits, sleep)

    for record_id, props in iterator:
        # --- ceilings (checked BEFORE doing any work) ---
        if max_records is not None and stats.processed >= max_records:
            stats.stop_reason = f"hit max_records ({max_records})"
            break
        if max_credits is not None and stats.est_credits + credits_each > max_credits:
            stats.stop_reason = f"hit max_credits ({max_credits})"
            break

        stats.processed += 1
        label = _record_label(props)

        if not execute:
            # Dry-run: count what we WOULD do; never call the credit-spending core.
            stats.est_credits += credits_each
            stats.records.append({"id": record_id, "label": label, "outcome": "would-process"})
            logger.info("[dry-run] %s #%d WOULD process: %s — %s",
                        object_label, stats.processed, record_id, label)
            continue

        outcome = "enriched"
        try:
            result = enrich_fn(str(record_id))
            if isinstance(result, dict) and result.get("error"):
                stats.failed += 1
                outcome = "error"
                logger.warning("%s %s (%s) returned error: %s",
                               object_label, record_id, label, result.get("error"))
            elif (result or {}).get("status") == "skipped":
                stats.skipped += 1
                outcome = "skipped"
            else:
                stats.enriched += 1
                stats.est_credits += credits_each  # only count credits when work actually happened
        except requests.HTTPError as exc:
            if _is_402(exc):
                stats.stop_reason = "Forager out of credits (HTTP 402) — stopped"
                stats.records.append({"id": record_id, "label": label, "outcome": "stopped-402"})
                logger.error("Forager 402 on %s %s (%s) — stopping backfill",
                             object_label, record_id, label)
                break
            stats.failed += 1
            outcome = "error"
            logger.exception("Enrich failed for %s %s (%s) (continuing)", object_label, record_id, label)
        except Exception:  # noqa: BLE001 — one bad record must never kill the whole run
            stats.failed += 1
            outcome = "error"
            logger.exception("Enrich failed for %s %s (%s) (continuing)", object_label, record_id, label)

        # Always record + log WHICH record was touched and what happened to it.
        stats.records.append({"id": record_id, "label": label, "outcome": outcome})
        logger.info("%s #%d %s: %s — %s",
                    object_label, stats.processed, outcome.upper(), record_id, label)

        if sleep:
            time.sleep(sleep)

    out = stats.as_dict()
    out["mode"] = "execute" if execute else "dry-run"
    out["object"] = object_label
    logger.info("Backfill %s DONE — %s", object_label, out)
    return out


def run_companies(execute=False, max_records=None, sleep=0.0, log_every=25) -> dict:
    """Backfill company fields only (firmographics + Claude scoring + Tier). No contact
    discovery — that is deliberately out of scope for this job. ~0 Forager credits."""
    return _run(
        "companies",
        hubspot_list.iter_companies(unenriched_only=True),
        enrichment.enrich_company,
        credits_each=0,
        execute=execute, max_records=_capped(max_records), max_credits=None,
        sleep=sleep, log_every=log_every,
    )


def _enrich_contact_full(contact_id: str) -> dict:
    """Run the FULL per-contact pipeline exactly as the live contact webhook does:
    Workflow 2 (Forager reveal of email/phone) followed by Workflow 3 (Deepline
    work-email + phone). Workflow 3 auto-activates only when DEEPLINE_API_KEY is set,
    identical to production — with it unset, the contact still gets the Forager reveal
    and WF3 is a no-op.

    handle_contact_webhook returns the WF2 result directly when Deepline is off, or
    {"workflow2": ..., "workflow3": ...} when it's on. We normalise that to a single
    dict carrying the WF2 status so the runner can tell enriched/skipped/error apart."""
    res = enrichment.handle_contact_webhook(contact_id)
    wf2 = res.get("workflow2", res) if isinstance(res, dict) else {}
    if isinstance(wf2, dict) and wf2.get("error"):
        return {"error": wf2["error"], "detail": res}
    return {"status": (wf2 or {}).get("status"), "detail": res}


def run_contacts(execute=False, max_records=None, max_credits=None, sleep=0.0, log_every=25) -> dict:
    """Backfill contacts via the full live pipeline: Forager reveal (Workflow 2) then
    Deepline (Workflow 3). ~CREDITS_PER_CONTACT Forager credits each; Deepline provider
    credits (BYOK) are separate and only incurred when DEEPLINE_API_KEY is configured."""
    return _run(
        "contacts",
        hubspot_list.iter_contacts(unenriched_only=True),
        _enrich_contact_full,
        credits_each=CREDITS_PER_CONTACT,
        execute=execute, max_records=_capped(max_records), max_credits=max_credits,
        sleep=sleep, log_every=log_every,
    )
