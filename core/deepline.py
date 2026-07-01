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
import threading
import time

import httpclient

logger = logging.getLogger(__name__)

_SESSION = httpclient.make_session()

# Thread-local capture of raw execute_tool responses — used ONLY by the debug test
# harness (?raw=1) to surface each provider's full JSON. Off by default (no overhead).
_capture = threading.local()

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


# Deepline validates each tool's payload against a STRICT schema and rejects (HTTP
# 422) any field the tool doesn't recognise — it does NOT silently ignore extras.
# The reference doc doesn't publish the exact accepted fields for most tools, so
# rather than guess we send the broad identity payload and let the validator tell
# us what to drop: a 422 body enumerates every unexpected field, we strip those and
# retry, and we remember them per tool so later calls are right on the first shot.
# A 422 is a pre-execution validation failure, so these retries cost ZERO credits.
#
# SEED: the exact reject-lists observed from live 422 responses, so even the FIRST
# contact in a fresh process sends the right payload immediately — no cold-start
# round-trips (Hunter in particular reports unexpected fields one at a time, which
# was ~12 retries on the first contact). This is purely an optimisation: the runtime
# self-heal above still corrects anything not seeded (or if Deepline changes a schema).
_REJECTED_FIELDS_SEED: dict = {
    "hunter_email_finder": {
        "fullName", "name", "company_domain", "companyDomain", "domainOrCompany",
        "company_name", "linkedin_url", "linkedinUrl", "linkedinProfileUrl",
        "profile_url", "profile", "email"},
    "leadmagic_email_finder": {
        "company", "companyDomain", "domainOrCompany", "email", "fullName", "full_name",
        "linkedinProfileUrl", "linkedinUrl", "linkedin_url", "name", "profile", "profile_url"},
    "prospeo_enrich_person": {
        "company", "companyDomain", "domainOrCompany", "fullName", "linkedinProfileUrl",
        "linkedinUrl", "name", "profile", "profile_url"},
    "contactout_enrich_person": {
        "companyDomain", "company_name", "domain", "domainOrCompany", "fullName",
        "linkedinProfileUrl", "linkedinUrl", "name", "profile", "profile_url"},
    "peopledatalabs_enrich_contact": {
        "companyDomain", "company_domain", "domainOrCompany", "fullName", "full_name",
        "linkedinProfileUrl", "linkedinUrl", "profile_url"},
    "lusha_enrich_person": {
        "company", "companyDomain", "domain", "domainOrCompany", "fullName", "full_name",
        "linkedinProfileUrl", "linkedinUrl", "name", "profile", "profile_url"},
}
_REJECTED_FIELDS: dict = {tool: set(fields) for tool, fields in _REJECTED_FIELDS_SEED.items()}

# Two shapes seen in Deepline 422 bodies:
#   leadmagic/prospeo/contactout/lusha:  full_name: Unexpected field "full_name".
#   pdl:                                 company_domain: Unexpected property
#   hunter (custom):                     ... unexpected field "fullName"
_UNEXPECTED_QUOTED = re.compile(r'unexpected (?:field|property)\s*"([A-Za-z_][\w]*)"', re.IGNORECASE)
_UNEXPECTED_PREFIX = re.compile(r'([A-Za-z_][\w]*)\s*:\s*Unexpected (?:field|property)', re.IGNORECASE)


def _parse_unexpected_fields(text: str) -> set:
    """Pull every field name Deepline flagged as unexpected from a 422 body. The body
    is raw JSON, so inner quotes arrive backslash-escaped (\\"fullName\\") — decode it
    first so the quoted-field regex sees clean text."""
    decoded = text or ""
    try:
        import json
        body = json.loads(decoded)
        if isinstance(body, dict):
            decoded = " ".join(str(body.get(k, "")) for k in ("error", "message")) or decoded
    except Exception:  # noqa: BLE001
        pass
    fields = set()
    for rx in (_UNEXPECTED_QUOTED, _UNEXPECTED_PREFIX):
        for m in rx.finditer(decoded):
            fields.add(m.group(1))
    return fields


