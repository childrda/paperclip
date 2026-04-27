"""Phase 10 — pluggable AI QA backend.

The :class:`AiProvider` ABC is the only thing the rest of the system
depends on. Concrete implementations include:

* :class:`NullProvider` — built-in, always returns []. Used when AI is
  disabled, in tests, and as the safe fallback when a real provider
  cannot be configured.
* :class:`OpenAICompatibleProvider` — drives OpenAI, Azure OpenAI, and
  Ollama (the latter exposes an OpenAI-compatible API at
  ``http://localhost:11434/v1``). Single class, configurable
  ``base_url`` and headers.
* :class:`AnthropicProvider` — Anthropic Messages API.

The driver in :mod:`foia.ai_driver` is the only caller. It treats every
provider as a pure function from text to a list of :class:`AiFlag`
records, so swapping providers (or mocking them in tests) is one
constructor away.

**Hard rule (spec):** AI never auto-redacts. There is no path from
:class:`AiFlag` to a :mod:`foia.redaction` row that doesn't go through
explicit human action.
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from .district import AiConfig

log = logging.getLogger(__name__)


# Default base URLs by provider name (overridable via cfg.base_url).
_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434/v1",
    "anthropic": "https://api.anthropic.com",
}

# Default model when the YAML doesn't specify one.
_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "azure": "gpt-4o-mini",
    "ollama": "llama3.1",
    "anthropic": "claude-3-5-haiku-latest",
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AiFlag:
    """One advisory flag from the AI."""
    entity_type: str
    start: int
    end: int
    matched_text: str
    confidence: float
    rationale: str = ""
    suggested_exemption: str | None = None


class AiProviderError(RuntimeError):
    """Raised when a provider can't be configured or its call fails."""


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """You are a FOIA review assistant for a K-12 school district.
You scan one document at a time and flag risks that a reviewer should consider
redacting. You DO NOT redact; you only flag.

Return JSON with the following shape and nothing else:
{
  "flags": [
    {
      "entity_type": "STUDENT_NAME" | "MEDICAL" | "DISCIPLINE" | "FAMILY_FINANCIAL"
                   | "PROTECTED_OPINION" | "MINOR_REFERENCE" | "OTHER_FERPA"
                   | "OTHER",
      "matched_text": "<exact substring of the document>",
      "confidence": 0.0..1.0,
      "rationale": "<short reason>",
      "suggested_exemption": "FERPA" | "HIPAA" | "PII" | null
    }
  ]
}

Rules:
- matched_text MUST be an exact substring of the input. Do not paraphrase.
- Skip generic SSNs / phone / email — those are caught by deterministic
  recognizers; only flag risks that pattern matching misses.
- If you find no flags, return {"flags": []}.
"""


def _build_user_prompt(text: str, max_input_chars: int) -> str:
    if len(text) > max_input_chars:
        text = text[:max_input_chars] + "\n[... truncated ...]"
    return f"DOCUMENT:\n{text}\n\nReturn JSON only."


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------


class AiProvider(ABC):
    name: str = "null"
    model: str | None = None

    @abstractmethod
    def flag_risks(self, text: str) -> list[AiFlag]:
        """Scan ``text`` and return zero or more flags. Pure function."""


# ---------------------------------------------------------------------------
# Null provider
# ---------------------------------------------------------------------------


class NullProvider(AiProvider):
    """Returns no flags. Default when AI is disabled."""
    name = "null"
    model = None

    def flag_risks(self, text: str) -> list[AiFlag]:
        return []


# ---------------------------------------------------------------------------
# Internal: parse / normalise the LLM JSON output
# ---------------------------------------------------------------------------


_JSON_BLOB_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(s: str) -> dict[str, Any]:
    """Tolerate models that wrap their JSON in commentary or ``` fences."""
    s = s.strip()
    if s.startswith("```"):
        # Strip leading ```json … ``` fences.
        s = s.strip("`")
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0] if s.endswith("```") else s
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOB_RE.search(s)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise AiProviderError(f"AI output was not valid JSON: {s[:200]!r}")


