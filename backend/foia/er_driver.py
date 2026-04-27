"""Database-backed entity resolution.

Reads each email's From/To/Cc/Bcc headers and body-text signature area,
upserts ``persons`` / ``person_emails`` / ``person_occurrences`` rows,
and supports manual merge/rename operations from the CLI.

Idempotent: re-running after new ingestion only adds the new identities
and occurrences; existing person records are preserved.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .entity_resolution import (
    ParsedAddress,
    canonicalize_email,
    canonicalize_name,
    extract_signature_emails,
    is_internal_email,
    parse_address,
    pick_display_name,
)

log = logging.getLogger(__name__)


SOURCE_FROM = "email_from"
SOURCE_TO = "email_to"
SOURCE_CC = "email_cc"
SOURCE_BCC = "email_bcc"
SOURCE_SIG = "signature"


@dataclass
class ResolveStats:
    emails_scanned: int = 0
    persons_created: int = 0
    persons_updated: int = 0
    occurrences_inserted: int = 0
    signatures_with_extra_emails: int = 0

    def as_dict(self) -> dict:
        return {
            "emails_scanned": self.emails_scanned,
            "persons_created": self.persons_created,
            "persons_updated": self.persons_updated,
            "occurrences_inserted": self.occurrences_inserted,
            "signatures_with_extra_emails": self.signatures_with_extra_emails,
        }


# ---------------------------------------------------------------------------
# Core upsert
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_person_id_by_email(
    conn: sqlite3.Connection, email: str
) -> int | None:
    row = conn.execute(
        "SELECT person_id FROM person_emails WHERE email = ?",
        (email,),
    ).fetchone()
    return int(row["person_id"]) if row else None


def _insert_person(
    conn: sqlite3.Connection,
    email: str,
    display_name: str | None,
    internal_domains: tuple[str, ...],
    now: str,
) -> int:
    names: list[str] = [display_name] if display_name else []
    is_internal = 1 if is_internal_email(email, internal_domains) else 0
    cur = conn.execute(
        """
        INSERT INTO persons (
            display_name, names_json, is_internal,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            display_name or email,
            json.dumps(names, ensure_ascii=False),
            is_internal,
            now,
            now,
        ),
    )
    person_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO person_emails (person_id, email, is_primary, first_seen)
        VALUES (?, ?, 1, ?)
        """,
        (person_id, email, now),
    )
    return person_id


def _add_name_variant(
    conn: sqlite3.Connection,
    person_id: int,
    name: str,
    now: str,
) -> bool:
    row = conn.execute(
        "SELECT display_name, names_json FROM persons WHERE id = ?",
        (person_id,),
    ).fetchone()
    if not row:
        return False
    names = json.loads(row["names_json"] or "[]")
    if name in names:
        return False
    names.append(name)

    # Recompute display_name from the aggregated variants. We don't have
    # per-name counts in the DB yet, so tie-break on length / alpha only.
    counts = {n: 1 for n in names}
    best = pick_display_name(counts, fallback_email=row["display_name"])

    conn.execute(
        "UPDATE persons SET names_json = ?, display_name = ?, updated_at = ? "
        "WHERE id = ?",
        (json.dumps(names, ensure_ascii=False), best, now, person_id),
    )
    return True


def _record_occurrence(
    conn: sqlite3.Connection,
    person_id: int,
    source_type: str,
    source_id: int,
    raw_text: str,
    now: str,
) -> bool:
    """Insert an occurrence, ignoring duplicates. Returns True if inserted."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO person_occurrences (
            person_id, source_type, source_id, raw_text, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (person_id, source_type, source_id, raw_text, now),
    )
    return cur.rowcount > 0


