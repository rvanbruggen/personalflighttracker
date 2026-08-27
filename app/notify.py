"""Alerting: a readable email to your inbox, plus a hashtagged copy to the IFTTT
Email trigger address which fires the phone notification applet.

IFTTT's Email service only fires for mail sent *from* the address registered
with IFTTT — so both copies go out over the same Gmail SMTP session.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class NotifyResult:
    inbox_sent: bool = False
    ifttt_sent: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def with_prefix(subject: str) -> str:
    """'KL1705 DELAYED' -> 'PFT KL1705 DELAYED'. Idempotent."""
    prefix = settings.email_subject_prefix.strip()
    if not prefix:
        return subject
    if subject.strip().upper().startswith(prefix.upper()):
        return subject
    return f"{prefix} {subject}"


def _build(to_address: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.effective_mail_from
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    return message


def send_alert(subject: str, body: str, ifttt_subject: Optional[str] = None) -> NotifyResult:
    """Blocking — call from a worker thread, not the event loop.

    `subject` goes to your inbox; `ifttt_subject` (hashtag appended) goes to
    IFTTT. IFTTT matches on the hashtag, so keep it in the subject line.
    """
    result = NotifyResult()

    if not settings.notifications_enabled:
        result.error = "notifications disabled (NOTIFICATIONS_ENABLED=false)"
        return result

    if not settings.smtp_configured:
        result.error = (
            "SMTP is not configured — set SMTP_USER and SMTP_PASSWORD "
            "(a Gmail app password) in .env"
        )
        log.warning("Skipping alert %r: %s", subject, result.error)
        return result

    subject = with_prefix(subject)
    messages: list[tuple[str, EmailMessage]] = [
        ("inbox", _build(settings.effective_mail_to, subject, body))
    ]

    if settings.ifttt_enabled and settings.ifttt_trigger_email:
        tag = settings.ifttt_hashtag.strip()
        trigger_subject = with_prefix(ifttt_subject) if ifttt_subject else subject
        if tag and tag.lower() not in trigger_subject.lower():
            trigger_subject = f"{trigger_subject} {tag}"
        messages.append(
            ("ifttt", _build(settings.ifttt_trigger_email, trigger_subject, body))
        )

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(settings.smtp_user, settings.smtp_password)
            for kind, message in messages:
                try:
                    server.send_message(message)
                    if kind == "inbox":
                        result.inbox_sent = True
                    else:
                        result.ifttt_sent = True
                except smtplib.SMTPException as exc:
                    log.error("Failed to send %s copy: %s", kind, exc)
                    result.error = f"{kind} copy failed: {exc}"
    except smtplib.SMTPAuthenticationError as exc:
        result.error = (
            "Gmail rejected the login. Use a 16-character app password "
            f"(not your account password), 2FA must be on. ({exc.smtp_code})"
        )
        log.error(result.error)
    except (smtplib.SMTPException, OSError) as exc:
        result.error = f"SMTP error: {exc}"
        log.error("SMTP failure sending %r: %s", subject, exc)

    if result.inbox_sent or result.ifttt_sent:
        log.info(
            "Alert sent (inbox=%s ifttt=%s): %s",
            result.inbox_sent,
            result.ifttt_sent,
            subject,
        )
    return result


def send_test_alert() -> NotifyResult:
    return send_alert(
        subject="test alert",
        body=(
            "This is a test alert from your Personal Flight Tracker.\n\n"
            "If this landed in your inbox, Gmail SMTP works.\n"
            "If your phone buzzed too, the IFTTT applet works.\n"
        ),
        ifttt_subject="TEST alert from flight tracker",
    )
