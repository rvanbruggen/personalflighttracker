"""Phone push via IFTTT Webhooks (an IFTTT Pro service).

One HTTPS POST per alert to the Maker endpoint. The applet on the IFTTT side —
Webhooks "Receive a web request" -> Notifications "Send a rich notification
from the IFTTT app" — maps the three ingredients:

    value1  title    e.g. "KL1705 AMS→LIS DELAYED +45min"
    value2  message  what changed, one line per change
    value3  link     the flight page (needs PUBLIC_BASE_URL), or ""

Independent of Gmail: a failed SMTP login does not stop the push, and a failed
push does not stop the email.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import settings

log = logging.getLogger(__name__)

WEBHOOK_URL = "https://maker.ifttt.com/trigger/{event}/with/key/{key}"
ATTEMPTS = 2  # one retry on a network error or a 5xx; 4xx means fix the config


@dataclass
class PushMessage:
    title: str
    message: str
    link: str = ""


@dataclass
class PushResult:
    sent: bool = False
    error: str = ""


def _redact(text: str) -> str:
    """The key lives in the URL; keep it out of logs and the change history."""
    key = settings.ifttt_webhook_key
    return text.replace(key, "***") if key else text


def _post(url: str, payload: dict) -> httpx.Response:
    return httpx.post(
        url,
        json=payload,
        timeout=settings.ifttt_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
    )


def send_push(push: PushMessage) -> PushResult:
    """Blocking — call from a worker thread. Not configured is not an error:
    it returns an unsent result with no error, so email-only setups stay quiet."""
    result = PushResult()
    if not settings.notifications_enabled or not settings.ifttt_configured:
        return result

    url = WEBHOOK_URL.format(event=settings.ifttt_event.strip(), key=settings.ifttt_webhook_key.strip())
    payload = {"value1": push.title, "value2": push.message, "value3": push.link}

    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = _post(url, payload)
        except httpx.HTTPError as exc:
            result.error = _redact(f"IFTTT webhook unreachable: {exc}")
            log.warning("%s (attempt %d/%d)", result.error, attempt, ATTEMPTS)
            continue
        if response.status_code < 300:
            result.sent, result.error = True, ""
            log.info("Phone push sent via IFTTT: %s", push.title)
            return result
        detail = _redact(response.text.strip())[:200]
        if response.status_code in (401, 403):
            result.error = f"IFTTT rejected the webhook key (HTTP {response.status_code}) — check IFTTT_WEBHOOK_KEY"
        else:
            result.error = f"IFTTT webhook returned HTTP {response.status_code}: {detail}"
        log.warning("%s (attempt %d/%d)", result.error, attempt, ATTEMPTS)
        if response.status_code < 500:
            break
    return result
