"""
deepline.py
-----------
Deepline integration — the OPTIONAL "Workflow 3" enrichment layer plus company
Funding. This module is ADDITIVE and DORMANT by default: every public function
short-circuits unless ``DEEPLINE_API_KEY`` is set, so when the key is absent the
rest of the service behaves exactly as it did before Deepline existed.

What it does (only when enabled):
  * run_email_waterfall(...)  -> find a WORK email via a configurable chain of
    providers, validating each candidate with ZeroBounce; only a VALID address is
    accepted (INVALID / CATCH-ALL are rejected). Records the SMTP provider.
  * run_phone_waterfall(...)  -> find a phone via a configurable chain, validating
    each candidate with Trestle; only VALID + activity_score >= 50 is accepted.
    Records activity score, line type, country, and calling code.
  * get_company_funding(...)  -> company funding summary (LeadMagic by default).

Two credit-saving boundary rules apply to BOTH waterfalls (per the spec):
  1. A candidate value already rejected by the validator is never re-validated.
  2. If a second provider surfaces that SAME already-rejected value, the whole
     waterfall stops and returns blank (assume the rest will repeat the bad value).

All Deepline tools are called through the documented REST endpoint:
    POST https://code.deepline.com/api/v2/integrations/{tool_id}/execute
    Authorization: Bearer <DEEPLINE_API_KEY>
    body: {"payload": { ... }}

Per-provider API keys are BYOK and live in the Deepline dashboard — this code
only needs the one workspace key. Nothing here ever raises into the caller: on
any error a function returns an empty result and logs, so Workflow 3 can never
break Workflow 2 or a webhook.
"""

import logging
import os
import re
import time

import httpclient

logger = logging.getLogger(__name__)

_SESSION = httpclient.make_session()

DEEPLINE_BASE = "https://code.deepline.com/api/v2"
DEEPLINE_API_KEY = os.environ.get("DEEPLINE_API_KEY")

