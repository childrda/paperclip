"""PII detection built on Microsoft Presidio's pattern recognizers.

Designed to run without spaCy when NER is disabled. Built-in recognizers
(SSN, phone, email, dates, credit card, US driver's licence, US bank
number) are pulled from :mod:`presidio_analyzer.predefined_recognizers`
and invoked directly, so no ``AnalyzerEngine``/``NlpEngine`` is needed.

Custom district recognizers are compiled from YAML into Presidio
:class:`PatternRecognizer` instances on load.

The detector emits a flat list of :class:`Detection` objects with
``start``/``end``/``score``/``entity_type``, which is the shape the rest
of the system (and the DB schema) expects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from .district import CustomRecognizerSpec, PiiDetectionConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    entity_type: str
    start: int
    end: int
    score: float
    matched_text: str
    recognizer: str | None = None


# Mapping of built-in entity type -> recognizer class, lazily imported.
_BUILTIN_RECOGNIZER_MAP: dict[str, str] = {
    "US_SSN": "UsSsnRecognizer",
    "PHONE_NUMBER": "PhoneRecognizer",
    "EMAIL_ADDRESS": "EmailRecognizer",
    "DATE_TIME": "DateRecognizer",
    "CREDIT_CARD": "CreditCardRecognizer",
    "US_DRIVER_LICENSE": "UsLicenseRecognizer",
    "US_BANK_NUMBER": "UsBankRecognizer",
    "US_ITIN": "UsItinRecognizer",
    "US_PASSPORT": "UsPassportRecognizer",
    "IBAN_CODE": "IbanRecognizer",
    "IP_ADDRESS": "IpRecognizer",
    "MEDICAL_LICENSE": "MedicalLicenseRecognizer",
    "URL": "UrlRecognizer",
}


class PiiDetector:
    """Lazy-built detector. Loads Presidio recognizers on first use.

    Thread-safety: not shared across threads; create one per worker.
    """

    def __init__(self, cfg: PiiDetectionConfig):
        self._cfg = cfg
        self._recognizers: list = []
        self._loaded = False

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        if self._loaded:
            return

        try:
            from presidio_analyzer import predefined_recognizers
            from presidio_analyzer import Pattern, PatternRecognizer
        except ImportError as e:
            raise RuntimeError(
                "presidio-analyzer is required for PII detection"
            ) from e

        self._Pattern = Pattern
        self._PatternRecognizer = PatternRecognizer

        for entity in self._cfg.builtins:
            cls_name = _BUILTIN_RECOGNIZER_MAP.get(entity)
            if not cls_name:
                log.warning("unknown built-in recognizer %s; skipping", entity)
                continue
            cls = getattr(predefined_recognizers, cls_name, None)
            if cls is None:
                log.warning(
                    "presidio does not expose %s; skipping %s", cls_name, entity
                )
                continue
            try:
                self._recognizers.append(cls())
            except Exception:
                log.exception("failed to instantiate %s", cls_name)

        for spec in self._cfg.custom_recognizers:
            try:
                self._recognizers.append(self._compile_custom(spec))
            except Exception:
                log.exception(
                    "failed to compile custom recognizer %s", spec.name
                )

        if self._cfg.enable_ner:
            try:
                self._recognizers.extend(self._load_ner_recognizers())
            except Exception:
                log.exception(
                    "NER enabled but failed to load; continuing with pattern recognizers only"
                )

        self._loaded = True

    def _compile_custom(self, spec: CustomRecognizerSpec):
        patterns = [
            self._Pattern(
                name=f"{spec.name} #{i}",
                regex=p.regex,
                score=p.score,
            )
            for i, p in enumerate(spec.patterns)
        ]
        return self._PatternRecognizer(
            supported_entity=spec.entity_type,
            patterns=patterns,
            context=list(spec.context) if spec.context else None,
            name=spec.name,
        )

    def _load_ner_recognizers(self) -> list:
        """Opt-in spaCy-backed PERSON/ORG/LOC detection."""
        from presidio_analyzer.predefined_recognizers import SpacyRecognizer
        return [SpacyRecognizer(supported_language=self._cfg.ner_language)]

    # --------------------------------------------------------------- analyze

    def detect(self, text: str, *, language: str = "en") -> list[Detection]:
        if not text:
            return []
        self._build()

        raw_results: list = []
        for rec in self._recognizers:
            try:
                entities = list(getattr(rec, "supported_entities", []) or [])
                if not entities:
                    entities = None
                hits = rec.analyze(
                    text=text,
                    entities=entities,
                    nlp_artifacts=None,
                )
                if hits:
                    raw_results.extend((rec, h) for h in hits)
            except Exception:
                log.exception(
                    "recognizer %s failed on %d-char input",
                    getattr(rec, "name", rec.__class__.__name__),
                    len(text),
                )

        detections: list[Detection] = []
        for rec, r in raw_results:
            if r.score < self._cfg.min_score:
                continue
            detections.append(
                Detection(
                    entity_type=r.entity_type,
                    start=int(r.start),
                    end=int(r.end),
                    score=float(r.score),
                    matched_text=text[int(r.start):int(r.end)],
                    recognizer=getattr(rec, "name", rec.__class__.__name__),
                )
            )

        return _resolve_overlaps(detections)


def _resolve_overlaps(detections: list[Detection]) -> list[Detection]:
    """Collapse overlapping spans of the same entity_type; keep highest score.

    Different entity types may overlap legitimately (e.g. a PHONE_NUMBER
    pattern that also matches a generic date). Those are preserved, since
    downstream redaction needs all signals.
    """
    by_entity: dict[str, list[Detection]] = {}
    for d in detections:
        by_entity.setdefault(d.entity_type, []).append(d)

    resolved: list[Detection] = []
    for ent, items in by_entity.items():
        items.sort(key=lambda x: (x.start, -x.score))
        kept: list[Detection] = []
        for cur in items:
            if kept and cur.start < kept[-1].end:
                if cur.score > kept[-1].score:
                    kept[-1] = cur
                # else drop
                continue
            kept.append(cur)
        resolved.extend(kept)

    resolved.sort(key=lambda d: (d.start, d.end, d.entity_type))
    return resolved


__all__ = ["Detection", "PiiDetector"]
