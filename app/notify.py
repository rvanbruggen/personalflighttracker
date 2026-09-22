"""Alerting: a readable email to your inbox (and any extra recipients) over
Gmail SMTP, plus a phone push through an IFTTT webhook (see push.py).

The two channels are independent: the push goes out even when SMTP is not
configured or Gmail rejects the login.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Optional, Sequence

from .config import settings
from .push import PushMessage, send_push

log = logging.getLogger(__name__)


@dataclass
class NotifyResult:
    inbox_sent: bool = False
    ifttt_sent: bool = False
    extra_sent: list[str] = field(default_factory=list)
    extra_failed: list[str] = field(default_factory=list)
    error: str = ""  # email delivery
    push_error: str = ""  # phone push; reported separately so email stays authoritative

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


EXTRA_RECIPIENT_FOOTER = (
    "\n\n--\nYou are receiving this because you were added as a recipient "
    "for this flight on {app}. Ask the person tracking it to remove you."
)


def send_alert(
    subject: str,
    body: str,
    extra_recipients: Sequence[str] = (),
    *,
    push: Optional[PushMessage] = None,
    include_default: bool = True,
) -> NotifyResult:
    """Blocking — call from a worker thread, not the event loop.

    `subject` and `body` go to your inbox. `push`, when given, also goes to
    your phone via the IFTTT webhook — leave it out to keep a message off the
    phone.

    Each of `extra_recipients` gets an individual copy, so nobody sees anyone
    else's address and one bad address cannot block the others.

    `include_default=False` sends only to the extras (e.g. welcoming someone
    added later).
    """
    result = NotifyResult()

    if not settings.notifications_enabled:
        result.error = "notifications disabled (NOTIFICATIONS_ENABLED=false)"
        return result

    if push is not None:
        pushed = send_push(push)
        result.ifttt_sent, result.push_error = pushed.sent, pushed.error

    if not settings.smtp_configured:
        result.error = (
            "SMTP is not configured — set SMTP_USER and SMTP_PASSWORD "
            "(a Gmail app password) in .env"
        )
        log.warning("Skipping alert %r: %s", subject, result.error)
        return result

    subject = with_prefix(subject)
    messages: list[tuple[str, EmailMessage]] = []
    if include_default:
        messages.append(
            ("inbox", _build(settings.effective_mail_to, subject, body))
        )
    default = settings.effective_mail_to.strip().lower()
    extra_body = body + EXTRA_RECIPIENT_FOOTER.format(app=settings.app_name)
    for address in dict.fromkeys(a.strip().lower() for a in extra_recipients):
        if address and address != default:
            messages.append(
                (f"extra:{address}", _build(address, subject, extra_body))
            )

    if not messages:
        return result  # nothing to send is not an error

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
                        result.extra_sent.append(kind.split(":", 1)[1])
                except smtplib.SMTPException as exc:
                    log.error("Failed to send %s copy: %s", kind, exc)
                    if kind.startswith("extra:"):
                        # A bad extra address is reported, not treated as a
                        # failure of the whole alert.
                        result.extra_failed.append(kind.split(":", 1)[1])
                    else:
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

    if result.inbox_sent or result.ifttt_sent or result.extra_sent:
        log.info(
            "Alert sent (inbox=%s ifttt=%s extra=%d failed=%d): %s",
            result.inbox_sent,
            result.ifttt_sent,
            len(result.extra_sent),
            len(result.extra_failed),
            subject,
        )
    return result


def send_test_alert() -> NotifyResult:
    return send_alert(
        subject="test alert",
        body=(
            "This is a test alert from your Personal Flight Tracker.\n\n"
            "If this landed in your inbox, Gmail SMTP works.\n"
            "If your phone buzzed too, the IFTTT webhook works.\n"
        ),
        push=PushMessage(
            title="Test alert from flight tracker",
            message="If you can read this, the IFTTT webhook works.",
            link=settings.app_link("/"),
        ),
    )
