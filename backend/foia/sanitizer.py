"""HTML sanitization for email bodies.

Strips JavaScript, tracking pixels, and external references. The output
is intended for display/indexing only; the original bytes are preserved
verbatim in the `raw_content` table.
"""

from __future__ import annotations

import re
from typing import Iterable

import bleach
from bs4 import BeautifulSoup

_ALLOWED_TAGS: frozenset[str] = frozenset({
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "span",
    "strong", "sub", "sup", "table", "tbody", "td", "th", "thead", "tr",
    "u", "ul",
})

_ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title"],
    "abbr": ["title"],
}

_ALLOWED_PROTOCOLS: frozenset[str] = frozenset({"http", "https", "mailto"})

_TRACKING_PIXEL_MAX_DIM = 2  # width or height <= 2 px is treated as a pixel


def _drop_external_references(html: str) -> str:
    """Remove <img>, <link>, <iframe>, <object>, <embed>, <video>, <audio>, <source>,
    <track>, <script>, <style> elements and any tracking pixels.

    bleach already strips most of these via the tag allowlist, but we pre-parse
    with BeautifulSoup so that tracking-pixel images, embedded SVGs, data:/cid:
    URLs, and on* attributes are handled predictably regardless of bleach's tag
    policy changes.
    """
    soup = BeautifulSoup(html, "html.parser")

    kill_tags: Iterable[str] = (
        "script", "style", "link", "iframe", "object", "embed",
        "video", "audio", "source", "track", "meta", "base",
        "img", "picture", "svg", "canvas", "form", "input",
        "button", "select", "textarea",
    )
    for tag_name in kill_tags:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]

    return str(soup)


_STYLE_URL_RE = re.compile(r"url\s*\(", re.IGNORECASE)


def sanitize_html(html: str | None) -> str:
    """Return a safe HTML string.

    Empty or None input yields an empty string.
    """
    if not html:
        return ""

    pre = _drop_external_references(html)

    cleaned = bleach.clean(
        pre,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )

    # Belt-and-suspenders: any leaked url(...) in inline style attributes
    # (bleach strips `style` by default since it's not in attrs, but guard anyway).
    cleaned = _STYLE_URL_RE.sub("url-removed(", cleaned)

    return cleaned


def html_to_text(html: str | None) -> str:
    """Best-effort plain-text fallback from HTML for search/indexing."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)
