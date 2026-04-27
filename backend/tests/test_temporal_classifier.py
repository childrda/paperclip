"""Phase 4 — temporal entity classifier tests.

The legal defensibility argument requires that classifications for an
email dated March 2022 reflect what the corpus *knew about that person
in March 2022*, not what we know today. These tests pin that down.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from foia.er_driver import (
    affiliation_history,
    classify_person_at,
    is_internal_at,
    run_resolution,
)


def _ins_email(
    conn,
    *,
    mbox_index: int,
    from_addr: str,
    date_sent: str,
    to=None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO emails (
            mbox_source, mbox_index, from_addr, to_addrs,
            date_sent, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "t.mbox", mbox_index, from_addr,
            json.dumps(to or []),
            date_sent,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Affiliations are recorded
# ---------------------------------------------------------------------------


def test_resolve_records_email_domain_affiliations(db_conn):
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="Jane <jane@district.example.org>",
        date_sent="2024-03-15T10:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=("district.example.org",))

    pid = db_conn.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
    history = affiliation_history(db_conn, pid)
    types = {h["affiliation_type"] for h in history}
    # Only raw evidence is stored; is_internal is computed at query time.
    assert types == {"email_domain"}

    domain_rows = [h for h in history if h["affiliation_type"] == "email_domain"]
    assert len(domain_rows) == 1
    assert domain_rows[0]["affiliation_value"] == "district.example.org"
    # And the interpretation, supplied at query time, is True.
    assert is_internal_at(
        db_conn, pid, "2024-03-15T10:00:00+00:00",
        internal_domains=("district.example.org",),
    ) is True


def test_observation_time_uses_email_date_not_now(db_conn):
    """If the email's Date header says 2022-03-01, the affiliation row
    should be tagged with that — never with today's clock."""
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="alice@x.org",
        date_sent="2022-03-01T09:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
    history = affiliation_history(db_conn, pid, affiliation_type="email_domain")
    assert all(h["observed_at"] == "2022-03-01T09:00:00+00:00" for h in history)


def test_falls_back_to_ingested_at_when_no_date(db_conn):
    cur = db_conn.execute(
        """
        INSERT INTO emails (
            mbox_source, mbox_index, from_addr,
            date_sent, ingested_at
        ) VALUES ('t.mbox', 0, 'a@x.org', NULL, ?)
        """,
        ("2024-01-01T00:00:00+00:00",),
    )
    db_conn.commit()
    _ = cur
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
    rows = affiliation_history(db_conn, pid)
    assert all(r["observed_at"] == "2024-01-01T00:00:00+00:00" for r in rows)


# ---------------------------------------------------------------------------
# Point-in-time classification
# ---------------------------------------------------------------------------


