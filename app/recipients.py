"""Per-flight alert recipients.

The default address (MAIL_TO, falling back to SMTP_USER) always receives every
alert. Each flight can additionally carry its own list of extra addresses,
stored normalised and comma-separated on the flight row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import settings

MAX_EXTRA_RECIPIENTS = 10

# Deliberately pragmatic: catches typos without rejecting real addresses.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_SPLIT_RE = re.compile(r"[\s,;]+")


@dataclass
class ParsedRecipients:
    emails: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    dropped_default: bool = False
    truncated: bool = False

    @property
    def error(self) -> str:
        if self.invalid:
            shown = ", ".join(self.invalid[:3])
            more = f" (+{len(self.invalid) - 3} more)" if len(self.invalid) > 3 else ""
            return f"Not a valid email address: {shown}{more}"
        if self.truncated:
            return f"At most {MAX_EXTRA_RECIPIENTS} extra recipients per flight."
        return ""


def default_recipient() -> str:
    return (settings.effective_mail_to or "").strip().lower()


def is_valid_email(address: str) -> bool:
    return bool(_EMAIL_RE.match(address or "")) and len(address) <= 254


def parse_recipients(raw: str) -> ParsedRecipients:
    """Split on commas/semicolons/whitespace, validate, lowercase, dedupe.

    The default address is removed if listed — it always receives alerts
    anyway, and storing it would make it look removable.
    """
    result = ParsedRecipients()
    seen: set[str] = set()
    default = default_recipient()

    for token in _SPLIT_RE.split(raw or ""):
        address = token.strip().strip("<>").lower()
        if not address:
            continue
        if not is_valid_email(address):
            result.invalid.append(token.strip())
            continue
        if address == default:
            result.dropped_default = True
            continue
        if address in seen:
            continue
        seen.add(address)
        result.emails.append(address)

    if len(result.emails) > MAX_EXTRA_RECIPIENTS:
        result.emails = result.emails[:MAX_EXTRA_RECIPIENTS]
        result.truncated = True
    return result


def serialise(emails: list[str]) -> str:
    return ",".join(emails)


def extra_recipients(stored: str) -> list[str]:
    """Read a flight's stored list. Tolerates hand-edited or legacy values."""
    return parse_recipients(stored or "").emails


def all_recipients(stored: str) -> list[str]:
    """Default first, then the flight's extras."""
    default = default_recipient()
    return ([default] if default else []) + extra_recipients(stored)
