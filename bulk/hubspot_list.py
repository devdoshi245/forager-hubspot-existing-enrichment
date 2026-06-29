"""
bulk.hubspot_list
-----------------
The ONE building block the original service never had: enumerate EVERY company
and contact already in HubSpot (the webhook service only ever fetched one record
by id). Everything else is reused from core/.

We use HubSpot's LIST endpoint (GET /crm/v3/objects/{type}?after=<cursor>) rather
than the Search endpoint on purpose:
  * Search caps results at 10,000 — contacts (~10k, and growing) would silently
    truncate. The list endpoint has no such cap.
  * It streams in pages of 100 via an opaque cursor, so we never hold all 18k
    records in memory.

"Already enriched?" is decided the SAME way the core skip-logic decides it, so the
work-set here lines up with what enrich_company / enrich_contact would actually do:
  * company  -> has a forager_org_id  (we've matched + written it before)
  * contact  -> forager_enriched == "true"  (we've revealed + paid for it before)

Reading records costs nothing (HubSpot API, no Forager/Claude credits).
"""

import os

import httpclient  # from core/ (see bulk/__init__.py path shim)

HUBSPOT_BASE = "https://api.hubapi.com"
_SESSION = httpclient.make_session()

# Page size for the list endpoint (HubSpot max is 100).
_PAGE_LIMIT = 100

# Only the properties we need to decide "already enriched?" — keeps payloads small.
_COMPANY_PROPS = ["domain", "name", "forager_org_id", "icp_match_score"]
_CONTACT_PROPS = ["firstname", "lastname", "forager_person_id", "forager_enriched", "email"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('HUBSPOT_TOKEN')}",
        "Content-Type": "application/json",
    }


def _iter_objects(object_type: str, properties: list[str]):
    """Yield every record of `object_type` as {"id", "properties": {...}}, paging
    through the list endpoint until HubSpot stops handing back a cursor."""
    after = None
    while True:
        params = {"limit": _PAGE_LIMIT, "properties": ",".join(properties)}
        if after:
            params["after"] = after
        resp = _SESSION.get(
            f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}",
            headers=_headers(), params=params, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for record in data.get("results", []):
            yield record
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break


# ---------------------------------------------------------------------------
# "Already enriched?" predicates — mirror the core skip-logic.
# ---------------------------------------------------------------------------
def company_is_enriched(props: dict) -> bool:
    """A company we've already matched in Forager carries a forager_org_id."""
    return bool((props.get("forager_org_id") or "").strip())


def contact_is_enriched(props: dict) -> bool:
    """A contact we've already revealed (and paid for) is stamped forager_enriched."""
    return (props.get("forager_enriched") or "").strip().lower() == "true"


# ---------------------------------------------------------------------------
# Public iterators — yield (id, props) for the records the backfill should touch.
# ---------------------------------------------------------------------------
def iter_companies(unenriched_only: bool = True):
    for rec in _iter_objects("companies", _COMPANY_PROPS):
        props = rec.get("properties", {}) or {}
        if unenriched_only and company_is_enriched(props):
            continue
        yield rec.get("id"), props


def iter_contacts(unenriched_only: bool = True):
    for rec in _iter_objects("contacts", _CONTACT_PROPS):
        props = rec.get("properties", {}) or {}
        if unenriched_only and contact_is_enriched(props):
            continue
        yield rec.get("id"), props


# ---------------------------------------------------------------------------
# Counting (for the dry-run estimate). One full read-only pass; no credits spent.
# ---------------------------------------------------------------------------
def count_companies() -> dict:
    total = enriched = 0
    for _id, props in iter_companies(unenriched_only=False):
        total += 1
        if company_is_enriched(props):
            enriched += 1
    return {"total": total, "enriched": enriched, "remaining": total - enriched}


def count_contacts() -> dict:
    total = enriched = 0
    for _id, props in iter_contacts(unenriched_only=False):
        total += 1
        if contact_is_enriched(props):
            enriched += 1
    return {"total": total, "enriched": enriched, "remaining": total - enriched}
