"""Integration tests for the entity-resolution driver."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from foia.er_driver import (
    MergeError,
    SOURCE_BCC,
    SOURCE_CC,
    SOURCE_FROM,
    SOURCE_SIG,
    SOURCE_TO,
    annotate_person,
    list_persons,
    merge_persons,
    rename_person,
    run_resolution,
    show_person,
)


def _ins_email(
    conn,
    *,
    mbox_index: int,
    from_addr: str | None,
    to=None,
    cc=None,
    bcc=None,
    body: str = "",
    subject: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO emails (
            mbox_source, mbox_index, subject, from_addr,
            to_addrs, cc_addrs, bcc_addrs, body_text, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "t.mbox", mbox_index, subject, from_addr,
            json.dumps(to or []), json.dumps(cc or []), json.dumps(bcc or []),
            body, datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# run_resolution — core behaviour
# ---------------------------------------------------------------------------


def test_creates_person_per_unique_email(db_conn):
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="Jane Doe <jane@district.org>",
        to=["Bob <bob@parent.example>"],
        cc=["carl@parent.example"],
    )
    stats = run_resolution(db_conn, internal_domains=("district.org",))
    assert stats.emails_scanned == 1
    assert stats.persons_created == 3
    assert stats.occurrences_inserted == 3

    rows = {
        r["email"]: r["person_id"]
        for r in db_conn.execute(
            "SELECT email, person_id FROM person_emails"
        )
    }
    assert set(rows) == {
        "jane@district.org", "bob@parent.example", "carl@parent.example",
    }


def test_same_email_across_emails_is_single_person(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="Jane <jane@district.org>")
    _ins_email(
        db_conn, mbox_index=1,
        from_addr="Other <someone@elsewhere.org>",
        to=["Jane Doe <jane@district.org>"],
    )
    stats = run_resolution(db_conn, internal_domains=("district.org",))
    assert stats.persons_created == 2
    person_id_rows = db_conn.execute(
        "SELECT person_id FROM person_emails WHERE email = ?",
        ("jane@district.org",),
    ).fetchall()
    assert len(person_id_rows) == 1
    pid = person_id_rows[0]["person_id"]
    occ = db_conn.execute(
        "SELECT source_type FROM person_occurrences WHERE person_id = ?",
        (pid,),
    ).fetchall()
    kinds = {r["source_type"] for r in occ}
    assert kinds == {SOURCE_FROM, SOURCE_TO}

    names = json.loads(
        db_conn.execute(
            "SELECT names_json FROM persons WHERE id = ?", (pid,)
        ).fetchone()[0]
    )
    assert "Jane" in names
    assert "Jane Doe" in names


def test_is_internal_flag_set_when_domain_matches(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@district.org")
    _ins_email(db_conn, mbox_index=1, from_addr="a@parent.example")
    run_resolution(db_conn, internal_domains=("district.org",))
    internal = db_conn.execute(
        "SELECT p.is_internal FROM persons p "
        "JOIN person_emails e ON e.person_id = p.id "
        "WHERE e.email = 'a@district.org'"
    ).fetchone()[0]
    external = db_conn.execute(
        "SELECT p.is_internal FROM persons p "
        "JOIN person_emails e ON e.person_id = p.id "
        "WHERE e.email = 'a@parent.example'"
    ).fetchone()[0]
    assert internal == 1
    assert external == 0


def test_signature_email_recorded_as_occurrence(db_conn):
    body = (
        "Hi team,\n\nThanks for your time.\n\n"
        "Best,\nJane Doe\nContact: jane.alt@district.org\n"
    )
    eid = _ins_email(
        db_conn, mbox_index=0,
        from_addr="Jane Doe <jane@district.org>",
        body=body,
    )
    stats = run_resolution(db_conn, internal_domains=("district.org",))
    assert stats.signatures_with_extra_emails == 1

    sig_person = db_conn.execute(
        "SELECT p.id FROM persons p "
        "JOIN person_emails e ON e.person_id = p.id "
        "WHERE e.email = 'jane.alt@district.org'"
    ).fetchone()
    assert sig_person is not None
    occ = db_conn.execute(
        "SELECT source_type FROM person_occurrences WHERE person_id = ?",
        (sig_person["id"],),
    ).fetchone()
    assert occ["source_type"] == SOURCE_SIG


def test_signature_does_not_double_count_from_address(db_conn):
    body = "Best,\nJane\njane@district.org"
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="Jane <jane@district.org>",
        body=body,
    )
    stats = run_resolution(db_conn, internal_domains=("district.org",))
    # Only a single FROM occurrence — the signature email matches the From.
    assert stats.signatures_with_extra_emails == 0
    pid = db_conn.execute(
        "SELECT person_id FROM person_emails WHERE email = ?",
        ("jane@district.org",),
    ).fetchone()[0]
    occs = db_conn.execute(
        "SELECT source_type FROM person_occurrences WHERE person_id = ?",
        (pid,),
    ).fetchall()
    assert len(occs) == 1
    assert occs[0]["source_type"] == SOURCE_FROM


def test_is_idempotent(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com", to=["b@y.com"])
    first = run_resolution(db_conn, internal_domains=())
    second = run_resolution(db_conn, internal_domains=())
    assert first.persons_created == 2
    assert second.persons_created == 0
    assert second.occurrences_inserted == 0
    assert db_conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 2
    assert db_conn.execute("SELECT COUNT(*) FROM person_occurrences").fetchone()[0] == 2


def test_only_email_id_scopes_the_run(db_conn):
    a = _ins_email(db_conn, mbox_index=0, from_addr="a@x.com")
    b = _ins_email(db_conn, mbox_index=1, from_addr="b@y.com")
    run_resolution(db_conn, internal_domains=(), only_email_id=a)
    assert db_conn.execute(
        "SELECT COUNT(*) FROM person_emails"
    ).fetchone()[0] == 1


def test_handles_empty_headers(db_conn):
    _ins_email(
        db_conn, mbox_index=0,
        from_addr=None, to=[], cc=[], bcc=[], body="",
    )
    stats = run_resolution(db_conn, internal_domains=())
    assert stats.persons_created == 0
    assert stats.occurrences_inserted == 0


# ---------------------------------------------------------------------------
# Manual operations
# ---------------------------------------------------------------------------


def test_list_persons_returns_counts(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com", to=["a@x.com"])
    run_resolution(db_conn, internal_domains=())
    ppl = list_persons(db_conn)
    assert len(ppl) == 1
    assert ppl[0]["primary_email"] == "a@x.com"
    assert ppl[0]["occurrences"] == 2  # From + To on same email


def test_show_person_detail(db_conn):
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="Jane <jane@district.org>",
        to=["jane@district.org"],
    )
    run_resolution(db_conn, internal_domains=("district.org",))
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    data = show_person(db_conn, pid)
    assert data is not None
    assert data["display_name"] == "Jane"
    assert data["is_internal"] is True
    assert data["emails"][0]["email"] == "jane@district.org"
    assert data["occurrences_by_type"]["email_from"] == 1
    assert data["occurrences_by_type"]["email_to"] == 1


def test_show_person_missing(db_conn):
    assert show_person(db_conn, 42) is None


def test_merge_persons_combines_emails_and_occurrences(db_conn):
    _ins_email(
        db_conn, mbox_index=0,
        from_addr="Jane Doe <jane@district.org>",
        to=["jane.personal@example.com"],
    )
    run_resolution(db_conn, internal_domains=("district.org",))
    ids = {
        r["email"]: r["person_id"]
        for r in db_conn.execute(
            "SELECT email, person_id FROM person_emails"
        )
    }
    loser = ids["jane.personal@example.com"]
    winner = ids["jane@district.org"]

    result = merge_persons(db_conn, loser, winner)
    assert result["winner_id"] == winner

    # Loser row gone.
    assert db_conn.execute(
        "SELECT id FROM persons WHERE id = ?", (loser,)
    ).fetchone() is None
    # Both emails now point to the winner.
    owners = {
        r["email"]: r["person_id"]
        for r in db_conn.execute(
            "SELECT email, person_id FROM person_emails"
        )
    }
    assert owners["jane@district.org"] == winner
    assert owners["jane.personal@example.com"] == winner


def test_merge_self_raises(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com")
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    with pytest.raises(MergeError):
        merge_persons(db_conn, pid, pid)


def test_merge_unknown_raises(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com")
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    with pytest.raises(MergeError):
        merge_persons(db_conn, 9999, pid)


def test_rename_person_sets_display_name_and_variant(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com")
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    rename_person(db_conn, pid, "Parent A")
    row = db_conn.execute(
        "SELECT display_name, names_json FROM persons WHERE id = ?", (pid,)
    ).fetchone()
    assert row["display_name"] == "Parent A"
    assert "Parent A" in json.loads(row["names_json"])


def test_rename_empty_raises(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com")
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    with pytest.raises(MergeError):
        rename_person(db_conn, pid, "   ")


def test_annotate_person(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com")
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    annotate_person(db_conn, pid, "Principal — primary contact.")
    note = db_conn.execute(
        "SELECT notes FROM persons WHERE id = ?", (pid,)
    ).fetchone()[0]
    assert note == "Principal — primary contact."


def test_cascade_delete_removes_emails_and_occurrences(db_conn):
    _ins_email(db_conn, mbox_index=0, from_addr="a@x.com")
    run_resolution(db_conn, internal_domains=())
    pid = db_conn.execute("SELECT id FROM persons").fetchone()[0]
    db_conn.execute("DELETE FROM persons WHERE id = ?", (pid,))
    db_conn.commit()
    assert db_conn.execute("SELECT COUNT(*) FROM person_emails").fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM person_occurrences"
    ).fetchone()[0] == 0
