"""Detection engine tests."""

from __future__ import annotations

from foia.detection import PiiDetector
from foia.district import (
    CustomRecognizerSpec,
    PatternSpec,
    PiiDetectionConfig,
)


def _detector(**kwargs) -> PiiDetector:
    cfg = PiiDetectionConfig(
        builtins=kwargs.pop("builtins", ("US_SSN", "EMAIL_ADDRESS", "PHONE_NUMBER", "DATE_TIME")),
        min_score=kwargs.pop("min_score", 0.3),
        enable_ner=kwargs.pop("enable_ner", False),
        custom_recognizers=tuple(kwargs.pop("custom", ())),
    )
    return PiiDetector(cfg)


def test_detects_ssn_email_phone():
    det = _detector()
    text = (
        "Contact parent jane@example.com about SSN 572-68-1439. "
        "Home phone (571) 555-0123."
    )
    hits = det.detect(text)
    types = {h.entity_type for h in hits}
    assert "EMAIL_ADDRESS" in types
    assert "US_SSN" in types
    assert "PHONE_NUMBER" in types

    by = {h.entity_type: h for h in hits}
    assert by["EMAIL_ADDRESS"].matched_text == "jane@example.com"
    assert by["US_SSN"].matched_text == "572-68-1439"
    assert "(571) 555-0123" in by["PHONE_NUMBER"].matched_text


def test_custom_recognizer_matches_student_id():
    custom = [
        CustomRecognizerSpec(
            name="Student ID",
            entity_type="STUDENT_ID",
            patterns=(PatternSpec(regex=r"\b\d{8}\b", score=0.7),),
            context=("student",),
        )
    ]
    det = _detector(builtins=(), custom=custom)
    hits = det.detect("The student ID is 82746153 in the file.")
    assert len(hits) == 1
    assert hits[0].entity_type == "STUDENT_ID"
    assert hits[0].matched_text == "82746153"
    assert hits[0].recognizer == "Student ID"


def test_min_score_filters_low_confidence():
    custom = [
        CustomRecognizerSpec(
            name="Low confidence",
            entity_type="LOW",
            patterns=(PatternSpec(regex=r"LOW\d", score=0.2),),
        )
    ]
    det_strict = _detector(builtins=(), custom=custom, min_score=0.5)
    det_loose = _detector(builtins=(), custom=custom, min_score=0.1)
    text = "testing LOW1 and LOW2"
    assert det_strict.detect(text) == []
    assert len(det_loose.detect(text)) == 2


def test_overlap_resolution_same_entity_keeps_highest_score():
    custom = [
        CustomRecognizerSpec(
            name="A (weak)",
            entity_type="FOO",
            patterns=(PatternSpec(regex=r"\d{3,4}", score=0.4),),
        ),
        CustomRecognizerSpec(
            name="B (strong)",
            entity_type="FOO",
            patterns=(PatternSpec(regex=r"\d{4}", score=0.9),),
        ),
    ]
    det = _detector(builtins=(), custom=custom)
    hits = det.detect("code 1234 here")
    assert len(hits) == 1  # the weak 3-digit hit loses to the strong 4-digit one
    assert hits[0].score == 0.9
    assert hits[0].recognizer == "B (strong)"


def test_different_entities_can_overlap():
    custom = [
        CustomRecognizerSpec(
            name="STUDENT", entity_type="STUDENT_ID",
            patterns=(PatternSpec(regex=r"\b\d{8}\b", score=0.7),),
        ),
        CustomRecognizerSpec(
            name="CODE", entity_type="INTERNAL_CODE",
            patterns=(PatternSpec(regex=r"\b\d{8}\b", score=0.6),),
        ),
    ]
    det = _detector(builtins=(), custom=custom)
    hits = det.detect("8-digit 12345678 code")
    kinds = {h.entity_type for h in hits}
    assert kinds == {"STUDENT_ID", "INTERNAL_CODE"}


def test_empty_and_whitespace_inputs():
    det = _detector()
    assert det.detect("") == []
    assert det.detect(None) == []  # type: ignore[arg-type]
    assert det.detect("   \n\t  ") == []


def test_results_are_sorted_by_position():
    det = _detector(builtins=("EMAIL_ADDRESS", "PHONE_NUMBER"))
    text = "phone (571) 555-0123 then email a@b.com then phone (804) 555-9999"
    hits = det.detect(text)
    starts = [h.start for h in hits]
    assert starts == sorted(starts)


def test_detector_is_reusable_across_calls():
    det = _detector()
    first = det.detect("email a@example.com")
    second = det.detect("ssn 572-68-1439")
    assert any(h.entity_type == "EMAIL_ADDRESS" for h in first)
    assert any(h.entity_type == "US_SSN" for h in second)