# Rough Deepline credits per call (from the integration reference) — for log/estimate only.
_EST = {
    "zerobounce_validate": 0.28,
    "trestle_real_contact": 0.42,
    "leadmagic_company_funding": 1.35,
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")

# ---------------------------------------------------------------------------
# Configurable provider order. Override via env (comma-separated tool keys), e.g.
#   DEEPLINE_EMAIL_ORDER="hunter,leadmagic,prospeo,contactout,pdl,lusha"
# The final order is decided with the client once their BYOK keys are connected;
# until then these sensible defaults (cheap + synchronous first) apply.
# ---------------------------------------------------------------------------
_DEFAULT_EMAIL_ORDER = ["hunter", "leadmagic", "prospeo", "contactout", "pdl", "crustdata", "lusha"]

# Phone enrichment is REGION-AWARE: the tool order depends on where the contact is.
# These defaults come from Shirish's email (client's list still pending final sign-off),
# and each region can be overridden via env, e.g.:
#   DEEPLINE_PHONE_ORDER_NAMER="upcell,pdl,findymail,wiza,prospeo"
_DEFAULT_PHONE_ORDER_BY_REGION = {
    "namer":  ["upcell", "pdl", "findymail", "wiza", "prospeo"],
    "europe": ["prospeo", "wiza", "datagma", "pdl", "contactout"],
    "mea":    ["pdl", "wiza", "prospeo", "datagma", "contactout"],
    "apac":   ["pdl", "wiza", "prospeo", "leadmagic", "findymail"],
    "latam":  ["pdl", "wiza", "datagma", "prospeo", "findymail"],
}
# Used when a contact's country can't be mapped to a region.
_DEFAULT_PHONE_ORDER_FALLBACK = ["pdl", "wiza", "prospeo", "contactout"]


def _order(env_name: str, default: list) -> list:
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    return [t.strip() for t in raw.split(",") if t.strip()]


def _phone_order_for_region(region: str) -> list:
    """Tool order for a region — env override (DEEPLINE_PHONE_ORDER_<REGION>) wins."""
    default = _DEFAULT_PHONE_ORDER_BY_REGION.get(region, _DEFAULT_PHONE_ORDER_FALLBACK)
    return _order(f"DEEPLINE_PHONE_ORDER_{region.upper()}", default)


# Country -> region. Lower-cased exact match on the contact's country string.
# (Kept compact: the common countries per region; unknowns fall back to "fallback".)
_REGION_BY_COUNTRY = {
    # NAMER
    "united states": "namer", "usa": "namer", "us": "namer", "u.s.": "namer",
    "u.s.a.": "namer", "america": "namer", "canada": "namer",
    # EUROPE
    "united kingdom": "europe", "uk": "europe", "england": "europe", "scotland": "europe",
    "ireland": "europe", "germany": "europe", "france": "europe", "spain": "europe",
    "portugal": "europe", "italy": "europe", "netherlands": "europe", "belgium": "europe",
    "switzerland": "europe", "austria": "europe", "sweden": "europe", "norway": "europe",
    "denmark": "europe", "finland": "europe", "poland": "europe", "czech republic": "europe",
    "czechia": "europe", "romania": "europe", "greece": "europe", "hungary": "europe",
    "ukraine": "europe",
    # MEA (Middle East + Africa)
    "united arab emirates": "mea", "uae": "mea", "saudi arabia": "mea", "qatar": "mea",
    "kuwait": "mea", "bahrain": "mea", "oman": "mea", "israel": "mea", "turkey": "mea",
    "egypt": "mea", "south africa": "mea", "nigeria": "mea", "kenya": "mea", "morocco": "mea",
    # APAC
    "india": "apac", "china": "apac", "japan": "apac", "south korea": "apac", "korea": "apac",
    "singapore": "apac", "australia": "apac", "new zealand": "apac", "indonesia": "apac",
    "malaysia": "apac", "thailand": "apac", "vietnam": "apac", "philippines": "apac",
    "hong kong": "apac", "taiwan": "apac", "pakistan": "apac", "bangladesh": "apac",
    # LATAM
    "mexico": "latam", "brazil": "latam", "argentina": "latam", "chile": "latam",
    "colombia": "latam", "peru": "latam", "uruguay": "latam", "costa rica": "latam",
    "panama": "latam", "ecuador": "latam",
}


def _region_for_country(country: str | None) -> str:
    """Map a contact's country to a phone-waterfall region. Returns the region key, or
    'fallback' when unknown (which uses the fallback order and skips phone validation)."""
    c = (country or "").strip().lower()
    return _REGION_BY_COUNTRY.get(c, "fallback")


def is_enabled() -> bool:
    """Deepline runs only when a workspace API key is configured."""
    return bool(DEEPLINE_API_KEY)


# ---------------------------------------------------------------------------
# Low-level tool execution
# ---------------------------------------------------------------------------
def _headers() -> dict:
    return {"Authorization": f"Bearer {DEEPLINE_API_KEY}", "Content-Type": "application/json"}


def execute_tool(tool_id: str, payload: dict, timeout: int = 60) -> dict | None:
    """Execute one Deepline tool. Returns the parsed JSON body (dict) or None on any
    error. Never raises — a failed provider must not break the pipeline."""
    if not is_enabled():
        return None
    url = f"{DEEPLINE_BASE}/integrations/{tool_id}/execute"
    try:
        resp = _SESSION.post(url, json={"payload": payload}, headers=_headers(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deepline %s request failed: %s", tool_id, exc)
        return None
    if resp.status_code == 402:
        logger.warning("Deepline %s -> 402 (out of credits)", tool_id)
        return None
    if resp.status_code >= 400:
        logger.warning("Deepline %s -> HTTP %s: %s", tool_id, resp.status_code, resp.text[:200])
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None


def _poll(tool_id: str, payload: dict, tries: int = 12, delay: float = 2.0) -> dict | None:
    """Poll an async tool's read endpoint until it returns a terminal result."""
    for _ in range(tries):
        data = execute_tool(tool_id, payload)
        if data is None:
            return None
        status = str(_first(data, ("status", "state")) or "").upper()
        if status in ("SCHEDULED", "RUNNING", "PENDING", "IN_PROGRESS", "PROCESSING", ""):
            if _extract_email(data) or _extract_phone(data):
                return data  # terminal data already present
            time.sleep(delay)
            continue
        return data
    return None


# ---------------------------------------------------------------------------
# Defensive extractors — provider response shapes vary, so we walk the whole
# structure: prefer the documented "best" keys, then fall back to any match.
# ---------------------------------------------------------------------------
def _walk(obj):
    """Yield every (key, value) pair anywhere inside a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _first(obj, keys: tuple):
    """First value whose key (case-insensitive) is in `keys`."""
    wanted = {k.lower() for k in keys}
    for k, v in _walk(obj):
        if isinstance(k, str) and k.lower() in wanted and v not in (None, "", [], {}):
            return v
    return None


def _extract_email(obj) -> str | None:
    if obj is None:
        return None
    # 1) Preferred work-email keys first.
    for key in ("most_probable_work_email", "work_email", "professional_email",
                "business_email", "email", "email_address"):
        val = _first(obj, (key,))
        email = _coerce_email(val)
        if email:
            return email
    # 2) Anything that looks like an email anywhere in the response.
    for _, v in _walk(obj):
        email = _coerce_email(v)
        if email:
            return email
    return None


def _coerce_email(val) -> str | None:
    if isinstance(val, str):
        m = _EMAIL_RE.search(val)
        return m.group(0) if m else None
    if isinstance(val, dict):
        for key in ("email", "value", "address", "most_probable_work_email"):
            if val.get(key):
                return _coerce_email(val[key])
    if isinstance(val, list) and val:
        return _coerce_email(val[0])
    return None


def _extract_phone(obj) -> str | None:
    if obj is None:
        return None
    for key in ("mobile", "mobile_phone", "phone", "phone_number", "direct_dial", "number"):
        val = _first(obj, (key,))
        phone = _coerce_phone(val)
        if phone:
            return phone
    for _, v in _walk(obj):
        phone = _coerce_phone(v)
        if phone:
            return phone
    return None


def _coerce_phone(val) -> str | None:
    if isinstance(val, (int,)):
        val = str(val)
    if isinstance(val, str):
        m = _PHONE_RE.search(val)
        if m:
            cleaned = re.sub(r"[^\d+]", "", m.group(0))
            return cleaned if len(re.sub(r"\D", "", cleaned)) >= 8 else None
        return None
    if isinstance(val, dict):
        for key in ("number", "phone", "phone_number", "value", "raw"):
            if val.get(key):
                return _coerce_phone(val[key])
    if isinstance(val, list) and val:
        return _coerce_phone(val[0])
    return None


# ---------------------------------------------------------------------------
# Provider input payloads. We pass every identity field we have under the common
# aliases providers expect; unknown extras are generally ignored. Exact per-provider
# field tuning is verified on the first live run (when BYOK keys + credits exist).
# ---------------------------------------------------------------------------
def _identity(inp: dict, extra: dict | None = None) -> dict:
    """Build a provider payload that covers EVERY field-name variant the Deepline doc
    uses across providers (snake_case, camelCase, and provider-specific names like
    `linkedinProfileUrl`, `profile_url`, `domainOrCompany`). Providers ignore keys
    they don't use, so sending all variants maximises match rate without harm."""
    first = inp.get("first_name") or ""
    last = inp.get("last_name") or ""
    full = (inp.get("full_name") or f"{first} {last}").strip()
    domain = inp.get("domain") or ""
    company = inp.get("company_name") or ""
    li = inp.get("linkedin_url") or ""
    payload = {
        # name
        "first_name": first, "last_name": last,
        "full_name": full, "fullName": full, "name": full,
        # company domain (snake / camel / icypeas' domainOrCompany)
        "domain": domain, "company_domain": domain, "companyDomain": domain,
        "domainOrCompany": domain or company,
        # company name
        "company": company, "company_name": company,
        # linkedin (every variant the doc shows: leadmagic profile_url, crustdata
        # linkedinProfileUrl, contactout/lusha linkedin_url, etc.)
        "linkedin_url": li, "linkedinUrl": li, "linkedinProfileUrl": li,
        "profile_url": li, "profile": li,
    }
    if inp.get("email"):
        payload["email"] = inp["email"]
    if extra:
        payload.update(extra)
    return {k: v for k, v in payload.items() if v}


# --- EMAIL finder adapters: each returns a candidate email or None ---
def _email_hunter(inp):       return _extract_email(execute_tool("hunter_email_finder", _identity(inp)))
def _email_leadmagic(inp):    return _extract_email(execute_tool("leadmagic_email_finder", _identity(inp)))
def _email_prospeo(inp):      return _extract_email(execute_tool("prospeo_enrich_person", _identity(inp)))
def _email_contactout(inp):   return _extract_email(execute_tool("contactout_enrich_person", _identity(inp, {"include": ["work_email"]})))
def _email_pdl(inp):          return _extract_email(execute_tool("peopledatalabs_enrich_contact", _identity(inp)))
def _email_crustdata(inp):    return _extract_email(execute_tool("crustdata_person_enrichment", _identity(inp)))
def _email_lusha(inp):        return _extract_email(execute_tool("lusha_enrich_person", _identity(inp, {"reveal_emails": True})))


def _email_icypeas(inp):
    """Async: submit search, then poll read-results for the email."""
    submitted = execute_tool("icypeas_email_search", _identity(inp))
    job_id = _first(submitted, ("_id", "id", "task_id"))
    if not job_id:
        return _extract_email(submitted)
    return _extract_email(_poll("icypeas_read_results", {"_id": job_id}))


def _email_fullenrich(inp):
    """Async: FullEnrich is its own waterfall. Submit a 1-row bulk job, then poll."""
    row = _identity(inp, {"enrich_fields": ["contact.emails"]})
    submitted = execute_tool("fullenrich_bulk_enrich", {"name": "forager-hubspot", "data": [row]})
    job_id = _first(submitted, ("enrichment_id", "id"))
    if not job_id:
        return _extract_email(submitted)
    return _extract_email(_poll("fullenrich_get_result", {"enrichment_id": job_id, "forceResults": True}))


_EMAIL_ADAPTERS = {
    "hunter": _email_hunter, "leadmagic": _email_leadmagic, "prospeo": _email_prospeo,
    "contactout": _email_contactout, "pdl": _email_pdl, "crustdata": _email_crustdata,
    "lusha": _email_lusha, "icypeas": _email_icypeas, "fullenrich": _email_fullenrich,
}


# --- PHONE finder adapters: each returns a candidate phone or None ---
# NOTE: the exact tool IDs/payloads for the newer providers (upcell, findymail, wiza,
# datagma) are best-effort from the Deepline docs and confirmed on the first live run.
def _phone_leadmagic(inp):    return _extract_phone(execute_tool("leadmagic_mobile_finder", _identity(inp)))
def _phone_contactout(inp):   return _extract_phone(execute_tool("contactout_enrich_person", _identity(inp, {"include": ["phone"]})))
def _phone_lusha(inp):        return _extract_phone(execute_tool("lusha_enrich_person", _identity(inp, {"reveal_phones": True})))
def _phone_pdl(inp):          return _extract_phone(execute_tool("peopledatalabs_enrich_contact", _identity(inp)))
def _phone_prospeo(inp):      return _extract_phone(execute_tool("prospeo_enrich_person", _identity(inp, {"enrich_mobile": True})))
def _phone_upcell(inp):       return _extract_phone(execute_tool("upcell_enrich_contact", _identity(inp, {"fields": ["mobile"]})))
def _phone_datagma(inp):      return _extract_phone(execute_tool("datagma_search_phone_numbers", _identity(inp)))
def _phone_wiza(inp):         return _extract_phone(execute_tool("wiza_reveal_person", _identity(inp)))
def _phone_findymail(inp):    return _extract_phone(execute_tool("findymail_find_phone", _identity(inp)))


def _phone_fullenrich(inp):
    row = _identity(inp, {"enrich_fields": ["contact.phones"]})
    submitted = execute_tool("fullenrich_bulk_enrich", {"name": "forager-hubspot", "data": [row]})
    job_id = _first(submitted, ("enrichment_id", "id"))
    if not job_id:
        return _extract_phone(submitted)
    return _extract_phone(_poll("fullenrich_get_result", {"enrichment_id": job_id, "forceResults": True}))


_PHONE_ADAPTERS = {
    "leadmagic": _phone_leadmagic, "contactout": _phone_contactout, "lusha": _phone_lusha,
    "pdl": _phone_pdl, "prospeo": _phone_prospeo, "fullenrich": _phone_fullenrich,
    "upcell": _phone_upcell, "datagma": _phone_datagma, "wiza": _phone_wiza,
    "findymail": _phone_findymail,
}


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def validate_email(email: str) -> dict:
    """ZeroBounce gate. Returns {"valid": bool, "smtp_provider": str|None, "status": str}.
    Only status == 'valid' passes; 'invalid' and 'catch-all' fail (per spec)."""
    data = execute_tool("zerobounce_validate", {"email": email})
    status = str(_first(data, ("status",)) or "").lower().replace("_", "-")
    smtp = _first(data, ("smtp_provider", "mx_provider", "provider"))
    return {"valid": status == "valid", "status": status or "unknown", "smtp_provider": smtp}


def _phone_validation_enabled() -> bool:
    """Global off-switch — set DEEPLINE_PHONE_VALIDATION=off to skip phone validation
    everywhere (Shirish: if Real Contact proves tricky in Deepline, skip it entirely)."""
    return (os.environ.get("DEEPLINE_PHONE_VALIDATION") or "on").strip().lower() not in ("off", "0", "false", "no")


def validate_phone(phone: str, name: str, region: str = "namer") -> dict:
    """Phone gate per Shirish's call:
      * Trestle's validity / activity-score are NOT used (deemed unreliable).
      * ONLY the Trestle 'Real Contact' name-match is used, and ONLY for NAMER.
        Accept name_match == True OR unknown/'Don't know'; reject ONLY explicit False.
      * EMEA / APAC / LATAM / unknown regions: NO validation — accept as-is.
      * If validation is globally off, or Real Contact returns nothing, accept (skip)."""
    if region != "namer" or not _phone_validation_enabled():
        return {"valid": True, "validated": False, "region": region}
    data = execute_tool("trestle_real_contact", {"phone": phone, "name": name})
    if data is None:
        # "If Real Contact proves tricky in Deepline, skip validation" -> accept.
        return {"valid": True, "validated": False, "region": region, "note": "real_contact unavailable"}
    nm = _first(data, ("name_match",))
    nm_s = str(nm).strip().lower() if nm is not None else ""
    rejected = (nm is False) or nm_s in ("false", "no", "mismatch", "name.mismatch", "no_match")
    return {"valid": not rejected, "validated": True, "region": region, "name_match": nm}


# ---------------------------------------------------------------------------
# Waterfall engine (shared structure for email + phone)
# ---------------------------------------------------------------------------
def _run_waterfall(order: list, adapters: dict, inp: dict, validate, normalize, channel: str) -> dict:
    """Try providers in order; validate each candidate; apply both boundary rules.
    Returns {"value": ..., "meta": {...}, "providers_tried": n, "winner": key} or {}."""
    failed: set = set()
    tried = 0
    for key in order:
        adapter = adapters.get(key)
        if adapter is None:
            continue
        tried += 1
        try:
            raw = adapter(inp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Deepline %s provider %s errored: %s", channel, key, exc)
            continue
        if not raw:
            continue
        norm = normalize(raw)
        if norm in failed:
            # Boundary rule 1 (don't re-validate a known-bad value) AND rule 2 (a
            # second provider surfaced the same rejected value) -> stop, return blank.
            logger.info("Deepline %s: provider %s repeated a rejected value -> stopping (blank)", channel, key)
            return {}
        verdict = validate(norm)
        if verdict.get("valid"):
            logger.info("Deepline %s resolved by %s (providers tried=%d)", channel, key, tried)
            return {"value": raw, "meta": verdict, "providers_tried": tried, "winner": key}
        failed.add(norm)  # rejected once; a repeat from any later provider triggers the stop above
    return {}


def run_email_waterfall(inp: dict) -> dict:
    if not is_enabled() or not inp.get("domain") or not (inp.get("first_name") or inp.get("full_name")):
        return {}
    return _run_waterfall(_order("DEEPLINE_EMAIL_ORDER", _DEFAULT_EMAIL_ORDER),
                          _EMAIL_ADAPTERS, inp, validate_email,
                          lambda e: e.lower().strip(), "email")


def run_phone_waterfall(inp: dict) -> dict:
    """Region-aware phone waterfall: the tool ORDER depends on the contact's region
    (from inp['country']), and validation only runs for NAMER (Trestle name-match)."""
    if not is_enabled() or not (inp.get("first_name") or inp.get("full_name")):
        return {}
    region = _region_for_country(inp.get("country"))
    order = _phone_order_for_region(region)
    name = inp.get("full_name") or ""
    result = _run_waterfall(order, _PHONE_ADAPTERS, inp,
                            lambda phone: validate_phone(phone, name, region),
                            lambda p: re.sub(r"\D", "", p), "phone")
    if result:
        result["region"] = region
    return result


# ---------------------------------------------------------------------------
# Company funding
# ---------------------------------------------------------------------------
def _parse_money(value) -> float | None:
    """Best-effort parse of a funding amount into USD float. Handles 12000000,
    '12,000,000', '$5M', '$1.2B', '500K'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower().replace("$", "").replace(",", "").replace("usd", "").strip()
    if not s:
        return None
    mult = 1.0
    if s.endswith("k"):
        mult, s = 1e3, s[:-1]
    elif s.endswith("m"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("b"):
        mult, s = 1e9, s[:-1]
    try:
        return float(s.strip()) * mult
    except ValueError:
        return None


def get_company_funding(domain: str | None, name: str | None = None) -> dict:
    """Return {"display": str|None, "amount": float|None} for a company's funding.
    `display` -> HubSpot Funding field; `amount` (USD) -> the Tier-1 rule. Uses
    Crustdata by default (per client), configurable via DEEPLINE_FUNDING_TOOL."""
    if not is_enabled() or not (domain or name):
        return {"display": None, "amount": None}
    tool = os.environ.get("DEEPLINE_FUNDING_TOOL") or "crustdata_company_enrichment"
    payload = {}
    if domain:
        payload["companyDomain"] = domain   # Crustdata uses camelCase
        payload["company_domain"] = domain
        payload["domain"] = domain
    if name:
        payload["company_name"] = name
        payload["name"] = name
    data = execute_tool(tool, payload)
    if not data:
        return {"display": None, "amount": None}
    total = _first(data, ("total_funding_usd", "total_funding", "total_funding_amount",
                          "funding_total", "total_raised", "funding_amount", "funding"))
    last_round = _first(data, ("last_funding_type", "latest_round", "last_round", "funding_stage"))
    last_date = _first(data, ("last_funding_date", "latest_funding_date"))
    amount = _parse_money(total)
    parts = []
    if total:
        parts.append(f"Total: {total}")
    if last_round:
        parts.append(f"Last round: {last_round}")
    if last_date:
        parts.append(str(last_date))
    display = " | ".join(str(p) for p in parts) if parts else _first(data, ("summary", "description"))
    return {"display": str(display) if display else None, "amount": amount}
