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


def _is_402(exc: Exception) -> bool:
    """True if this exception is Forager (or any upstream) reporting out-of-credits."""
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 402


class _Stats:
    def __init__(self):
        self.processed = 0      # records we attempted (or would attempt, in dry-run)
        self.enriched = 0       # core returned a non-skip result
        self.skipped = 0        # core decided there was nothing to do
        self.failed = 0         # core raised (non-402)
        self.est_credits = 0    # estimated Forager credits spent (contacts only)
        self.stop_reason = "completed — no records left"

    def as_dict(self) -> dict:
        return {
            "processed": self.processed, "enriched": self.enriched,
            "skipped": self.skipped, "failed": self.failed,
            "estimated_forager_credits_spent": self.est_credits,
            "stop_reason": self.stop_reason,
        }


def _run(object_label, iterator, enrich_fn, credits_each, execute,
         max_records, max_credits, sleep, log_every) -> dict:
    stats = _Stats()
    mode = "EXECUTE (spending credits)" if execute else "DRY-RUN (no credits)"
    logger.info("Backfill %s — %s | max_records=%s max_credits=%s sleep=%ss",
                object_label, mode, max_records, max_credits, sleep)

    for record_id, _props in iterator:
        # --- ceilings (checked BEFORE doing any work) ---
        if max_records is not None and stats.processed >= max_records:
            stats.stop_reason = f"hit max_records ({max_records})"
            break
        if max_credits is not None and stats.est_credits + credits_each > max_credits:
            stats.stop_reason = f"hit max_credits ({max_credits})"
            break

        stats.processed += 1

        if not execute:
            # Dry-run: count what we WOULD do; never call the credit-spending core.
            stats.est_credits += credits_each
            if stats.processed % log_every == 0:
                logger.info("[dry-run] %s: would process %d so far (~%d credits)",
                            object_label, stats.processed, stats.est_credits)
            continue

        try:
            result = enrich_fn(str(record_id))
            status = (result or {}).get("status")
            if status == "skipped":
                stats.skipped += 1
            else:
                stats.enriched += 1
                stats.est_credits += credits_each  # only count credits when work actually happened
        except requests.HTTPError as exc:
            if _is_402(exc):
                stats.stop_reason = "Forager out of credits (HTTP 402) — stopped"
                logger.error("Forager 402 on %s %s — stopping backfill", object_label, record_id)
                break
            stats.failed += 1
            logger.exception("Enrich failed for %s %s (continuing)", object_label, record_id)
        except Exception:  # noqa: BLE001 — one bad record must never kill the whole run
            stats.failed += 1
            logger.exception("Enrich failed for %s %s (continuing)", object_label, record_id)

        if stats.processed % log_every == 0:
            logger.info("%s: processed=%d enriched=%d skipped=%d failed=%d ~credits=%d",
                        object_label, stats.processed, stats.enriched,
                        stats.skipped, stats.failed, stats.est_credits)

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
        execute=execute, max_records=max_records, max_credits=None,
        sleep=sleep, log_every=log_every,
    )


def run_contacts(execute=False, max_records=None, max_credits=None, sleep=0.0, log_every=25) -> dict:
    """Backfill contacts (Forager reveal of email/phone). ~CREDITS_PER_CONTACT each.
    Workflow 3 / Deepline stays dormant unless DEEPLINE_API_KEY is set on the env —
    this changes nothing about that gating; we call the same per-contact core path."""
    return _run(
        "contacts",
        hubspot_list.iter_contacts(unenriched_only=True),
        enrichment.enrich_contact,
        credits_each=CREDITS_PER_CONTACT,
        execute=execute, max_records=max_records, max_credits=max_credits,
        sleep=sleep, log_every=log_every,
    )
