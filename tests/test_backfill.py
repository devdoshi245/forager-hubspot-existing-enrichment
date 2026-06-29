"""
Offline tests for the backfill layer. No network, no credits: the HubSpot iterators
and the core enrich functions are stubbed. We assert the SAFETY behaviour — dry-run
spends nothing, ceilings stop the run, a 402 halts it, already-enriched records are
skipped, and the estimate counts the right work-set.
"""

import requests

# Importing bulk first puts core/ on sys.path (bulk/__init__.py), so the core
# modules the runner reaches (enrichment, forager, ...) resolve by bare name.
from bulk import hubspot_list, estimate, runner


# --------------------------------------------------------------------------- #
# Fixtures: fake HubSpot record streams.
# --------------------------------------------------------------------------- #
# (id, props) — the shape iter_companies / iter_contacts YIELD (used by runner tests).
def _company(i, org_id=""):
    return (str(i), {"domain": f"co{i}.com", "name": f"Co{i}", "forager_org_id": org_id})


def _contact(i, enriched=False):
    return (str(i), {"firstname": f"F{i}", "lastname": f"L{i}",
                     "forager_enriched": "true" if enriched else "",
                     "forager_person_id": str(1000 + i)})


# Raw HubSpot record dict — the shape _iter_objects yields (used to stub the API layer).
def _raw(record_id, props):
    return {"id": record_id, "properties": props}


def _raw_company(i, org_id=""):
    return _raw(*_company(i, org_id))


def _raw_contact(i, enriched=False):
    return _raw(*_contact(i, enriched))


# --------------------------------------------------------------------------- #
# Predicates / iterators
# --------------------------------------------------------------------------- #
def test_enriched_predicates():
    assert hubspot_list.company_is_enriched({"forager_org_id": "abc"}) is True
    assert hubspot_list.company_is_enriched({"forager_org_id": ""}) is False
    assert hubspot_list.contact_is_enriched({"forager_enriched": "true"}) is True
    assert hubspot_list.contact_is_enriched({"forager_enriched": ""}) is False
    assert hubspot_list.contact_is_enriched({"forager_enriched": "TRUE"}) is True


def test_iter_filters_unenriched(monkeypatch):
    raw = [_raw_company(1), _raw_company(2, org_id="X"), _raw_company(3)]
    monkeypatch.setattr(hubspot_list, "_iter_objects", lambda *a, **k: iter(raw))
    only_new = list(hubspot_list.iter_companies(unenriched_only=True))
    assert [cid for cid, _ in only_new] == ["1", "3"]
    everything = list(hubspot_list.iter_companies(unenriched_only=False))
    assert [cid for cid, _ in everything] == ["1", "2", "3"]


# --------------------------------------------------------------------------- #
# Estimate (dry-run counting)
# --------------------------------------------------------------------------- #
def test_estimate_contacts_counts_and_projects(monkeypatch):
    raw = [_raw_contact(i, enriched=(i % 2 == 0)) for i in range(1, 11)]  # 5 enriched, 5 remaining
    monkeypatch.setattr(hubspot_list, "_iter_objects", lambda *a, **k: iter(raw))
    est = estimate.estimate_contacts()
    assert est["total"] == 10
    assert est["enriched"] == 5
    assert est["remaining"] == 5
    assert est["forager_credits_estimate"] == 5 * estimate.CREDITS_PER_CONTACT


def test_estimate_companies_zero_forager_credits(monkeypatch):
    raw = [_raw_company(i, org_id=("X" if i <= 3 else "")) for i in range(1, 9)]  # 3 enriched, 5 remaining
    monkeypatch.setattr(hubspot_list, "_iter_objects", lambda *a, **k: iter(raw))
    est = estimate.estimate_companies()
    assert est["remaining"] == 5
    assert est["forager_credits_estimate"] == 0
    assert est["claude_scoring_runs"] == 5


# --------------------------------------------------------------------------- #
# Runner: dry-run spends nothing
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_call_core(monkeypatch):
    monkeypatch.setattr(runner.hubspot_list, "iter_contacts",
                        lambda **k: iter([_contact(i) for i in range(1, 6)]))

    def _boom(_id):
        raise AssertionError("handle_contact_webhook must NOT be called in a dry-run")

    monkeypatch.setattr(runner.enrichment, "handle_contact_webhook", _boom)
    out = runner.run_contacts(execute=False)
    assert out["mode"] == "dry-run"
    assert out["processed"] == 5
    assert out["enriched"] == 0  # nothing actually enriched
    assert out["estimated_forager_credits_spent"] == 5 * estimate.CREDITS_PER_CONTACT


