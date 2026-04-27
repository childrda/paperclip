"""Unit tests for entity-resolution primitives."""

from __future__ import annotations

from foia.entity_resolution import (
    canonicalize_email,
    canonicalize_name,
    extract_signature_emails,
    is_internal_email,
    parse_address,
    pick_display_name,
)


# ---------------------------------------------------------------------------
# canonicalize_email / canonicalize_name
# ---------------------------------------------------------------------------


def test_canonicalize_email_lowercases_and_trims():
    assert canonicalize_email("  Jane.Doe@EXAMPLE.com  ") == "jane.doe@example.com"


def test_canonicalize_email_preserves_plus_addressing():
    # Plus-addressing is deliberately NOT stripped to avoid cross-merging
    # people who actually use distinct sub-addresses.
    assert canonicalize_email("jane+foia@example.com") == "jane+foia@example.com"


def test_canonicalize_email_empty():
    assert canonicalize_email("") == ""
    assert canonicalize_email(None) == ""  # type: ignore[arg-type]


def test_canonicalize_name_collapses_whitespace():
    assert canonicalize_name("  Jane    Doe  ") == "Jane Doe"


def test_canonicalize_name_strips_quotes_and_trailing_punct():
    assert canonicalize_name('"Jane Doe",') == "Jane Doe"


def test_canonicalize_name_none_and_empty():
    assert canonicalize_name(None) is None
    assert canonicalize_name("") is None
    assert canonicalize_name("   ") is None


# ---------------------------------------------------------------------------
# parse_address
# ---------------------------------------------------------------------------


def test_parse_address_name_and_email():
    p = parse_address("Jane Doe <jane@example.com>")
    assert p.display_name == "Jane Doe"
    assert p.email == "jane@example.com"
    assert p.is_empty is False


def test_parse_address_bare_email():
    p = parse_address("jane@example.com")
    assert p.display_name is None
    assert p.email == "jane@example.com"


def test_parse_address_uppercase_email():
    p = parse_address("Jane <Jane@EXAMPLE.com>")
    assert p.email == "jane@example.com"


def test_parse_address_empty():
    p = parse_address("")
    assert p.is_empty is True
    assert p.email == ""


# ---------------------------------------------------------------------------
# is_internal_email
# ---------------------------------------------------------------------------


def test_is_internal_email_exact_match():
    assert is_internal_email("a@district.org", ("district.org",))


def test_is_internal_email_subdomain_match():
    # A host mail.district.org should count as internal.
    assert is_internal_email("a@mail.district.org", ("district.org",))


def test_is_internal_email_no_match():
    assert not is_internal_email("a@elsewhere.com", ("district.org",))


def test_is_internal_email_empty_and_malformed():
    assert not is_internal_email("", ("d.org",))
    assert not is_internal_email("no-at-sign", ("d.org",))


def test_is_internal_email_leading_dot_tolerated():
    assert is_internal_email("a@district.org", (".district.org",))


# ---------------------------------------------------------------------------
# extract_signature_emails
# ---------------------------------------------------------------------------


def test_extract_signature_uses_marker():
    body = (
        "Hi team,\n\n"
        "Please see the attached.\n\n"
        "-- \n"
        "Jane Doe\n"
        "Principal | jane.doe@district.org\n"
        "Cell: (555) 123-4567\n"
    )
    assert extract_signature_emails(body) == ["jane.doe@district.org"]


def test_extract_signature_multiple_emails():
    body = (
        "Regards,\n"
        "Jane Doe\n"
        "jane@district.org\n"
        "backup: jane.doe@gmail.example\n"
    )
    assert extract_signature_emails(body) == [
        "jane@district.org",
        "jane.doe@gmail.example",
    ]


def test_extract_signature_without_marker_uses_tail():
    body = (
        "Hey\n\nthis\nhas\nmany\nlines\nand ends with\n"
        "admin@district.org"
    )
    # Last 6 non-empty lines are considered; the email must appear in that tail.
    assert "admin@district.org" in extract_signature_emails(body)


def test_extract_signature_no_email_returns_empty():
    assert extract_signature_emails("Hi, just checking in. --\nJane") == []


def test_extract_signature_handles_none_and_empty():
    assert extract_signature_emails("") == []
    assert extract_signature_emails(None) == []


def test_extract_signature_dedupes_preserving_order():
    body = (
        "Best,\n"
        "jane@district.org\n"
        "jane@district.org (primary)\n"
        "alt: alt@example.com\n"
    )
    assert extract_signature_emails(body) == ["jane@district.org", "alt@example.com"]


# ---------------------------------------------------------------------------
# pick_display_name
# ---------------------------------------------------------------------------


def test_pick_display_name_prefers_highest_count():
    assert pick_display_name({"Jane Doe": 3, "J. Doe": 1}, "f") == "Jane Doe"


def test_pick_display_name_breaks_tie_on_length():
    assert pick_display_name({"Jane": 2, "Jane Doe": 2}, "f") == "Jane Doe"


def test_pick_display_name_breaks_tie_alphabetically():
    # Same count, same length — alpha asc.
    chosen = pick_display_name({"Bob A": 1, "Ann B": 1}, "f")
    assert chosen == "Ann B"


def test_pick_display_name_fallback_on_empty():
    assert pick_display_name({}, "fallback@example.com") == "fallback@example.com"