def _record_affiliation(
    conn: sqlite3.Connection,
    person_id: int,
    affiliation_type: str,
    affiliation_value: str,
    observed_at: str,
    source_email_id: int | None,
    now: str,
) -> None:
    """Append a timestamped observation. UNIQUE collapses duplicates per email."""
    conn.execute(
        """
        INSERT OR IGNORE INTO person_affiliations (
            person_id, affiliation_type, affiliation_value,
            observed_at, source_email_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            person_id, affiliation_type, affiliation_value,
            observed_at, source_email_id, now,
        ),
    )


def _record_email_affiliations(
    conn: sqlite3.Connection,
    person_id: int,
    email: str,
    internal_domains: tuple[str, ...],
    observed_at: str,
    source_email_id: int | None,
    now: str,
) -> None:
    """Record raw corpus evidence: the domain this person used at this time.

    We deliberately do *not* persist an ``is_internal`` interpretation —
    that's a function of the district's current rules, applied at query
    time over the recorded timeline. Otherwise re-running resolution
    after a rule change would write contradictory rows against the
    same source email.
    """
    if not email or "@" not in email:
        return
    domain = email.rsplit("@", 1)[1].lower()
    _record_affiliation(
        conn, person_id, "email_domain", domain,
        observed_at, source_email_id, now,
    )
    # `internal_domains` is consumed at query time (see is_internal_at),
    # not at storage time.
    _ = internal_domains


def _upsert_identity(
    conn: sqlite3.Connection,
    parsed: ParsedAddress,
    source_type: str,
    source_id: int,
    internal_domains: tuple[str, ...],
    stats: ResolveStats,
    now: str,
    *,
    observed_at: str | None = None,
) -> int | None:
    if parsed.is_empty:
        return None

    person_id = _find_person_id_by_email(conn, parsed.email)
    if person_id is None:
        person_id = _insert_person(
            conn, parsed.email, parsed.display_name, internal_domains, now
        )
        stats.persons_created += 1
    elif parsed.display_name:
        if _add_name_variant(conn, person_id, parsed.display_name, now):
            stats.persons_updated += 1

    if _record_occurrence(
        conn, person_id, source_type, source_id, parsed.raw, now
    ):
        stats.occurrences_inserted += 1

    # Temporal classifier: record what the corpus observed about this
    # person *at the time of this email*. Falls back to the ingest time
    # if the email has no Date header.
    when = observed_at or now
    src_email = source_id if source_type.startswith("email_") else None
    _record_email_affiliations(
        conn, person_id, parsed.email,
        internal_domains, when, src_email, now,
    )
    return person_id


def _iter_addresses(raw: str | None, field: str) -> Iterable[str]:
    """Decode a JSON array (for to/cc/bcc) or a single string (for from)."""
    if raw is None:
        return
    if field == "from":
        yield raw
        return
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return
    if isinstance(items, list):
        for item in items:
            if item:
                yield str(item)


def run_resolution(
    conn: sqlite3.Connection,
    internal_domains: tuple[str, ...] = (),
    *,
    only_email_id: int | None = None,
) -> ResolveStats:
    stats = ResolveStats()
    now = _now()

    sql = """
        SELECT id, from_addr, to_addrs, cc_addrs, bcc_addrs, body_text,
               date_sent, ingested_at
        FROM emails
    """
    params: list = []
    if only_email_id is not None:
        sql += " WHERE id = ?"
        params.append(only_email_id)
    sql += " ORDER BY id"

    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        email_id = int(row["id"])
        stats.emails_scanned += 1
        # Use the email's own send time as the observation timestamp; the
        # ingest timestamp is a poor substitute but keeps things working
        # for malformed mail with no Date header.
        observed_at = row["date_sent"] or row["ingested_at"]

        from_parsed = parse_address(row["from_addr"] or "")
        _upsert_identity(
            conn, from_parsed, SOURCE_FROM, email_id,
            internal_domains, stats, now,
            observed_at=observed_at,
        )

        for field, column, source in (
            ("to", "to_addrs", SOURCE_TO),
            ("cc", "cc_addrs", SOURCE_CC),
            ("bcc", "bcc_addrs", SOURCE_BCC),
        ):
            for raw in _iter_addresses(row[column], field):
                parsed = parse_address(raw)
                _upsert_identity(
                    conn, parsed, source, email_id,
                    internal_domains, stats, now,
                    observed_at=observed_at,
                )

        # Signature scan — only the emails found, no name parsing.
        from_email = from_parsed.email or ""
        sig_emails = [
            e for e in extract_signature_emails(row["body_text"])
            if e != from_email
        ]
        if sig_emails:
            stats.signatures_with_extra_emails += 1
        for sig_email in sig_emails:
            parsed = ParsedAddress(
                display_name=None, email=sig_email, raw=sig_email
            )
            _upsert_identity(
                conn, parsed, SOURCE_SIG, email_id,
                internal_domains, stats, now,
                observed_at=observed_at,
            )

        conn.commit()

    return stats


# ---------------------------------------------------------------------------
# Temporal classifier query API
# ---------------------------------------------------------------------------


def classify_person_at(
    conn: sqlite3.Connection,
    person_id: int,
    when: str,
) -> dict[str, dict]:
    """Return the corpus's view of this person on or before ``when``.

    The result is keyed by ``affiliation_type``. For each type we return
    the *most recent* observation with ``observed_at <= when``. This is
    the legally defensible answer to "what did we know about this
    person on this date?" — later evidence is ignored.

    Returns an empty dict if no observations exist by that date (the
    person was unknown to the corpus then).
    """
    rows = conn.execute(
        """
        SELECT a1.affiliation_type, a1.affiliation_value, a1.observed_at,
               a1.source_email_id
        FROM person_affiliations a1
        WHERE a1.person_id = ?
          AND a1.observed_at <= ?
          AND a1.observed_at = (
              SELECT MAX(a2.observed_at)
              FROM person_affiliations a2
              WHERE a2.person_id = a1.person_id
                AND a2.affiliation_type = a1.affiliation_type
                AND a2.observed_at <= ?
          )
        """,
        (person_id, when, when),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        # Multiple values may share the most-recent timestamp — keep
        # all of them under the same type.
        prev = out.get(r["affiliation_type"])
        if prev is None:
            out[r["affiliation_type"]] = {
                "value": r["affiliation_value"],
                "values": [r["affiliation_value"]],
                "observed_at": r["observed_at"],
                "source_email_id": r["source_email_id"],
            }
        else:
            prev["values"].append(r["affiliation_value"])
    return out


def is_internal_at(
    conn: sqlite3.Connection,
    person_id: int,
    when: str,
    *,
    internal_domains: tuple[str, ...] | None = None,
) -> bool | None:
    """Did this person count as internal on the given date?

    Computed from the corpus evidence (the ``email_domain`` they were
    using at or before ``when``) against the current district rules
    (``internal_domains``). If ``internal_domains`` isn't supplied, we
    fall back to the cached ``persons.is_internal`` flag — useful for
    UI display where a separate config lookup would be wasteful.

    Returns None when the corpus has no observation by that date.
    """
    row = conn.execute(
        """
        SELECT affiliation_value
        FROM person_affiliations
        WHERE person_id = ?
          AND affiliation_type = 'email_domain'
          AND observed_at <= ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (person_id, when),
    ).fetchone()
    if row is None:
        return None
    domain = row["affiliation_value"]
    if internal_domains is None:
        # No rules supplied — defer to cached flag on persons.
        cached = conn.execute(
            "SELECT is_internal FROM persons WHERE id = ?", (person_id,)
        ).fetchone()
        return bool(cached["is_internal"]) if cached else None
    return is_internal_email(f"x@{domain}", internal_domains)


