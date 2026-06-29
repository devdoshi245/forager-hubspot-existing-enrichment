"""
auth.py
-------
Lightweight shared-secret auth for the sensitive endpoints (webhooks, /enrich/*,
/debug/*). The service is on the public internet and its endpoints spend real money
(Forager credits) and write to the CRM, so they must not be open to anyone who learns
the URL.

Set APP_SECRET in the environment and send it on every request as either the header
`X-App-Secret: <secret>` or the query string `?token=<secret>` (HubSpot lets you add a
query param to the webhook Target URL, e.g. .../webhook?token=<secret>).

If APP_SECRET is unset, endpoints stay OPEN but every call logs a loud warning — so
existing setups keep working, but you MUST set it before going live with real
credentials. (A stronger option is verifying HubSpot's X-HubSpot-Signature-V3 HMAC,
which needs the app client secret; the shared secret is simpler and proxy-agnostic.)
"""

import hmac
import logging
import os

logger = logging.getLogger(__name__)

_APP_SECRET = os.environ.get("APP_SECRET")


def is_enabled() -> bool:
    return bool(_APP_SECRET)


def authorize(request) -> bool:
    """True if the request may proceed. Enforces APP_SECRET when it is configured;
    warns and allows when it is not (so nothing breaks before you set it)."""
    if not _APP_SECRET:
        logger.warning("SECURITY: %s served WITHOUT auth — set APP_SECRET to lock it down.", request.path)
        return True
    provided = request.headers.get("X-App-Secret") or request.args.get("token") or ""
    return hmac.compare_digest(provided, _APP_SECRET)