# --------------------------------------------------------------------------- #
# Runner: ceilings
# --------------------------------------------------------------------------- #
def test_max_records_stops_run(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.hubspot_list, "iter_contacts",
                        lambda **k: iter([_contact(i) for i in range(1, 101)]))
    monkeypatch.setattr(runner.enrichment, "handle_contact_webhook",
                        lambda cid: calls.append(cid) or {"status": "enriched"})
    out = runner.run_contacts(execute=True, max_records=10)
    assert out["processed"] == 10
    assert len(calls) == 10
    assert out["stop_reason"].startswith("hit max_records")


def test_max_credits_stops_run(monkeypatch):
    monkeypatch.setattr(runner.hubspot_list, "iter_contacts",
                        lambda **k: iter([_contact(i) for i in range(1, 101)]))
    monkeypatch.setattr(runner.enrichment, "handle_contact_webhook", lambda cid: {"status": "enriched"})
    # Budget = 60 credits, 25 per contact -> room for 2 (3rd would exceed 60).
    out = runner.run_contacts(execute=True, max_credits=60)
    assert out["enriched"] == 2
    assert out["estimated_forager_credits_spent"] == 50
    assert out["stop_reason"].startswith("hit max_credits")


# --------------------------------------------------------------------------- #
# Runner: 402 halts the whole run
# --------------------------------------------------------------------------- #
def test_402_stops_run(monkeypatch):
    monkeypatch.setattr(runner.hubspot_list, "iter_contacts",
                        lambda **k: iter([_contact(i) for i in range(1, 101)]))

    def _enrich(cid):
        if cid == "3":
            err = requests.HTTPError("out of credits")
            resp = requests.Response()
            resp.status_code = 402
            err.response = resp
            raise err
        return {"status": "enriched"}

    monkeypatch.setattr(runner.enrichment, "handle_contact_webhook", _enrich)
    out = runner.run_contacts(execute=True)
    assert "402" in out["stop_reason"]
    assert out["enriched"] == 2          # ids 1 and 2 succeeded before the 402 on 3
    assert out["processed"] == 3


# --------------------------------------------------------------------------- #
# Runner: skipped results don't count as enriched (no credit charged)
# --------------------------------------------------------------------------- #
def test_skipped_not_counted_as_credits(monkeypatch):
    monkeypatch.setattr(runner.hubspot_list, "iter_contacts",
                        lambda **k: iter([_contact(i) for i in range(1, 6)]))

    def _enrich(cid):
        return {"status": "skipped"} if cid in ("2", "4") else {"status": "enriched"}

    monkeypatch.setattr(runner.enrichment, "handle_contact_webhook", _enrich)
    out = runner.run_contacts(execute=True)
    assert out["enriched"] == 3
    assert out["skipped"] == 2
    assert out["estimated_forager_credits_spent"] == 3 * estimate.CREDITS_PER_CONTACT


# --------------------------------------------------------------------------- #
# Runner: companies use the company core path and count ~0 Forager credits
# --------------------------------------------------------------------------- #
def test_companies_zero_credits(monkeypatch):
    monkeypatch.setattr(runner.hubspot_list, "iter_companies",
                        lambda **k: iter([_company(i) for i in range(1, 6)]))
    monkeypatch.setattr(runner.enrichment, "enrich_company", lambda cid: {"status": "enriched"})
    out = runner.run_companies(execute=True)
    assert out["enriched"] == 5
    assert out["estimated_forager_credits_spent"] == 0


# --------------------------------------------------------------------------- #
# Runner: handles the Deepline-ON nested return ({"workflow2":..,"workflow3":..})
# --------------------------------------------------------------------------- #
def test_contact_nested_deepline_shape(monkeypatch):
    monkeypatch.setattr(runner.hubspot_list, "iter_contacts",
                        lambda **k: iter([_contact(i) for i in range(1, 5)]))

    def _wf(cid):
        # Mimic handle_contact_webhook when DEEPLINE_API_KEY is set: nested result.
        status = "skipped" if cid == "2" else "enriched"
        return {"workflow2": {"status": status}, "workflow3": {"status": "deepline_enriched"}}

    monkeypatch.setattr(runner.enrichment, "handle_contact_webhook", _wf)
    out = runner.run_contacts(execute=True)
    assert out["enriched"] == 3          # ids 1,3,4
    assert out["skipped"] == 1           # id 2 (WF2 skip read from the nested shape)
    assert out["estimated_forager_credits_spent"] == 3 * estimate.CREDITS_PER_CONTACT