def affiliation_history(
    conn: sqlite3.Connection,
    person_id: int,
    *,
    affiliation_type: str | None = None,
) -> list[dict]:
    """Full timeline of observations for a person, oldest first."""
    sql = (
        "SELECT id, affiliation_type, affiliation_value, observed_at, "
        "       source_email_id, created_at "
        "FROM person_affiliations WHERE person_id = ?"
    )
    params: list = [person_id]
    if affiliation_type:
        sql += " AND affiliation_type = ?"
        params.append(affiliation_type)
    sql += " ORDER BY observed_at, id"
    return [
        {k: r[k] for k in r.keys()}
        for r in conn.execute(sql, params).fetchall()
    ]


# ---------------------------------------------------------------------------
# Manual operations (CLI)
# ---------------------------------------------------------------------------


def list_persons(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.id, p.display_name, p.is_internal,
               (SELECT email FROM person_emails
                 WHERE person_id = p.id AND is_primary = 1
                 LIMIT 1) AS primary_email,
               (SELECT COUNT(*) FROM person_occurrences WHERE person_id = p.id)
                 AS occurrences
        FROM persons p
        ORDER BY occurrences DESC, p.display_name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def show_person(conn: sqlite3.Connection, person_id: int) -> dict | None:
    p = conn.execute(
        "SELECT * FROM persons WHERE id = ?", (person_id,)
    ).fetchone()
    if not p:
        return None
    emails = [
        dict(r)
        for r in conn.execute(
            "SELECT email, is_primary, first_seen FROM person_emails "
            "WHERE person_id = ? ORDER BY is_primary DESC, email",
            (person_id,),
        )
    ]
    occ_by_type = dict(
        conn.execute(
            "SELECT source_type, COUNT(*) FROM person_occurrences "
            "WHERE person_id = ? GROUP BY source_type",
            (person_id,),
        ).fetchall()
    )
    return {
        "id": p["id"],
        "display_name": p["display_name"],
        "names": json.loads(p["names_json"] or "[]"),
        "is_internal": bool(p["is_internal"]),
        "notes": p["notes"],
        "emails": emails,
        "occurrences_by_type": {
            str(k): int(v) for k, v in occ_by_type.items()
        },
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
    }


class MergeError(Exception):
    """Raised when a manual merge cannot proceed."""


def merge_persons(
    conn: sqlite3.Connection, loser_id: int, winner_id: int
) -> dict:
    if loser_id == winner_id:
        raise MergeError("cannot merge a person into itself")
    loser = conn.execute(
        "SELECT id, display_name, names_json FROM persons WHERE id = ?",
        (loser_id,),
    ).fetchone()
    winner = conn.execute(
        "SELECT id, display_name, names_json FROM persons WHERE id = ?",
        (winner_id,),
    ).fetchone()
    if loser is None:
        raise MergeError(f"person id {loser_id} not found")
    if winner is None:
        raise MergeError(f"person id {winner_id} not found")

    now = _now()
    # Reassign emails. The target may already have an email — we keep
    # ``is_primary`` bits untouched on the winner side.
    conn.execute(
        "UPDATE person_emails SET person_id = ?, is_primary = 0 "
        "WHERE person_id = ?",
        (winner_id, loser_id),
    )
    # Move occurrences; duplicates collapse via INSERT OR IGNORE pattern.
    losing_occ = conn.execute(
        "SELECT source_type, source_id, raw_text, created_at "
        "FROM person_occurrences WHERE person_id = ?",
        (loser_id,),
    ).fetchall()
    for r in losing_occ:
        conn.execute(
            """
            INSERT OR IGNORE INTO person_occurrences (
                person_id, source_type, source_id, raw_text, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (winner_id, r["source_type"], r["source_id"], r["raw_text"], r["created_at"]),
        )
    conn.execute(
        "DELETE FROM person_occurrences WHERE person_id = ?", (loser_id,)
    )

    # Transfer affiliations. The UNIQUE on
    # (person_id, affiliation_type, affiliation_value, source_email_id)
    # collapses any duplicates that already exist on the winner side.
    losing_aff = conn.execute(
        "SELECT affiliation_type, affiliation_value, observed_at, "
        "       source_email_id, created_at "
        "FROM person_affiliations WHERE person_id = ?",
        (loser_id,),
    ).fetchall()
    for r in losing_aff:
        conn.execute(
            """
            INSERT OR IGNORE INTO person_affiliations (
                person_id, affiliation_type, affiliation_value,
                observed_at, source_email_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                winner_id, r["affiliation_type"], r["affiliation_value"],
                r["observed_at"], r["source_email_id"], r["created_at"],
            ),
        )
    conn.execute(
        "DELETE FROM person_affiliations WHERE person_id = ?", (loser_id,)
    )

    # Merge name variants.
    loser_names = set(json.loads(loser["names_json"] or "[]"))
    winner_names = set(json.loads(winner["names_json"] or "[]"))
    merged = sorted(loser_names | winner_names)
    counts = {n: 1 for n in merged}
    new_display = pick_display_name(counts, fallback_email=winner["display_name"])
    conn.execute(
        "UPDATE persons SET names_json = ?, display_name = ?, updated_at = ? "
        "WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False), new_display, now, winner_id),
    )

    # Drop the loser row.
    conn.execute("DELETE FROM persons WHERE id = ?", (loser_id,))
    conn.commit()
    return {"winner_id": winner_id, "merged_name": new_display}


def rename_person(
    conn: sqlite3.Connection, person_id: int, new_display_name: str
) -> dict:
    cleaned = canonicalize_name(new_display_name) or new_display_name.strip()
    if not cleaned:
        raise MergeError("display name must not be empty")
    now = _now()
    row = conn.execute(
        "SELECT names_json FROM persons WHERE id = ?", (person_id,)
    ).fetchone()
    if not row:
        raise MergeError(f"person id {person_id} not found")
    names = json.loads(row["names_json"] or "[]")
    if cleaned not in names:
        names.append(cleaned)
    conn.execute(
        "UPDATE persons SET display_name = ?, names_json = ?, updated_at = ? "
        "WHERE id = ?",
        (cleaned, json.dumps(names, ensure_ascii=False), now, person_id),
    )
    conn.commit()
    return {"id": person_id, "display_name": cleaned}


def annotate_person(
    conn: sqlite3.Connection, person_id: int, note: str
) -> None:
    now = _now()
    conn.execute(
        "UPDATE persons SET notes = ?, updated_at = ? WHERE id = ?",
        (note, now, person_id),
    )
    conn.commit()


__all__ = [
    "MergeError",
    "ResolveStats",
    "SOURCE_BCC", "SOURCE_CC", "SOURCE_FROM", "SOURCE_SIG", "SOURCE_TO",
    "annotate_person",
    "list_persons",
    "merge_persons",
    "rename_person",
    "run_resolution",
    "show_person",
]
