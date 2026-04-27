"""Entity-resolution primitives.

Pure functions for normalizing emails/names and extracting signature
email addresses from a body. The DB-aware driver lives in
:mod:`foia.er_driver`.

Design decision — we only auto-merge on *exact* canonical-email match.
Same-name matching across different emails is deliberately *not*
automatic because common names (e.g. "John Smith") would collide across
unrelated people. The CLI's ``merge`` subcommand exists for those cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr

# Conservative, standard email regex. Purely for signature scraping — the
# real validation happened upstream in the ingestion layer.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
)

# Common sign-off lines that mark the start of a signature block.
_SIGNATURE_MARKERS = {
    "-- ", "--", "best,", "best regards,", "regards,", "sincerely,",
    "thanks,", "thanks!", "thank you,", "cheers,", "many thanks,",
    "warmly,", "respectfully,", "kind regards,",
}


@dataclass(frozen=True)
class ParsedAddress:
    """A single (name, email) pair."""
    display_name: str | None
    email: str                     # canonicalized
    raw: str                       # original string as stored

    @property
    def is_empty(self) -> bool:
        return not self.email


def canonicalize_email(email: str) -> str:
    """Lowercase and trim — the conservative canonicalization.

    We do NOT strip plus-addressing or Gmail dots: those mappings are
    aggressive and can cross-merge unrelated people when inboxes are
    configured with non-default rules.
    """
    return (email or "").strip().lower()


def canonicalize_name(name: str | None) -> str | None:
    """Collapse whitespace, strip surrounding quotes/punctuation.

    Returns None when the result is empty so callers can use truthiness
    to check.
    """
    if not name:
        return None
    cleaned = re.sub(r"\s+", " ", name).strip()
    cleaned = cleaned.strip(" .,\"'")
    return cleaned or None


def parse_address(raw: str) -> ParsedAddress:
    """Parse a stored address string (e.g. ``Name <a@b.com>`` or just ``a@b.com``).

    Always returns a :class:`ParsedAddress` — check ``is_empty`` to know
    whether the parse succeeded.
    """
    raw = (raw or "").strip()
    if not raw:
        return ParsedAddress(None, "", raw)
    name, addr = parseaddr(raw)
    return ParsedAddress(
        display_name=canonicalize_name(name),
        email=canonicalize_email(addr),
        raw=raw,
    )


def is_internal_email(email: str, internal_domains: tuple[str, ...]) -> bool:
    """True iff the email's domain (or a parent domain) is declared internal."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    for d in internal_domains:
        d = d.lower().lstrip(".")
        if domain == d or domain.endswith("." + d):
            return True
    return False


def extract_signature_emails(body_text: str | None) -> list[str]:
    """Return canonical emails found within the signature region of a body.

    Strategy:
      * Look at the final ~20 non-empty lines.
      * If a sign-off marker is present, take everything from that marker
        to the end as the signature block.
      * Otherwise treat the final 6 non-empty lines as a heuristic
        signature block.
      * Extract email-shaped substrings from that region.

    Duplicates are removed while preserving order of first appearance.
    """
    if not body_text:
        return []

    lines = body_text.splitlines()
    # Find the last sign-off marker.
    marker_index: int | None = None
    for i, line in enumerate(lines):
        if line.strip().lower() in _SIGNATURE_MARKERS:
            marker_index = i

    if marker_index is not None:
        sig_region = "\n".join(lines[marker_index + 1:])
    else:
        # Fall back: consider the last 6 non-empty lines as the sig area.
        tail = [ln for ln in lines[-20:] if ln.strip()][-6:]
        sig_region = "\n".join(tail)

    seen: dict[str, None] = {}
    for m in _EMAIL_RE.finditer(sig_region):
        e = canonicalize_email(m.group(0))
        if e and e not in seen:
            seen[e] = None
    return list(seen.keys())


def pick_display_name(
    name_counts: dict[str, int],
    fallback_email: str,
) -> str:
    """Choose the best display name from a frequency table.

    Priority: most frequent → longest → lexicographic. Fall back to the
    canonical email if no names have been observed.
    """
    if not name_counts:
        return fallback_email
    ranked = sorted(
        name_counts.items(),
        key=lambda kv: (-kv[1], -len(kv[0]), kv[0]),
    )
    return ranked[0][0]


__all__ = [
    "ParsedAddress",
    "canonicalize_email",
    "canonicalize_name",
    "extract_signature_emails",
    "is_internal_email",
    "parse_address",
    "pick_display_name",
]
