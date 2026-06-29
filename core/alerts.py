"""
alerts.py
---------
Email error alerts. When the pipeline hits a real failure (an unhandled error,
a HubSpot/Forager API error, Forager out-of-credits, etc.), send an email with
the reason + traceback so the team finds out without watching the logs.

Sends over SMTP, so it works with any provider (Gmail, Brevo, SendGrid, Mailgun,
Postmark, …). It stays OFF until configured — if the SMTP/recipient env vars are
missing, alerts are skipped (logged only) and nothing breaks. The alerter never
raises: a failure to send is swallowed so it can't take down a request.

Configuration (env):
  SMTP_HOST          e.g. smtp.gmail.com
  SMTP_PORT          e.g. 587 (STARTTLS) or 465 (SSL); default 587
  SMTP_USER          SMTP username (often the from-address)
  SMTP_PASSWORD      SMTP password / app password
  SMTP_USE_TLS       "false" to disable STARTTLS on port 587 (default true)
  ALERT_EMAIL_FROM   from address (defaults to SMTP_USER)
  ALERT_EMAIL_TO     comma-separated recipients (required to enable alerts)
"""

import logging
import os
import smtplib
import ssl
import time
import traceback
from email.message import EmailMessage

logger = logging.getLogger(__name__)

_SMTP_HOST = os.environ.get("SMTP_HOST")
_SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
_SMTP_USER = os.environ.get("SMTP_USER")
_SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").strip().lower() != "false"
_ALERT_FROM = os.environ.get("ALERT_EMAIL_FROM") or _SMTP_USER
# Default recipient; override anytime with the ALERT_EMAIL_TO env var (comma-separated).
_DEFAULT_ALERT_TO = "doshidev58@gmail.com"
_ALERT_TO = [e.strip() for e in os.environ.get("ALERT_EMAIL_TO", _DEFAULT_ALERT_TO).split(",") if e.strip()]

# Don't flood: suppress identical alerts within this window (per process).
_THROTTLE_SECONDS = int(os.environ.get("ALERT_THROTTLE_SECONDS", "300"))
_recent_sent: dict[str, float] = {}


def is_configured() -> bool:
    return bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASSWORD and _ALERT_FROM and _ALERT_TO)


def _build_body(summary: str, error, context: dict | None) -> str:
    lines = [summary, ""]
    if context:
        lines.append("Context:")
        for key, value in context.items():
            lines.append(f"  {key}: {value}")
        lines.append("")
    if error is not None:
        lines.append(f"Error: {type(error).__name__}: {error}")
        tb = "".join(traceback.format_exception(type(error), error, getattr(error, "__traceback__", None)))
        if tb.strip() and tb.strip() != "NoneType: None":
            lines += ["", "Traceback:", tb]
    return "\n".join(lines)


def _build_message(summary: str, error=None, context: dict | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[Forager x HubSpot] {summary}"
    msg["From"] = _ALERT_FROM
    msg["To"] = ", ".join(_ALERT_TO)
    msg.set_content(_build_body(summary, error, context))
    return msg


def _transmit(msg: EmailMessage) -> None:
    """Open the SMTP connection and send the message. Raises on any failure."""
    if _SMTP_PORT == 465:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=20,
                              context=ssl.create_default_context()) as server:
            server.login(_SMTP_USER, _SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=20) as server:
            if _USE_TLS:
                server.starttls(context=ssl.create_default_context())
            server.login(_SMTP_USER, _SMTP_PASSWORD)
            server.send_message(msg)


def send_error_alert(summary: str, error=None, context: dict | None = None) -> bool:
    """Email an error alert. Returns True if sent, False if skipped/failed. Never raises."""
    if not is_configured():
        logger.info("Email alerts not configured; skipping alert: %s", summary)
        return False

    # Throttle identical (summary + error) alerts.
    key = f"{summary}|{type(error).__name__ if error else ''}|{str(error)[:160]}"
    now = time.monotonic()
    last = _recent_sent.get(key)
    if last is not None and (now - last) < _THROTTLE_SECONDS:
        logger.info("Suppressing duplicate alert (throttled): %s", summary)
        return False

    try:
        _transmit(_build_message(summary, error, context))
        # Record only after a successful send, so a failed send doesn't throttle (block) retries.
        _recent_sent[key] = now
        logger.info("Sent error alert email to %s: %s", _ALERT_TO, summary)
        return True
    except Exception as exc:  # noqa: BLE001 - the alerter must never break the request
        logger.warning("Failed to send alert email (%s): %s", summary, exc)
        return False


def config_summary() -> dict:
    """Non-secret view of the current alert config, for debugging (no password)."""
    return {
        "configured": is_configured(),
        "smtp_host": _SMTP_HOST,
        "smtp_port": _SMTP_PORT,
        "smtp_user": _SMTP_USER,
        "smtp_password_set": bool(_SMTP_PASSWORD),
        "use_tls": _USE_TLS,
        "from": _ALERT_FROM,
        "to": _ALERT_TO,
    }


def test_send() -> dict:
    """Attempt a real test send and surface the ACTUAL error (for /debug/alert-test).

    Unlike send_error_alert(), this bypasses the throttle (so repeated calls always
    attempt) and returns the real SMTP error string instead of swallowing it.
    """
    result = {"alerts_configured": is_configured(), "email_sent": False,
              "error": None, "config": config_summary()}
    if not is_configured():
        result["error"] = ("Not configured. Need SMTP_HOST, SMTP_USER, SMTP_PASSWORD, "
                            "ALERT_EMAIL_TO (and ALERT_EMAIL_FROM or SMTP_USER).")
        return result
    msg = _build_message(
        "Test alert",
        RuntimeError("This is a test alert — your error-email setup is working."),
        {"trigger": "manual /debug/alert-test"},
    )
    try:
        _transmit(msg)
        result["email_sent"] = True
        logger.info("Sent test alert email to %s", _ALERT_TO)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("Test alert send failed: %s", result["error"])
    return result