def execute_tool(tool_id: str, payload: dict, timeout: int = 60) -> dict | None:
    """Execute one Deepline tool. Returns the parsed JSON body (dict) or None on any
    error. Never raises — a failed provider must not break the pipeline.

    On a 422 "unexpected field" validation error, the offending fields are stripped
    and the call is retried (and cached per tool), so strict-schema tools succeed
    without us hardcoding each tool's payload shape."""
    if not is_enabled():
        return None
    url = f"{DEEPLINE_BASE}/integrations/{tool_id}/execute"
    # Pre-strip fields this tool already rejected earlier in the process.
    known_bad = _REJECTED_FIELDS.get(tool_id)
    if known_bad:
        payload = {k: v for k, v in payload.items() if k not in known_bad}
    attempts = 0
    while True:
        attempts += 1
        try:
            resp = _SESSION.post(url, json={"payload": payload}, headers=_headers(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Deepline %s request failed: %s", tool_id, exc)
            return None
        if resp.status_code == 402:
            logger.warning("Deepline %s -> 402 (out of credits)", tool_id)
            return None
        if resp.status_code == 422 and payload and attempts <= 20:
            unexpected = _parse_unexpected_fields(resp.text) & set(payload)
            if unexpected:
                _REJECTED_FIELDS.setdefault(tool_id, set()).update(unexpected)
                payload = {k: v for k, v in payload.items() if k not in unexpected}
                logger.info("Deepline %s: dropped unexpected field(s) %s, retrying",
                            tool_id, ",".join(sorted(unexpected)))
                if payload:
                    continue
            logger.warning("Deepline %s -> HTTP 422: %s", tool_id, resp.text[:200])
            return None
        if resp.status_code >= 400:
            logger.warning("Deepline %s -> HTTP %s: %s", tool_id, resp.status_code, resp.text[:200])
            return None
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001
            return None
        buf = getattr(_capture, "buf", None)
        if buf is not None:
            buf.append({"tool": tool_id, "response": parsed})
        return parsed


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


def _emails_from(val) -> list:
    """All email addresses found in a value (handles strings, dicts, and lists)."""
    out = []
    if isinstance(val, str):
        out.extend(_EMAIL_RE.findall(val))
    elif isinstance(val, dict):
        for key in ("email", "value", "address", "most_probable_work_email"):
            if val.get(key):
                out.extend(_emails_from(val[key]))
    elif isinstance(val, list):
        for item in val:
            out.extend(_emails_from(item))
    return out


def _same_company(email: str, domain: str) -> bool:
    """True if `email`'s domain matches the target company `domain` (exact / sub / parent)."""
    if not domain or "@" not in (email or ""):
        return False
    ed = email.rsplit("@", 1)[1].lower().strip().replace("www.", "")
    d = domain.lower().strip().lstrip("/").replace("www.", "")
    return bool(ed) and (ed == d or ed.endswith("." + d) or d.endswith("." + ed))


def _extract_email(obj, prefer_domain: str | None = None) -> str | None:
    if obj is None:
        return None
    # Collect candidate emails in priority order: documented work-email keys first,
    # then any other email field, then any email-looking string anywhere.
    candidates = []
    for key in ("most_probable_work_email", "work_email", "professional_email",
                "business_email", "email", "email_address"):
        wanted = key.lower()
        for k, v in _walk(obj):
            if isinstance(k, str) and k.lower() == wanted:
                candidates.extend(_emails_from(v))
    if not candidates:
        for _, v in _walk(obj):
            if isinstance(v, str):
                candidates.extend(_EMAIL_RE.findall(v))
    seen, ordered = set(), []
    for e in candidates:
        el = e.lower()
        if el not in seen:
            seen.add(el)
            ordered.append(e)
    if not ordered:
        return None
    # When a provider returns SEVERAL emails (e.g. ContactOut returns jsconf.eu AND
    # vercel.com), PREFER the one at the target company's domain over just the first.
    if prefer_domain:
        for e in ordered:
            if _same_company(e, prefer_domain):
                return e
    return ordered[0]


# Keys whose values are NEVER phone numbers — skip them in the greedy fallback so a
# date / id / score / count / URL is never mistaken for a phone (e.g. a verification
# date "2026-06-29" -> "20260629", or a timestamp inside a logo URL).
_NON_PHONE_KEY_HINTS = (
    "date", "_on", "_at", "year", "founded", "score", "count", "id", "uuid", "zip",
    "postal", "version", "timestamp", "grade", "created", "updated", "_ts",
    "latitude", "longitude", "geo", "url", "uri", "href", "logo", "image", "img",
    "photo", "picture", "avatar", "permalink", "link", "icon",
)
_DATE_LIKE_RE = re.compile(r"^\s*\d{4}-\d{1,2}-\d{1,2}")
_PHONE_SEP_RE = re.compile(r"[ ().\-]")


def _extract_phone(obj) -> str | None:
    if obj is None:
        return None
    # 1) Trusted phone keys. INTERNATIONAL / E.164 keys FIRST so we keep the full
    #    +country-code number — some tools (e.g. Datagma) put the bare national number
    #    under "number" (1718659901) and the real one under "displayInternational"
    #    (+49 171 8659901). Then the common keys; then provider-specific ones (Wiza
    #    phone_number1/mobile_phone1). Bare digits are allowed here (trusted keys).
    for key in ("displayinternational", "phone_international", "international", "e164",
                "mobile_international", "mobile", "mobile_phone", "mobilephone",
                "mobile_number", "mobilenumber", "cell", "cell_phone", "cellphone",
                "phone", "phone_number", "phonenumber", "direct_dial", "directdial",
                "number", "phones", "phone_numbers", "phonenumbers", "mobile_phones",
                "mobile_phone1", "phone_number1", "mobile_phone2", "phone_number2"):
        val = _first(obj, (key,))
        phone = _coerce_phone(val)
        if phone:
            return phone
    # 2) Greedy fallback — skip non-phone keys AND require phone-shaped formatting (a "+"
    #    or separators) plus a real length, so a bare digit run from a URL / id / money
    #    amount / timestamp can't masquerade as a phone (e.g. a logo URL's
    #    ".../1630609823582/" or a funding figure 863000000).
    for k, v in _walk(obj):
        if isinstance(k, str) and any(h in k.lower() for h in _NON_PHONE_KEY_HINTS):
            continue
        phone = _coerce_phone(v, min_digits=10, require_format=True)
        if phone:
            return phone
    return None


def _coerce_phone(val, min_digits: int = 8, require_format: bool = False) -> str | None:
    if isinstance(val, int):
        val = str(val)
    if isinstance(val, str):
        if _DATE_LIKE_RE.match(val):                                   # a date, not a phone
            return None
        if "/" in val or "http" in val.lower() or "@" in val:          # URL/email, not a phone
            return None
        m = _PHONE_RE.search(val)
        if m:
            matched = m.group(0)
            cleaned = re.sub(r"[^\d+]", "", matched)
            digits = re.sub(r"\D", "", cleaned)
            # A real phone is ~min_digits–15 digits (E.164 max 15); longer is an id/timestamp.
            if not (min_digits <= len(digits) <= 15):
                return None
            # In the greedy context, demand phone formatting (+ or separators) so a bare
            # digit run (id / money / timestamp) is never taken as a phone.
            if require_format and "+" not in matched and not _PHONE_SEP_RE.search(matched):
                return None
            return cleaned
        return None
    if isinstance(val, dict):
        for key in ("displayInternational", "phone_international", "international", "e164",
                    "mobile_international", "number", "phone", "phone_number", "mobile",
                    "mobile_number", "value", "raw", "formatted", "national"):
            if val.get(key):
                got = _coerce_phone(val[key], min_digits, require_format)
                if got:
                    return got
    if isinstance(val, list):
        for item in val:
            got = _coerce_phone(item, min_digits, require_format)
            if got:
                return got
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
def _email_hunter(inp):       return _extract_email(execute_tool("hunter_email_finder", _identity(inp)), inp.get("domain"))
def _email_leadmagic(inp):    return _extract_email(execute_tool("leadmagic_email_finder", _identity(inp)), inp.get("domain"))
def _email_prospeo(inp):      return _extract_email(execute_tool("prospeo_enrich_person", _identity(inp)), inp.get("domain"))
def _email_contactout(inp):   return _extract_email(execute_tool("contactout_enrich_person", _identity(inp, {"include": ["work_email"]})), inp.get("domain"))
def _email_pdl(inp):          return _extract_email(execute_tool("peopledatalabs_enrich_contact", _identity(inp)), inp.get("domain"))
def _email_crustdata(inp):    return _extract_email(execute_tool("crustdata_person_enrichment", _identity(inp)), inp.get("domain"))
def _email_lusha(inp):        return _extract_email(execute_tool("lusha_enrich_person", _identity(inp, {"reveal_emails": True})), inp.get("domain"))


def _email_icypeas(inp):
    """Async: submit search, then poll read-results for the email."""
    submitted = execute_tool("icypeas_email_search", _identity(inp))
    job_id = _first(submitted, ("_id", "id", "task_id"))
    if not job_id:
        return _extract_email(submitted, inp.get("domain"))
    return _extract_email(_poll("icypeas_read_results", {"_id": job_id}), inp.get("domain"))


def _email_fullenrich(inp):
    """Async: FullEnrich is its own waterfall. Submit a 1-row bulk job, then poll."""
    row = _identity(inp, {"enrich_fields": ["contact.emails"]})
    submitted = execute_tool("fullenrich_bulk_enrich", {"name": "forager-hubspot", "data": [row]})
    job_id = _first(submitted, ("enrichment_id", "id"))
    if not job_id:
        return _extract_email(submitted, inp.get("domain"))
    return _extract_email(_poll("fullenrich_get_result", {"enrichment_id": job_id, "forceResults": True}), inp.get("domain"))


_EMAIL_ADAPTERS = {
    "hunter": _email_hunter, "leadmagic": _email_leadmagic, "prospeo": _email_prospeo,
    "contactout": _email_contactout, "pdl": _email_pdl, "crustdata": _email_crustdata,
    "lusha": _email_lusha, "icypeas": _email_icypeas, "fullenrich": _email_fullenrich,
}


# --- PHONE finder adapters: each returns a candidate phone or None ---
def _phone_leadmagic(inp):    return _extract_phone(execute_tool("leadmagic_mobile_finder", _identity(inp)))
def _phone_contactout(inp):   return _extract_phone(execute_tool("contactout_enrich_person", _identity(inp, {"include": ["phone"]})))
def _phone_lusha(inp):        return _extract_phone(execute_tool("lusha_enrich_person", _identity(inp, {"reveal_phones": True})))
def _phone_pdl(inp):          return _extract_phone(execute_tool("peopledatalabs_enrich_contact", _identity(inp)))
def _phone_prospeo(inp):      return _extract_phone(execute_tool("prospeo_enrich_person", _identity(inp, {"enrich_mobile": True})))
def _phone_datagma(inp):      return _extract_phone(execute_tool("datagma_search_phone_numbers", _identity(inp)))
def _phone_findymail(inp):    return _extract_phone(execute_tool("findymail_find_phone", _identity(inp)))


def _phone_upcell(inp):
    """Upcell wants the identity FLAT + camelCase at the top level (linkedinUrl,
    firstName, lastName, companyName, companyDomain, email, personalEmail) plus a
    `fields` list — NOT nested in a `contact` object. (Per the live 422: "Provide
    linkedinUrl, email, personalEmail, firstName+lastName+title+companyName, or
    firstName+lastName plus companyDomain/companySocialUrl.") Mobile-only for now."""
    payload = {k: v for k, v in {
        "linkedinUrl": inp.get("linkedin_url"),
        "firstName": inp.get("first_name"),
        "lastName": inp.get("last_name"),
        "companyName": inp.get("company_name"),
        "companyDomain": inp.get("domain"),
        "email": inp.get("email"),
    }.items() if v}
    payload["fields"] = ["mobile"]
    return _extract_phone(execute_tool("upcell_enrich_contact", payload))


def _phone_wiza(inp):
    """Wiza only returns phones at enrichment_level 'full' (~7 credits) and is async.
    We submit at 'full' and read phone_number1/mobile_phone1; we do NOT re-poll (avoids
    double-charging) — if Deepline returns it queued, the waterfall just moves on. Level
    is overridable via DEEPLINE_WIZA_LEVEL (e.g. 'partial' to skip phone credits)."""
    level = os.environ.get("DEEPLINE_WIZA_LEVEL") or "full"
    return _extract_phone(execute_tool("wiza_reveal_person", _identity(inp, {"enrichment_level": level})))


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
# Deepline wraps every tool result in a job envelope that ALSO carries a
# "status" field — the JOB status ("completed"/"running"/"scheduled") — separate
# from the provider's own verdict "status" (ZeroBounce: valid/invalid/catch-all).
# A greedy search for "status" can grab the job status by mistake and reject every
# email. So we locate the verdict object SPECIFICALLY: the nested dict that holds
# the verdict's "status" alongside a field unique to that verdict (e.g. ZeroBounce's
# "address"). Falls back to any "status" whose VALUE is a known ZeroBounce verdict.
_ZB_VERDICTS = {"valid", "invalid", "catch-all", "spamtrap", "abuse", "do-not-mail", "unknown"}


def _verdict_object(data, sibling_key: str):
    """Return the nested dict that has BOTH a 'status' key and `sibling_key` — i.e. the
    provider's verdict object, not the Deepline job envelope (which has 'status' alone)."""
    found = [None]

    def walk(o):
        if found[0] is not None:
            return
        if isinstance(o, dict):
            if "status" in o and sibling_key in o:
                found[0] = o
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(data)
    return found[0]


def validate_email(email: str) -> dict:
    """ZeroBounce gate. Returns {"valid": bool, "smtp_provider": str|None, "status": str,
    "catchall_domain": bool}. Only status == 'valid' passes; 'invalid' and 'catch-all'
    fail (per spec). Reads the ZeroBounce verdict object, NOT the Deepline job status."""
    data = execute_tool("zerobounce_validate", {"email": email})
    zb = _verdict_object(data, "address")
    if zb:
        raw_status = zb.get("status")
        smtp = zb.get("smtp_provider") or zb.get("mx_provider") or zb.get("provider")
        catchall = bool(zb.get("catchall_domain"))
    else:
        # Fallback: pick a "status" whose value is a real ZeroBounce verdict (never the
        # job status), so a wrapper-shape change can't silently reject every email.
        raw_status, smtp, catchall = None, None, False
        for k, v in _walk(data or {}):
            if isinstance(k, str) and k.lower() == "status" and isinstance(v, str) \
                    and v.strip().lower().replace("_", "-") in _ZB_VERDICTS:
                raw_status = v
                break
        smtp = _first(data, ("smtp_provider", "mx_provider", "provider"))
        catchall = bool(_first(data, ("catchall_domain",)))
    status = str(raw_status or "").lower().replace("_", "-")
    return {"valid": status == "valid", "status": status or "unknown",
            "smtp_provider": smtp, "catchall_domain": catchall}


def _phone_validation_enabled() -> bool:
    """Global off-switch — set DEEPLINE_PHONE_VALIDATION=off to skip phone validation
    everywhere (Shirish: if Real Contact proves tricky in Deepline, skip it entirely)."""
    return (os.environ.get("DEEPLINE_PHONE_VALIDATION") or "on").strip().lower() not in ("off", "0", "false", "no")


def _phone_name_match(data):
    """Read Trestle Real Contact's PHONE name-match. Trestle returns FLATTENED dotted
    keys ("phone.name_match", "phone.is_valid", ...) and ALSO carries address.name_match
    / email.name_match — so a bare "name_match" lookup misses the right field entirely
    (and silently accepts everything, incl. real mismatches). Read the phone one
    specifically, tolerating both the dotted-flat shape and a nested {"phone": {...}}."""
    if not isinstance(data, (dict, list)):
        return None
    # 1) flattened dotted key (the shape Trestle actually returns)
    val = _first(data, ("phone.name_match",))
    if val is not None:
        return val
    # 2) nested {"phone": {"name_match": ...}}
    phone_obj = _first(data, ("phone",))
    if isinstance(phone_obj, dict) and "name_match" in phone_obj:
        return phone_obj.get("name_match")
    # 3) last-resort bare key
    return _first(data, ("name_match",))


def validate_phone(phone: str, name: str, region: str = "namer") -> dict:
    """Phone gate per Shirish's call:
      * Trestle's validity / activity-score are NOT used to GATE (deemed unreliable).
      * ONLY the Trestle 'Real Contact' PHONE name-match is used, and ONLY for NAMER.
        Accept name_match == True OR unknown/'Don't know' (null); reject ONLY explicit False.
      * EMEA / APAC / LATAM / unknown regions: NO validation — accept as-is.
      * If validation is globally off, or Real Contact returns nothing, accept (skip).
    Activity score + line type are captured as metadata (not used to gate)."""
    if region != "namer" or not _phone_validation_enabled():
        return {"valid": True, "validated": False, "region": region}
    data = execute_tool("trestle_real_contact", {"phone": phone, "name": name})
    if data is None:
        # "If Real Contact proves tricky in Deepline, skip validation" -> accept.
        return {"valid": True, "validated": False, "region": region, "note": "real_contact unavailable"}
    nm = _phone_name_match(data)
    nm_s = str(nm).strip().lower() if nm is not None else ""
    rejected = (nm is False) or nm_s in ("false", "no", "mismatch", "name.mismatch", "no_match")
    return {
        "valid": not rejected, "validated": True, "region": region, "name_match": nm,
        # Metadata only — surfaced to HubSpot, never used to accept/reject.
        "activity_score": _first(data, ("phone.activity_score", "activity_score")),
        "line_type": _first(data, ("phone.linetype", "phone.line_type", "linetype", "line_type")),
    }


# ---------------------------------------------------------------------------
# Waterfall engine (shared structure for email + phone)
# ---------------------------------------------------------------------------
def _verdict_brief(verdict: dict) -> str:
    """One-line summary of a validation verdict, for the per-provider log trail."""
    if not isinstance(verdict, dict):
        return ""
    if verdict.get("status"):  # email (ZeroBounce)
        s = str(verdict["status"])
        if verdict.get("catchall_domain"):
            s += ",catch-all-domain"
        if verdict.get("smtp_provider"):
            s += ",%s" % verdict["smtp_provider"]
        return s
    bits = []  # phone (Trestle) / generic
    if "name_match" in verdict:
        bits.append("name_match=%s" % verdict.get("name_match"))
    if not verdict.get("validated"):
        bits.append("not-validated")
    if verdict.get("region"):
        bits.append("region=%s" % verdict["region"])
    return ",".join(bits)


def _run_waterfall(order: list, adapters: dict, inp: dict, validate, normalize, channel: str,
                   accept=None) -> dict:
    """Try providers in order; validate each candidate; apply both boundary rules.
    `accept` (optional) pre-filters a raw candidate BEFORE validation — used by the
    email waterfall to drop a wrong-company address without poisoning the boundary set.
    Logs the full per-provider trail (tried -> nothing / rejected / resolved) so the
    Railway logs show which tool produced the value and which ones failed and why.
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
            logger.warning("Deepline %s: %s errored: %s", channel, key, exc)
            continue
        if not raw:
            logger.info("Deepline %s: %s found nothing", channel, key)
            continue
        if accept is not None and not accept(raw):
            # Off-target (e.g. an email at a DIFFERENT company than we're enriching for).
            # Skip without adding to `failed` so it can't trip the boundary stop.
            logger.info("Deepline %s: %s returned %s (off-target company) -> skipping", channel, key, raw)
            continue
        norm = normalize(raw)
        if norm in failed:
            # Boundary rule 1 (don't re-validate a known-bad value) AND rule 2 (a
            # second provider surfaced the same rejected value) -> stop, return blank.
            logger.info("Deepline %s: %s repeated rejected value %s -> stopping (blank)", channel, key, raw)
            return {}
        verdict = validate(norm)
        brief = _verdict_brief(verdict)
        if verdict.get("valid"):
            logger.info("Deepline %s RESOLVED by %s = %s [%s] (providers tried=%d)",
                        channel, key, raw, brief, tried)
            return {"value": raw, "meta": verdict, "providers_tried": tried, "winner": key}
        logger.info("Deepline %s: %s returned %s but REJECTED (%s)", channel, key, raw, brief or "invalid")
        failed.add(norm)  # rejected once; a repeat from any later provider triggers the stop above
    logger.info("Deepline %s: no valid result after trying %d provider(s)", channel, tried)
    return {}


def _email_domain_ok(email: str, domain: str) -> bool:
    """True if `email` belongs to the target company `domain` (exact, or a sub/parent
    domain). Guards against enrich-person tools returning the person's email at a
    DIFFERENT current employer (e.g. a board seat). Set DEEPLINE_EMAIL_DOMAIN_MATCH=off
    to disable (e.g. for companies whose email domain differs from their website)."""
    if (os.environ.get("DEEPLINE_EMAIL_DOMAIN_MATCH") or "on").strip().lower() in ("off", "0", "false", "no"):
        return True
    if not domain or "@" not in (email or ""):
        return True
    return _same_company(email, domain)


def run_email_waterfall(inp: dict) -> dict:
    if not is_enabled() or not inp.get("domain") or not (inp.get("first_name") or inp.get("full_name")):
        return {}
    domain = inp.get("domain")
    return _run_waterfall(_order("DEEPLINE_EMAIL_ORDER", _DEFAULT_EMAIL_ORDER),
                          _EMAIL_ADAPTERS, inp, validate_email,
                          lambda e: e.lower().strip(), "email",
                          accept=lambda e: _email_domain_ok(e, domain))


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
# Per-tool test harness (for the /debug/email-tools + /debug/phone-tools endpoints).
# Runs EACH tool's real adapter (with its correct reveal flags) through the real
# extraction + validation, WITHOUT stopping at the first success and WITHOUT writing
# to HubSpot. `only` (a list of tool keys) limits which tools run — pass it to confirm
# just the unverified ones and avoid spending on tools already proven.
# ---------------------------------------------------------------------------
def _run_adapter_capturing(adapter, inp, include_raw):
    """Call an adapter, optionally capturing the raw execute_tool responses it makes."""
    if not include_raw:
        return adapter(inp), None
    _capture.buf = []
    try:
        value = adapter(inp)
        return value, list(_capture.buf)
    finally:
        _capture.buf = None


def test_email_tools(inp: dict, only: list | None = None, include_raw: bool = False) -> list:
    if not is_enabled():
        return []
    keys = list(only) if only else _order("DEEPLINE_EMAIL_ORDER", _DEFAULT_EMAIL_ORDER)
    domain = inp.get("domain") or ""
    out = []
    for key in keys:
        adapter = _EMAIL_ADAPTERS.get(key)
        if adapter is None:
            out.append({"tool": key, "error": "unknown tool"})
            continue
        try:
            raw, captured = _run_adapter_capturing(adapter, inp, include_raw)
        except Exception as exc:  # noqa: BLE001
            out.append({"tool": key, "found": False, "error": str(exc)})
            continue
        if not raw:
            entry = {"tool": key, "found": False}
        else:
            verdict = validate_email(raw.lower().strip())
            dom_ok = _email_domain_ok(raw, domain)
            entry = {"tool": key, "email": raw, "zerobounce": verdict.get("status"),
                     "valid": verdict.get("valid"), "domain_ok": dom_ok,
                     "accepted": bool(verdict.get("valid") and dom_ok)}
        if include_raw:
            entry["raw"] = captured
        out.append(entry)
    return out


def test_phone_tools(inp: dict, only: list | None = None, include_raw: bool = False) -> dict:
    if not is_enabled():
        return {}
    region = _region_for_country(inp.get("country"))
    keys = list(only) if only else _phone_order_for_region(region)
    name = inp.get("full_name") or ""
    out = []
    for key in keys:
        adapter = _PHONE_ADAPTERS.get(key)
        if adapter is None:
            out.append({"tool": key, "error": "unknown tool"})
            continue
        try:
            raw, captured = _run_adapter_capturing(adapter, inp, include_raw)
        except Exception as exc:  # noqa: BLE001
            out.append({"tool": key, "found": False, "error": str(exc)})
            continue
        if not raw:
            entry = {"tool": key, "found": False}
        else:
            verdict = validate_phone(re.sub(r"\D", "", raw), name, region)
            entry = {"tool": key, "phone": raw, "name_match": verdict.get("name_match"),
                     "valid": verdict.get("valid"), "validated": verdict.get("validated")}
        if include_raw:
            entry["raw"] = captured
        out.append(entry)
    return {"region": region, "results": out}


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


def _human_money(n) -> str:
    """863000000 -> '863M', 1200000000 -> '1.2B', 500000 -> '500K'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1e9:
        return f"{n / 1e9:.2f}".rstrip("0").rstrip(".") + "B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:.0f}"


def _pretty_round(r) -> str:
    """'series_f' -> 'Series F'."""
    return str(r).replace("_", " ").strip().title()


def _crustdata_pick_company(companies: list, domain: str) -> dict:
    """A domain can return several Crustdata records (e.g. the real 'Vercel' plus a tiny
    'Vercel Corp' / namesake). Pick the real one: prefer an exact website-domain match,
    then the largest headcount, then the most funding."""
    cands = [c for c in companies if isinstance(c, dict)]
    if not cands:
        return {}
    d = (domain or "").lower().lstrip("/").replace("www.", "")

    def score(c):
        emp = ((c.get("employee_metrics") or {}).get("latest_count")) or 0
        fund = c.get("crunchbase_total_investment_usd") or 0
        dom_ok = str(c.get("company_website_domain") or "").lower().replace("www.", "") == d
        return (1 if dom_ok else 0, emp, fund)

    return max(cands, key=score)


def get_company_funding(domain: str | None, name: str | None = None) -> dict:
    """Return {"display": str|None, "amount": float|None} for a company's funding.
    `display` -> HubSpot Funding field; `amount` (USD) -> the Tier-1 rule.

    Funding lives in Crustdata's company-DB SCREENER (crustdata_companydb_search) — the
    enrich tool returns firmographics only. We filter by website domain, pick the real
    company among any same-domain namesakes, and read crunchbase_total_investment_usd /
    last_funding_round_type / last_funding_date. Source overridable via DEEPLINE_FUNDING_TOOL."""
    if not is_enabled() or not (domain or name):
        return {"display": None, "amount": None}
    tool = os.environ.get("DEEPLINE_FUNDING_TOOL") or "crustdata_companydb_search"

    if "companydb_search" in tool:
        if domain:
            payload = {"filters": [{"filter_type": "company_website_domain", "type": "=", "value": domain}], "limit": 5}
        else:
            payload = {"filters": [{"filter_type": "company_name", "type": "(.)", "value": name}], "limit": 5}
        data = execute_tool(tool, payload)
        companies = _first(data, ("companies",))
        rec = _crustdata_pick_company(companies if isinstance(companies, list) else [], domain or "")
        total = rec.get("crunchbase_total_investment_usd")
        last_round = rec.get("last_funding_round_type")
        last_date = rec.get("last_funding_date")
    else:
        # Other funding sources (flat payload) — e.g. a per-domain enrich tool.
        payload = {k: v for k, v in {"companyDomain": domain, "company_domain": domain,
                                     "domain": domain, "company_name": name, "name": name}.items() if v}
        data = execute_tool(tool, payload)
        total = _first(data, ("crunchbase_total_investment_usd", "total_funding_usd", "total_funding",
                              "total_funding_amount", "funding_total", "total_raised", "funding_amount"))
        last_round = _first(data, ("last_funding_round_type", "last_funding_type", "funding_stage", "latest_round"))
        last_date = _first(data, ("last_funding_date", "latest_funding_date"))

    amount = _parse_money(total)
    parts = []
    if amount:
        parts.append(f"Total raised: ${_human_money(amount)}")
    if last_round:
        parts.append(f"Last round: {_pretty_round(last_round)}")
    if last_date:
        parts.append(str(last_date)[:10])  # trim the T00:00:00
    display = " | ".join(parts) if parts else None
    return {"display": display, "amount": amount}
