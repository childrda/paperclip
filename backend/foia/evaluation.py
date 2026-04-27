"""Evaluation harness for the PII detector.

Ships with a synthetic K–12 dataset generator. Each generated document
comes with character-level gold spans so we can compute precision /
recall / F1 per entity type.

Matching rule: a predicted span is a true positive if it *overlaps* a
gold span of the same entity type. (Exact-boundary matching is too
strict for phone/date patterns that legitimately include or exclude
trailing punctuation.)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .detection import Detection, PiiDetector


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    entity_type: str


@dataclass(frozen=True)
class LabeledDoc:
    text: str
    spans: tuple[Span, ...]


@dataclass
class EntityMetrics:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class EvaluationReport:
    per_entity: dict[str, EntityMetrics] = field(default_factory=dict)
    docs_evaluated: int = 0

    def micro(self) -> EntityMetrics:
        m = EntityMetrics()
        for em in self.per_entity.values():
            m.true_positives += em.true_positives
            m.false_positives += em.false_positives
            m.false_negatives += em.false_negatives
        return m

    def as_dict(self) -> dict:
        return {
            "docs_evaluated": self.docs_evaluated,
            "micro": self.micro().as_dict(),
            "per_entity": {k: v.as_dict() for k, v in sorted(self.per_entity.items())},
        }


# ---------------------------------------------------------------------------
# Synthetic dataset
# ---------------------------------------------------------------------------

_FIRST_NAMES = [
    "Aisha", "Brandon", "Camila", "Devon", "Elena", "Finn", "Gabriela",
    "Hiroshi", "Imani", "Jamal", "Karen", "Lucas", "Mariam", "Noah",
    "Priya", "Quincy",
]
_LAST_NAMES = [
    "Alvarez", "Brooks", "Chen", "Douglas", "Espinoza", "Fitzgerald",
    "Green", "Huang", "Iyer", "Johnson", "Khan", "Li",
]
_TEMPLATES = [
    ("Parent {fname} {lname} ({email}) called about student ID {sid}. "
     "Home phone {phone}. DOB {dob}."),
    ("Please update the record for {fname} {lname}, student ID {sid}, "
     "SSN {ssn}. Contact: {email} / {phone}."),
    ("IEP meeting scheduled {date}. Attendees: {fname} {lname} "
     "(guardian, {email}), homeroom teacher. Emergency phone: {phone}."),
    ("Field trip permission slip for {fname} {lname}. "
     "Emergency contact phone: {phone}. Lunch account LA{lunch}."),
    ("Disciplinary incident report filed {date} regarding student ID {sid}. "
     "Parent {fname} {lname} notified at {phone}. Follow-up via {email}."),
]


def _fake_ssn(rng: random.Random) -> str:
    # Avoid Presidio's deny list (all-same digits, 000 / 666 / 9xx areas,
    # group 00, serial 0000, and the canonical doc examples 078-05-1120
    # and 123-45-6789 / 98-765-432).
    while True:
        area = rng.randint(100, 899)
        if area in {666}:
            continue
        group = rng.randint(10, 99)
        serial = rng.randint(1000, 9999)
        digits = f"{area:03d}{group:02d}{serial:04d}"
        if any(
            digits.startswith(bad)
            for bad in ("000", "666", "123456789", "98765432", "078051120")
        ):
            continue
        if len(set(digits)) == 1:
            continue
        return f"{area:03d}-{group:02d}-{serial:04d}"


_VALID_AREA_CODES: tuple[int, ...] = (
    201, 202, 203, 205, 206, 207, 208, 209, 212, 213, 214, 215, 216, 217,
    301, 302, 303, 304, 305, 310, 312, 313, 314, 315, 316, 317, 318,
    401, 402, 404, 405, 406, 407, 408, 409, 410, 412, 413, 414, 415,
    501, 502, 503, 504, 505, 507, 508, 509, 510, 512, 513, 515,
    571, 703, 804, 434, 540, 757,   # Virginia codes, realistic for K-12 VA use
    602, 603, 605, 606, 607, 608, 609, 610, 612, 614, 615, 616,
    701, 702, 704, 706, 707, 708, 712, 713, 714, 715, 716, 717, 718,
    801, 802, 803, 805, 808, 812, 813, 814, 815, 816,
    901, 903, 904, 905, 906, 907, 908, 910, 912, 913, 914, 915, 916,
)


def _fake_phone(rng: random.Random) -> str:
    return (
        f"({rng.choice(_VALID_AREA_CODES)}) "
        f"{rng.randint(200, 999)}-{rng.randint(1000, 9999)}"
    )


def _fake_dob(rng: random.Random) -> str:
    return f"{rng.randint(1,12):02d}/{rng.randint(1,28):02d}/{rng.randint(2005,2017)}"


def _fake_date(rng: random.Random) -> str:
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return f"{rng.choice(months)} {rng.randint(1, 28)}, 2024"


def _fake_email(fname: str, lname: str, rng: random.Random) -> str:
    # Real TLDs only — Presidio's EmailRecognizer uses tldextract to validate.
    domain = rng.choice(
        ["example.com", "parentmail.com", "district.org", "school.k12.va.us"]
    )
    return f"{fname.lower()}.{lname.lower()}@{domain}"


def _fake_sid(rng: random.Random) -> str:
    return str(rng.randint(10_000_000, 99_999_999))


def _fake_lunch(rng: random.Random) -> str:
    return str(rng.randint(100_000, 999_999))


def generate_document(
    rng: random.Random, template_index: int | None = None
) -> LabeledDoc:
    tpl = _TEMPLATES[template_index] if template_index is not None else rng.choice(_TEMPLATES)

    fields = {
        "fname": rng.choice(_FIRST_NAMES),
        "lname": rng.choice(_LAST_NAMES),
        "sid": _fake_sid(rng),
        "ssn": _fake_ssn(rng),
        "phone": _fake_phone(rng),
        "dob": _fake_dob(rng),
        "date": _fake_date(rng),
        "lunch": _fake_lunch(rng),
    }
    fields["email"] = _fake_email(fields["fname"], fields["lname"], rng)

    # Assemble while recording the span for each field's value. We render
    # the template manually so we can capture offsets.
    out_parts: list[str] = []
    spans: list[Span] = []
    cursor = 0
    i = 0
    while i < len(tpl):
        if tpl[i] == "{":
            end = tpl.index("}", i)
            key = tpl[i + 1:end]
            value = str(fields[key])
            out_parts.append(value)
            start_off = cursor
            cursor += len(value)
            end_off = cursor
            spans.append(Span(start_off, end_off, _entity_for(key)))
            i = end + 1
        else:
            # Consume a literal chunk up to the next "{" (or EOS).
            nxt = tpl.find("{", i)
            if nxt < 0:
                chunk = tpl[i:]
                i = len(tpl)
            else:
                chunk = tpl[i:nxt]
                i = nxt
            out_parts.append(chunk)
            cursor += len(chunk)

    return LabeledDoc(text="".join(out_parts), spans=tuple(spans))


def _entity_for(field_name: str) -> str:
    return {
        "ssn": "US_SSN",
        "phone": "PHONE_NUMBER",
        "email": "EMAIL_ADDRESS",
        "dob": "DATE_TIME",
        "date": "DATE_TIME",
        "sid": "STUDENT_ID",
        "lunch": "LUNCH_ACCT",
        "fname": "PERSON",
        "lname": "PERSON",
    }[field_name]


def generate_dataset(n: int, seed: int = 0) -> list[LabeledDoc]:
    rng = random.Random(seed)
    return [generate_document(rng) for _ in range(n)]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _score_document(
    predictions: Sequence[Detection],
    gold: Sequence[Span],
    entity_types: Iterable[str],
) -> dict[str, EntityMetrics]:
    """Per-entity metrics for one document."""
    out: dict[str, EntityMetrics] = {e: EntityMetrics() for e in entity_types}

    pred_by_type: dict[str, list[Detection]] = {}
    for p in predictions:
        pred_by_type.setdefault(p.entity_type, []).append(p)

    gold_by_type: dict[str, list[Span]] = {}
    for g in gold:
        gold_by_type.setdefault(g.entity_type, []).append(g)

    for entity in out:
        preds = pred_by_type.get(entity, [])
        golds = gold_by_type.get(entity, [])
        matched_gold: set[int] = set()
        matched_pred: set[int] = set()

        for pi, p in enumerate(preds):
            for gi, g in enumerate(golds):
                if gi in matched_gold:
                    continue
                if _overlaps(p.start, p.end, g.start, g.end):
                    matched_gold.add(gi)
                    matched_pred.add(pi)
                    break

        out[entity].true_positives = len(matched_pred)
        out[entity].false_positives = len(preds) - len(matched_pred)
        out[entity].false_negatives = len(golds) - len(matched_gold)

    return out


def evaluate(
    detector: PiiDetector,
    dataset: Sequence[LabeledDoc],
    *,
    entity_types: Iterable[str] | None = None,
) -> EvaluationReport:
    report = EvaluationReport()
    if entity_types is None:
        entity_types = sorted({s.entity_type for d in dataset for s in d.spans})
    entity_types = list(entity_types)

    for doc in dataset:
        predictions = detector.detect(doc.text)
        per_doc = _score_document(predictions, doc.spans, entity_types)
        for ent, m in per_doc.items():
            agg = report.per_entity.setdefault(ent, EntityMetrics())
            agg.true_positives += m.true_positives
            agg.false_positives += m.false_positives
            agg.false_negatives += m.false_negatives
        report.docs_evaluated += 1

    return report


__all__ = [
    "EntityMetrics",
    "EvaluationReport",
    "LabeledDoc",
    "Span",
    "evaluate",
    "generate_dataset",
    "generate_document",
]
