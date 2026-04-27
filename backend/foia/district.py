"""District-level YAML configuration loader.

The entire "per-district" configuration surface lives in one YAML file
(path chosen via ``FOIA_CONFIG_FILE``, default ``./config/district.yaml``).
This module parses that file into typed dataclasses so the rest of the
codebase never sees raw dict soup.

Only Phase 3 fields (district name, email domains, pii_detection) are
consumed today. Phase 6+/8/10 keys are tolerated and surfaced via
:class:`DistrictConfig.raw` so later phases can adopt them without
touching this loader.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class DistrictConfigError(ValueError):
    """Raised when the YAML is missing required keys or malformed."""


@dataclass(frozen=True)
class PatternSpec:
    regex: str
    score: float = 0.5


@dataclass(frozen=True)
class CustomRecognizerSpec:
    name: str
    entity_type: str
    patterns: tuple[PatternSpec, ...]
    context: tuple[str, ...] = ()


@dataclass(frozen=True)
class PiiDetectionConfig:
    builtins: tuple[str, ...]
    min_score: float = 0.3
    enable_ner: bool = False
    ner_language: str = "en"
    custom_recognizers: tuple[CustomRecognizerSpec, ...] = ()


@dataclass(frozen=True)
class ExemptionCode:
    code: str
    description: str = ""


@dataclass(frozen=True)
class RedactionConfig:
    default_exemption: str | None = None
    entity_exemptions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BatesConfig:
    prefix: str = "DOC"
    start: int = 1
    width: int = 6

    def label(self, n: int) -> str:
        return f"{self.prefix}-{n:0{self.width}d}"


@dataclass(frozen=True)
class AiConfig:
    """Phase 10 — AI QA backend. Disabled by default."""
    enabled: bool = False
    provider: str = "null"           # null | openai | anthropic | azure | ollama
    model: str | None = None
    base_url: str | None = None
    api_key_env: str = "FOIA_AI_API_KEY"
    max_input_chars: int = 8000
    request_timeout_s: int = 60


@dataclass(frozen=True)
class DistrictConfig:
    name: str
    email_domains: tuple[str, ...]
    pii: PiiDetectionConfig
    exemptions: tuple[ExemptionCode, ...] = ()
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    bates: BatesConfig = field(default_factory=BatesConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    def is_known_exemption(self, code: str) -> bool:
        if not self.exemptions:
            # If the YAML doesn't list any, accept anything — districts using
            # ad-hoc codes shouldn't be blocked from creating redactions.
            return bool(code)
        return any(e.code == code for e in self.exemptions)

    def exemption_for_entity(self, entity_type: str) -> str | None:
        if entity_type in self.redaction.entity_exemptions:
            return self.redaction.entity_exemptions[entity_type]
        return self.redaction.default_exemption


_DEFAULT_BUILTINS: tuple[str, ...] = (
    "US_SSN",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "DATE_TIME",
    "CREDIT_CARD",
    "US_DRIVER_LICENSE",
    "US_BANK_NUMBER",
)


def _require(d: dict, path: str) -> Any:
    cur: Any = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise DistrictConfigError(f"missing required key: {path}")
        cur = cur[key]
    return cur


def _parse_patterns(raw: list | None, rec_name: str) -> tuple[PatternSpec, ...]:
    if not raw:
        raise DistrictConfigError(
            f"custom recognizer '{rec_name}' has no patterns"
        )
    out: list[PatternSpec] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or "regex" not in item:
            raise DistrictConfigError(
                f"custom recognizer '{rec_name}' pattern #{i} missing 'regex'"
            )
        try:
            score = float(item.get("score", 0.5))
        except (TypeError, ValueError):
            raise DistrictConfigError(
                f"custom recognizer '{rec_name}' pattern #{i} has non-numeric score"
            )
        out.append(PatternSpec(regex=str(item["regex"]), score=score))
    return tuple(out)


def _parse_custom_recognizers(raw: list | None) -> tuple[CustomRecognizerSpec, ...]:
    if not raw:
        return ()
    specs: list[CustomRecognizerSpec] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise DistrictConfigError(
                f"pii_detection.custom_recognizers[{i}] must be a mapping"
            )
        name = str(item.get("name") or f"custom_{i}")
        entity = item.get("entity_type")
        if not entity:
            raise DistrictConfigError(
                f"pii_detection.custom_recognizers[{i}] missing 'entity_type'"
            )
        patterns = _parse_patterns(item.get("patterns"), name)
        context_raw = item.get("context") or []
        if not isinstance(context_raw, list):
            raise DistrictConfigError(
                f"custom recognizer '{name}' context must be a list"
            )
        specs.append(
            CustomRecognizerSpec(
                name=name,
                entity_type=str(entity),
                patterns=patterns,
                context=tuple(str(c) for c in context_raw),
            )
        )
    return tuple(specs)


def _parse_pii(raw: dict | None) -> PiiDetectionConfig:
    raw = raw or {}
    builtins_raw = raw.get("builtins")
    builtins: tuple[str, ...]
    if builtins_raw is None:
        builtins = _DEFAULT_BUILTINS
    elif not isinstance(builtins_raw, list):
        raise DistrictConfigError("pii_detection.builtins must be a list")
    else:
        builtins = tuple(str(x) for x in builtins_raw)

    try:
        min_score = float(raw.get("min_score", 0.3))
    except (TypeError, ValueError):
        raise DistrictConfigError("pii_detection.min_score must be numeric")

    return PiiDetectionConfig(
        builtins=builtins,
        min_score=min_score,
        enable_ner=bool(raw.get("enable_ner", False)),
        ner_language=str(raw.get("ner_language", "en")),
        custom_recognizers=_parse_custom_recognizers(raw.get("custom_recognizers")),
    )


def load_district_config(path: str | Path | None = None) -> DistrictConfig:
    """Load and validate the district YAML.

    If ``path`` is None, use ``FOIA_CONFIG_FILE`` env var; if that is
    also unset, fall back to ``./config/district.yaml``. A missing file
    does not raise — it returns a sensible default so phase-3 detection
    can still run with built-in recognizers only.
    """
    import yaml  # deferred so tests can import this module without PyYAML

    target = Path(
        path
        or os.environ.get("FOIA_CONFIG_FILE")
        or "./config/district.yaml"
    )

    if not target.exists():
        log.info(
            "district config %s not found; using defaults with built-in recognizers",
            target,
        )
        return DistrictConfig(
            name="Unconfigured District",
            email_domains=(),
            pii=PiiDetectionConfig(builtins=_DEFAULT_BUILTINS),
            raw={},
        )

    with target.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise DistrictConfigError(f"{target} did not parse as a mapping")

    district_raw = data.get("district") or {}
    name = str(district_raw.get("name") or "Unnamed District")
    email_domains_raw = district_raw.get("email_domains") or []
    if not isinstance(email_domains_raw, list):
        raise DistrictConfigError("district.email_domains must be a list")

    return DistrictConfig(
        name=name,
        email_domains=tuple(str(d).lower() for d in email_domains_raw),
        pii=_parse_pii(data.get("pii_detection")),
        exemptions=_parse_exemptions(data.get("exemption_codes")),
        redaction=_parse_redaction(data.get("redaction")),
        bates=_parse_bates(data.get("bates")),
        ai=_parse_ai(data.get("ai")),
        raw=data,
    )


_VALID_AI_PROVIDERS: frozenset[str] = frozenset({
    "null", "openai", "anthropic", "azure", "ollama",
})


def _parse_ai(raw: dict | None) -> AiConfig:
    if not raw:
        return AiConfig()
    if not isinstance(raw, dict):
        raise DistrictConfigError("ai must be a mapping")
    provider = str(raw.get("provider") or "null").lower()
    if provider not in _VALID_AI_PROVIDERS:
        raise DistrictConfigError(
            f"ai.provider must be one of {sorted(_VALID_AI_PROVIDERS)}; "
            f"got {provider!r}"
        )
    try:
        max_in = int(raw.get("max_input_chars", 8000))
        timeout = int(raw.get("request_timeout_s", 60))
    except (TypeError, ValueError):
        raise DistrictConfigError(
            "ai.max_input_chars and ai.request_timeout_s must be integers"
        )
    if max_in < 100:
        raise DistrictConfigError("ai.max_input_chars must be >= 100")
    if timeout < 1:
        raise DistrictConfigError("ai.request_timeout_s must be >= 1")
    return AiConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=provider,
        model=(str(raw["model"]) if raw.get("model") else None),
        base_url=(str(raw["base_url"]) if raw.get("base_url") else None),
        api_key_env=str(raw.get("api_key_env", "FOIA_AI_API_KEY")),
        max_input_chars=max_in,
        request_timeout_s=timeout,
    )


def _parse_bates(raw: dict | None) -> BatesConfig:
    if not raw:
        return BatesConfig()
    if not isinstance(raw, dict):
        raise DistrictConfigError("bates must be a mapping")
    try:
        start = int(raw.get("start", 1))
        width = int(raw.get("width", 6))
    except (TypeError, ValueError):
        raise DistrictConfigError("bates.start and bates.width must be integers")
    if start < 0:
        raise DistrictConfigError("bates.start must be >= 0")
    if width < 1 or width > 20:
        raise DistrictConfigError("bates.width must be between 1 and 20")
    return BatesConfig(
        prefix=str(raw.get("prefix", "DOC")),
        start=start,
        width=width,
    )


def _parse_exemptions(raw: list | None) -> tuple[ExemptionCode, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise DistrictConfigError("exemption_codes must be a list")
    out: list[ExemptionCode] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append(ExemptionCode(code=item))
            continue
        if not isinstance(item, dict) or "code" not in item:
            raise DistrictConfigError(
                f"exemption_codes[{i}] must be a string or have a 'code' key"
            )
        out.append(
            ExemptionCode(
                code=str(item["code"]),
                description=str(item.get("description") or ""),
            )
        )
    return tuple(out)


def _parse_redaction(raw: dict | None) -> RedactionConfig:
    if not raw:
        return RedactionConfig()
    if not isinstance(raw, dict):
        raise DistrictConfigError("redaction must be a mapping")
    default = raw.get("default_exemption")
    mapping_raw = raw.get("entity_exemptions") or {}
    if not isinstance(mapping_raw, dict):
        raise DistrictConfigError("redaction.entity_exemptions must be a mapping")
    mapping = {str(k): str(v) for k, v in mapping_raw.items()}
    return RedactionConfig(
        default_exemption=str(default) if default else None,
        entity_exemptions=mapping,
    )


__all__ = [
    "AiConfig",
    "BatesConfig",
    "CustomRecognizerSpec",
    "DistrictConfig",
    "DistrictConfigError",
    "ExemptionCode",
    "PatternSpec",
    "PiiDetectionConfig",
    "RedactionConfig",
    "load_district_config",
]
