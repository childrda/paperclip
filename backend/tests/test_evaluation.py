"""Evaluation harness + synthetic dataset tests."""

from __future__ import annotations

import random

from foia.detection import PiiDetector
from foia.district import (
    CustomRecognizerSpec,
    PatternSpec,
    PiiDetectionConfig,
)
from foia.evaluation import (
    EntityMetrics,
    _score_document,
    evaluate,
    generate_dataset,
    generate_document,
)
from foia.detection import Detection
from foia.evaluation import Span


def test_generate_document_has_spans_and_matching_substrings():
    rng = random.Random(123)
    for tpl_idx in range(5):
        doc = generate_document(rng, template_index=tpl_idx)
        assert doc.text
        assert doc.spans
        for span in doc.spans:
            # Each gold span must correspond to a real substring.
            assert 0 <= span.start < span.end <= len(doc.text)
            assert doc.text[span.start:span.end]


def test_generate_dataset_is_deterministic_by_seed():
    a = generate_dataset(10, seed=42)
    b = generate_dataset(10, seed=42)
    assert [d.text for d in a] == [d.text for d in b]
    assert [d.spans for d in a] == [d.spans for d in b]


def test_dataset_contains_expected_entity_types():
    ds = generate_dataset(30, seed=0)
    seen: set[str] = set()
    for doc in ds:
        for s in doc.spans:
            seen.add(s.entity_type)
    # Core entities should all appear across 30 documents.
    for expected in (
        "US_SSN", "PHONE_NUMBER", "EMAIL_ADDRESS",
        "DATE_TIME", "STUDENT_ID", "PERSON",
    ):
        assert expected in seen, f"missing {expected} in dataset"


def test_score_document_overlap_matching():
    text = "call (571) 555-0123 now"
    gold = (Span(5, 19, "PHONE_NUMBER"),)
    # Prediction has slightly off boundaries — overlap rule should still match.
    preds = (
        Detection(
            entity_type="PHONE_NUMBER", start=6, end=18,
            score=0.5, matched_text=text[6:18],
        ),
    )
    m = _score_document(preds, gold, ["PHONE_NUMBER"])["PHONE_NUMBER"]
    assert m.true_positives == 1
    assert m.false_positives == 0
    assert m.false_negatives == 0


def test_score_document_false_positive_and_negative():
    text = "x y z"
    gold = (Span(0, 1, "A"),)
    preds = (
        Detection(entity_type="B", start=2, end=3, score=1.0, matched_text="y"),
    )
    metrics = _score_document(preds, gold, ["A", "B"])
    assert metrics["A"].false_negatives == 1
    assert metrics["A"].true_positives == 0
    assert metrics["B"].false_positives == 1


def test_full_evaluation_patterns_100_percent_recall():
    """Pattern-based entities must reach 100% recall on the synthetic set."""
    cfg = PiiDetectionConfig(
        builtins=("US_SSN", "EMAIL_ADDRESS", "PHONE_NUMBER", "DATE_TIME"),
        min_score=0.3,
        custom_recognizers=(
            CustomRecognizerSpec(
                name="Student ID",
                entity_type="STUDENT_ID",
                patterns=(PatternSpec(regex=r"\b\d{8}\b", score=0.7),),
            ),
            CustomRecognizerSpec(
                name="Lunch Acct",
                entity_type="LUNCH_ACCT",
                patterns=(PatternSpec(regex=r"\bLA\d{6}\b", score=0.85),),
            ),
            CustomRecognizerSpec(
                name="Narrative date",
                entity_type="DATE_TIME",
                patterns=(
                    PatternSpec(
                        regex=(
                            r"\b(?:January|February|March|April|May|June|"
                            r"July|August|September|October|November|December)"
                            r"\s+\d{1,2},?\s+\d{4}\b"
                        ),
                        score=0.6,
                    ),
                ),
            ),
        ),
    )
    det = PiiDetector(cfg)
    ds = generate_dataset(100, seed=11)
    report = evaluate(det, ds)

    for entity in ("US_SSN", "EMAIL_ADDRESS", "STUDENT_ID", "LUNCH_ACCT"):
        assert report.per_entity[entity].recall == 1.0, (
            f"{entity} recall dropped: {report.per_entity[entity].as_dict()}"
        )
    # DATE_TIME and PHONE_NUMBER should be ≥ 95% recall
    assert report.per_entity["DATE_TIME"].recall >= 0.95
    assert report.per_entity["PHONE_NUMBER"].recall >= 0.95


def test_metrics_edge_cases():
    # With no predictions and no gold, precision & recall are both 1.0
    # (nothing was wrong, nothing was missed). F1 therefore == 1.0.
    m = EntityMetrics()
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.f1 == 1.0

    # All predictions wrong, no gold missing yet: precision=0, recall=1, F1=0.
    m = EntityMetrics(false_positives=5)
    assert m.precision == 0.0
    assert m.recall == 1.0
    assert m.f1 == 0.0