def test_internal_then_external_over_time(db_conn):
    """Same human, two emails: internal in 2022, external in 2024.

    Classifying for a 2022 date should answer 'internal'; classifying
    for a 2024 date should answer 'external'. This is the spec's
    legal-defensibility requirement.
    """
    # 2022: emails sent from district address
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="Jane <jane.doe@district.example.org>",
        date_sent="2022-03-01T09:00:00+00:00",
    )
    # 2024: same Jane, now using a personal address (manually merged
    # later, but here we set up the data so the affiliations precede
    # the merge).
    _ins_email(
        db_conn, mbox_index=1,
        from_addr="Jane <jane.doe@personal.example>",
        date_sent="2024-09-01T09:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=("district.example.org",))

    # The two emails create two persons (different addresses); merge
    # them so the timeline lives on a single person.
    rows = db_conn.execute(
        "SELECT person_id, email FROM person_emails ORDER BY email"
    ).fetchall()
    by_email = {r["email"]: r["person_id"] for r in rows}
    from foia.er_driver import merge_persons
    merge_persons(
        db_conn,
        loser_id=by_email["jane.doe@personal.example"],
        winner_id=by_email["jane.doe@district.example.org"],
    )
    pid = by_email["jane.doe@district.example.org"]

    # Point-in-time queries: rules are supplied at query time.
    rules = ("district.example.org",)
    assert is_internal_at(
        db_conn, pid, "2022-06-01T00:00:00+00:00",
        internal_domains=rules,
    ) is True
    assert is_internal_at(
        db_conn, pid, "2025-01-01T00:00:00+00:00",
        internal_domains=rules,
    ) is False
    # Earlier than any observation → unknown.
    assert is_internal_at(
        db_conn, pid, "2020-01-01T00:00:00+00:00",
        internal_domains=rules,
    ) is None


def test_classify_person_at_returns_most_recent_per_type(db_conn):
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="bob@old.example",
        date_sent="2022-01-01T00:00:00+00:00",
    )
    _ins_email(
        db_conn, mbox_index=1,
        from_addr="bob@new.example",
        date_sent="2024-01-01T00:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=())

    rows = db_conn.execute(
        "SELECT person_id, email FROM person_emails ORDER BY email"
    ).fetchall()
    by_email = {r["email"]: r["person_id"] for r in rows}
    from foia.er_driver import merge_persons
    merge_persons(
        db_conn,
        loser_id=by_email["bob@new.example"],
        winner_id=by_email["bob@old.example"],
    )
    pid = by_email["bob@old.example"]

    # As of 2023, the corpus only knew the old domain.
    cls_2023 = classify_person_at(db_conn, pid, "2023-06-01T00:00:00+00:00")
    assert cls_2023["email_domain"]["value"] == "old.example"

    # As of 2025, the most recent observation is the new domain.
    cls_2025 = classify_person_at(db_conn, pid, "2025-06-01T00:00:00+00:00")
    assert cls_2025["email_domain"]["value"] == "new.example"


def test_affiliation_history_orders_oldest_first(db_conn):
    _ins_email(
        db_conn, mbox_index=0, from_addr="x@a.org",
        date_sent="2024-01-01T00:00:00+00:00",
    )
    _ins_email(
        db_conn, mbox_index=1, from_addr="x@a.org",
        date_sent="2022-01-01T00:00:00+00:00",
    )
    _ins_email(
        db_conn, mbox_index=2, from_addr="x@a.org",
        date_sent="2023-01-01T00:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]

    hist = affiliation_history(
        db_conn, pid, affiliation_type="email_domain"
    )
    timestamps = [h["observed_at"] for h in hist]
    assert timestamps == sorted(timestamps), "history must be oldest-first"


def test_unique_constraint_dedupes_per_email_observation(db_conn):
    """Re-running resolution shouldn't multiply affiliations for the
    same (person, type, value, source_email)."""
    _ins_email(
        db_conn, mbox_index=0, from_addr="a@b.org",
        date_sent="2024-01-01T00:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=())
    run_resolution(db_conn, internal_domains=())
    run_resolution(db_conn, internal_domains=())
    count = db_conn.execute(
        "SELECT COUNT(*) FROM person_affiliations"
    ).fetchone()[0]
    # 1 email × 1 evidence type (email_domain) = 1 row total.
    assert count == 1


def test_classifier_separates_evidence_from_rules(db_conn):
    """Affiliations record raw corpus evidence (the email_domain). The
    is_internal interpretation is computed at query time from the
    current rules. Changing the rules does not retroactively rewrite
    the timeline; it just changes the answer to is_internal_at()."""
    _ins_email(
        db_conn, mbox_index=0, from_addr="x@new-school.example",
        date_sent="2022-03-01T00:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]

    # Old rules: domain not internal.
    assert is_internal_at(
        db_conn, pid, "2022-03-15T00:00:00+00:00",
        internal_domains=(),
    ) is False
    # New rules: same evidence, different answer.
    assert is_internal_at(
        db_conn, pid, "2022-03-15T00:00:00+00:00",
        internal_domains=("new-school.example",),
    ) is True

    # The timeline itself stays honest — only one email_domain row,
    # never any is_internal row.
    types = {
        r["affiliation_type"]
        for r in db_conn.execute(
            "SELECT affiliation_type FROM person_affiliations WHERE person_id = ?",
            (pid,),
        )
    }
    assert types == {"email_domain"}


def test_classify_for_unknown_date_returns_empty(db_conn):
    _ins_email(
        db_conn, mbox_index=0, from_addr="a@b.org",
        date_sent="2024-06-01T00:00:00+00:00",
    )
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    result = classify_person_at(db_conn, pid, "2020-01-01T00:00:00+00:00")
    assert result == {}


def test_signature_email_keeps_separate_person_with_own_timeline(db_conn):
    """Signatures don't merge identities; they create another person
    with their own affiliation timeline."""
    db_conn.execute(
        """
        INSERT INTO emails (
            mbox_source, mbox_index, from_addr, body_text,
            date_sent, ingested_at
        ) VALUES (
            't.mbox', 0, 'jane@district.example.org',
            'Hi,\n\nThanks.\n\nBest,\nJane Doe\nbackup: jane.alt@personal.example\n',
            '2023-05-01T00:00:00+00:00',
            ?
        )
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    db_conn.commit()
    run_resolution(db_conn, internal_domains=("district.example.org",))

    rows = db_conn.execute(
        "SELECT email, person_id FROM person_emails"
    ).fetchall()
    by_email = {r["email"]: r["person_id"] for r in rows}
    primary = by_email["jane@district.example.org"]
    sig = by_email["jane.alt@personal.example"]
    assert primary != sig
    # Both persons have their own affiliations.
    rules = ("district.example.org",)
    assert is_internal_at(
        db_conn, primary, "2023-06-01T00:00:00+00:00",
        internal_domains=rules,
    ) is True
    assert is_internal_at(
        db_conn, sig, "2023-06-01T00:00:00+00:00",
        internal_domains=rules,
    ) is False