def _flags_from_payload(payload: dict[str, Any], source_text: str) -> list[AiFlag]:
    """Convert provider-returned JSON into typed flags.

    Char offsets returned by the LLM are unreliable; we always re-anchor
    against ``source_text`` by searching for ``matched_text``.
    """
    items = payload.get("flags") or []
    if not isinstance(items, list):
        return []
    out: list[AiFlag] = []
    seen_locs: set[tuple[int, int, str]] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        matched = str(raw.get("matched_text") or "").strip()
        if not matched:
            continue
        # Re-anchor by exact-match search.
        idx = source_text.find(matched)
        if idx < 0:
            log.debug("AI flag matched_text not found in source: %r", matched[:80])
            continue
        start = idx
        end = idx + len(matched)
        entity = str(raw.get("entity_type") or "OTHER").upper()
        try:
            confidence = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        suggested = raw.get("suggested_exemption")
        suggested = str(suggested) if suggested else None

        key = (start, end, entity)
        if key in seen_locs:
            continue
        seen_locs.add(key)

        out.append(
            AiFlag(
                entity_type=entity,
                start=start,
                end=end,
                matched_text=matched,
                confidence=confidence,
                rationale=str(raw.get("rationale") or "")[:1000],
                suggested_exemption=suggested,
            )
        )
    return out


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (OpenAI / Azure / Ollama)
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(AiProvider):
    """Drives any backend that speaks OpenAI's ``/chat/completions``.

    Tested in unit tests via dependency injection of a fake
    ``http_post`` callable; production callers use the default
    ``httpx.post``.
    """

    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key: str | None = None,
        max_input_chars: int = 8000,
        timeout_s: int = 60,
        extra_headers: dict[str, str] | None = None,
        http_post: Callable | None = None,
    ):
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_input_chars = max_input_chars
        self.timeout_s = timeout_s
        self.extra_headers = extra_headers or {}
        self._http_post = http_post

    # Pluggable HTTP for testability.
    def _post(self, url: str, json_body: dict, headers: dict) -> dict:
        if self._http_post is not None:
            return self._http_post(url, json_body, headers)
        import httpx
        res = httpx.post(url, json=json_body, headers=headers, timeout=self.timeout_s)
        if res.status_code >= 400:
            raise AiProviderError(
                f"{self.name} HTTP {res.status_code}: {res.text[:200]}"
            )
        return res.json()

    def flag_risks(self, text: str) -> list[AiFlag]:
        if not text or not text.strip():
            return []
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(text, self.max_input_chars)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        try:
            res = self._post(f"{self.base_url}/chat/completions", body, headers)
            content = res["choices"][0]["message"]["content"]
        except AiProviderError:
            raise
        except Exception as e:
            raise AiProviderError(f"{self.name} call failed: {e}") from e
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        try:
            payload = _extract_json(str(content))
        except AiProviderError:
            log.exception("provider %s returned non-JSON content", self.name)
            return []
        return _flags_from_payload(payload, text)


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------


class AnthropicProvider(AiProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        max_input_chars: int = 8000,
        timeout_s: int = 60,
        http_post: Callable | None = None,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_input_chars = max_input_chars
        self.timeout_s = timeout_s
        self._http_post = http_post

    def _post(self, url: str, json_body: dict, headers: dict) -> dict:
        if self._http_post is not None:
            return self._http_post(url, json_body, headers)
        import httpx
        res = httpx.post(url, json=json_body, headers=headers, timeout=self.timeout_s)
        if res.status_code >= 400:
            raise AiProviderError(
                f"anthropic HTTP {res.status_code}: {res.text[:200]}"
            )
        return res.json()

    def flag_risks(self, text: str) -> list[AiFlag]:
        if not text or not text.strip():
            return []
        body = {
            "model": self.model,
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": _build_user_prompt(text, self.max_input_chars),
                }
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            res = self._post(f"{self.base_url}/v1/messages", body, headers)
            blocks = res.get("content") or []
            text_out = "".join(
                b.get("text", "") for b in blocks if isinstance(b, dict)
            )
        except AiProviderError:
            raise
        except Exception as e:
            raise AiProviderError(f"anthropic call failed: {e}") from e
        try:
            payload = _extract_json(text_out)
        except AiProviderError:
            log.exception("anthropic returned non-JSON content")
            return []
        return _flags_from_payload(payload, text)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_provider(
    cfg: AiConfig,
    *,
    override_provider: str | None = None,
    override_model: str | None = None,
    api_key: str | None = None,
    http_post: Callable | None = None,
) -> AiProvider:
    """Resolve config + per-case overrides into a concrete provider.

    Per-case overrides win over the YAML config. If the resolved provider
    is anything other than ``null`` and the configured key env var is
    empty, an :class:`AiProviderError` is raised — it's better to fail
    loudly than silently fall back.
    """
    name = (override_provider or cfg.provider or "null").lower()
    if name == "null" or not cfg.enabled and override_provider is None:
        return NullProvider()

    model = override_model or cfg.model or _DEFAULT_MODELS.get(name)
    if not model:
        raise AiProviderError(f"no model configured for provider {name!r}")

    if api_key is None:
        api_key = os.environ.get(cfg.api_key_env or "FOIA_AI_API_KEY")

    base_url = cfg.base_url or _DEFAULT_BASE_URLS.get(name)

    if name in ("openai", "azure", "ollama"):
        if not base_url:
            raise AiProviderError(f"{name}: base_url is required")
        # Ollama doesn't require a key; OpenAI / Azure do.
        if name in ("openai", "azure") and not api_key:
            raise AiProviderError(
                f"{name}: API key is required (env {cfg.api_key_env})"
            )
        extra: dict[str, str] = {}
        if name == "azure" and api_key:
            # Azure OpenAI uses api-key header instead of bearer auth.
            extra["api-key"] = api_key
            api_key = None  # don't also send Authorization
        return OpenAICompatibleProvider(
            name=name, model=model, base_url=base_url,
            api_key=api_key,
            max_input_chars=cfg.max_input_chars,
            timeout_s=cfg.request_timeout_s,
            extra_headers=extra,
            http_post=http_post,
        )

    if name == "anthropic":
        if not api_key:
            raise AiProviderError(
                f"anthropic: API key is required (env {cfg.api_key_env})"
            )
        return AnthropicProvider(
            model=model, api_key=api_key,
            base_url=base_url or _DEFAULT_BASE_URLS["anthropic"],
            max_input_chars=cfg.max_input_chars,
            timeout_s=cfg.request_timeout_s,
            http_post=http_post,
        )

    raise AiProviderError(f"unknown ai provider: {name!r}")


__all__ = [
    "AiFlag",
    "AiProvider",
    "AiProviderError",
    "AnthropicProvider",
    "NullProvider",
    "OpenAICompatibleProvider",
    "build_provider",
]
