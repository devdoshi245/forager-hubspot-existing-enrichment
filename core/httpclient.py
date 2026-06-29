"""
httpclient.py
-------------
A shared requests.Session with retry/backoff, used for every external API call
(Forager and HubSpot). It retries on connection errors and on 429 / 5xx with
exponential backoff, honouring any Retry-After header, so a transient blip or a
rate-limit doesn't abort the whole pipeline. One Session per process gives
connection pooling too. The caller's per-request timeout is preserved.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_METHODS = frozenset(["GET", "POST", "PUT", "PATCH", "DELETE"])
_STATUS = (429, 500, 502, 503, 504)


def make_session(total: int = 3, backoff: float = 1.0) -> requests.Session:
    common = dict(
        total=total, connect=total, read=total, status=total,
        backoff_factor=backoff, status_forcelist=_STATUS,
        respect_retry_after_header=True, raise_on_status=False,
    )
    try:  # urllib3 >= 1.26
        retry = Retry(allowed_methods=_METHODS, **common)
    except TypeError:  # older urllib3
        retry = Retry(method_whitelist=_METHODS, **common)
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
