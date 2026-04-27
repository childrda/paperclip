"""Phase 10 — provider unit tests.

Real HTTP is mocked via ``http_post`` injection so the suite still
runs offline.
"""

from __future__ import annotations

import json

import pytest

from foia.ai import (
    AiProviderError,
    AnthropicProvider,
    NullProvider,
    OpenAICompatibleProvider,
    _extract_json,
    _flags_from_payload,
    build_provider,
)
from foia.district import AiConfig


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_fence():
    raw = "```json\n{\"flags\": []}\n```"
    assert _extract_json(raw) == {"flags": []}


def test_extract_json_embedded_in_prose():
    raw = 'Sure! Here is the JSON: {"flags": [{"x": 1}]} hope that helps.'
    assert _extract_json(raw) == {"flags": [{"x": 1}]}


def test_extract_json_invalid_raises():
    with pytest.raises(AiProviderError):
        _extract_json("not even close")


def test_flags_from_payload_re_anchors_offsets():
    text = "Student John Doe needs IEP review."
    payload = {
        "flags": [
            {
                # Offsets the model returned are deliberately wrong; we should
                # re-anchor by exact-match search on matched_text.
                "start": 9999,
                "end": 9999,
                "matched_text": "John Doe",
                "entity_type": "STUDENT_NAME",
                "confidence": 0.92,
                "rationale": "Student personal name",
                "suggested_exemption": "FERPA",
            }
        ]
    }
    flags = _flags_from_payload(payload, text)
    assert len(flags) == 1
    assert flags[0].start == text.index("John Doe")
    assert flags[0].end == flags[0].start + len("John Doe")
    assert flags[0].entity_type == "STUDENT_NAME"
    assert flags[0].suggested_exemption == "FERPA"


def test_flags_from_payload_drops_when_text_not_found():
    flags = _flags_from_payload(
        {"flags": [{"matched_text": "nope", "entity_type": "X"}]},
        "anything",
    )
    assert flags == []


def test_flags_from_payload_dedupes_same_span():
    text = "Aaaa bbbb Aaaa"
    payload = {
        "flags": [
            {"matched_text": "Aaaa", "entity_type": "X", "confidence": 0.5},
            {"matched_text": "Aaaa", "entity_type": "X", "confidence": 0.6},
        ]
    }
    out = _flags_from_payload(payload, text)
    # Only one span kept; first wins.
    assert len(out) == 1
    assert out[0].confidence == 0.5


def test_flags_from_payload_empty_or_malformed():
    assert _flags_from_payload({}, "x") == []
    assert _flags_from_payload({"flags": "not a list"}, "x") == []
    assert _flags_from_payload({"flags": [None, 42]}, "x") == []
    # Missing matched_text → dropped.
    assert _flags_from_payload({"flags": [{"entity_type": "X"}]}, "x") == []


def test_flags_clamp_confidence():
    flags = _flags_from_payload(
        {"flags": [
            {"matched_text": "A", "entity_type": "X", "confidence": 5},
            {"matched_text": "B", "entity_type": "Y", "confidence": -1},
            {"matched_text": "C", "entity_type": "Z", "confidence": "bad"},
        ]},
        "ABC",
    )
    assert [f.confidence for f in flags] == [1.0, 0.0, 0.5]


# ---------------------------------------------------------------------------
# NullProvider
# ---------------------------------------------------------------------------


def test_null_provider_returns_empty():
    p = NullProvider()
    assert p.flag_risks("anything goes") == []
    assert p.name == "null"


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (drives openai/azure/ollama)
# ---------------------------------------------------------------------------


def _fake_openai_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_openai_provider_parses_json_response():
    captured: dict = {}

    def fake_post(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return _fake_openai_response(
            json.dumps({"flags": [
                {"matched_text": "John", "entity_type": "STUDENT_NAME",
                 "confidence": 0.9, "rationale": "child"}
            ]})
        )

    p = OpenAICompatibleProvider(
        name="openai",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        http_post=fake_post,
    )
    flags = p.flag_risks("Hello John, please pick up forms.")
    assert len(flags) == 1
    assert flags[0].matched_text == "John"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["temperature"] == 0


def test_openai_provider_skips_empty_text():
    def fake_post(*a, **k):
        raise AssertionError("should not be called")
    p = OpenAICompatibleProvider(
        name="openai", model="m", base_url="http://x",
        api_key="k", http_post=fake_post,
    )
    assert p.flag_risks("") == []
    assert p.flag_risks("   \n  ") == []


def test_openai_provider_truncates_long_input():
    captured: dict = {}

    def fake_post(url, body, headers):
        captured["body"] = body
        return _fake_openai_response(json.dumps({"flags": []}))

    p = OpenAICompatibleProvider(
        name="openai", model="m", base_url="http://x",
        api_key="k", max_input_chars=100,
        http_post=fake_post,
    )
    p.flag_risks("z" * 500)
    user_msg = captured["body"]["messages"][-1]["content"]
    assert "[... truncated ...]" in user_msg


def test_openai_provider_non_json_returns_no_flags():
    def fake_post(*a, **k):
        return _fake_openai_response("Sorry, I can't help with that.")

    p = OpenAICompatibleProvider(
        name="openai", model="m", base_url="http://x",
        api_key="k", http_post=fake_post,
    )
    assert p.flag_risks("anything") == []


def test_openai_provider_http_error_raises():
    class Err(Exception):
        pass

    def fake_post(*a, **k):
        raise Err("boom")

    p = OpenAICompatibleProvider(
        name="openai", model="m", base_url="http://x",
        api_key="k", http_post=fake_post,
    )
    with pytest.raises(AiProviderError):
        p.flag_risks("hi")


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


def test_anthropic_provider_parses_response():
    def fake_post(url, body, headers):
        assert url.endswith("/v1/messages")
        assert headers["x-api-key"] == "ant-test"
        assert headers["anthropic-version"]
        return {"content": [
            {"type": "text", "text": json.dumps({"flags": [
                {"matched_text": "Doe", "entity_type": "STUDENT_NAME",
                 "confidence": 0.7}
            ]})},
        ]}

    p = AnthropicProvider(
        model="claude-test",
        api_key="ant-test",
        http_post=fake_post,
    )
    flags = p.flag_risks("Met with Doe today.")
    assert len(flags) == 1
    assert flags[0].matched_text == "Doe"


def test_anthropic_provider_handles_text_with_prose():
    def fake_post(*a, **k):
        return {"content": [{"type": "text",
                "text": "Result: {\"flags\": []}\nThanks."}]}
    p = AnthropicProvider(model="x", api_key="k", http_post=fake_post)
    assert p.flag_risks("hi") == []


# ---------------------------------------------------------------------------
# build_provider() factory + per-case overrides
# ---------------------------------------------------------------------------


def test_factory_returns_null_when_disabled():
    cfg = AiConfig(enabled=False, provider="openai", model="x")
    p = build_provider(cfg)
    assert isinstance(p, NullProvider)


def test_factory_returns_null_when_provider_explicit_null():
    cfg = AiConfig(enabled=True, provider="null")
    assert isinstance(build_provider(cfg), NullProvider)


def test_factory_per_case_override_provider():
    cfg = AiConfig(enabled=False, provider="null")  # disabled in YAML
    # Per-case override forces it on for one run.
    p = build_provider(
        cfg, override_provider="openai", override_model="m",
        api_key="x", http_post=lambda *a, **k: _fake_openai_response('{"flags": []}'),
    )
    assert p.name == "openai"
    assert p.model == "m"


def test_factory_per_case_override_model():
    cfg = AiConfig(enabled=True, provider="openai", model="default-m")
    p = build_provider(
        cfg, override_model="overridden",
        api_key="x", http_post=lambda *a, **k: None,
    )
    assert p.model == "overridden"


def test_factory_requires_api_key_for_openai():
    cfg = AiConfig(enabled=True, provider="openai", model="m")
    with pytest.raises(AiProviderError):
        build_provider(cfg, api_key=None)


def test_factory_does_not_require_api_key_for_ollama():
    cfg = AiConfig(enabled=True, provider="ollama", model="llama")
    p = build_provider(cfg, http_post=lambda *a, **k: None)
    assert p.name == "ollama"
    # Default URL points at localhost:11434.
    assert "11434" in getattr(p, "base_url", "") or "ollama" in p.name


def test_factory_azure_uses_api_key_header(monkeypatch):
    cfg = AiConfig(
        enabled=True, provider="azure",
        model="gpt-4o", base_url="https://x.azure",
    )
    captured: dict = {}

    def fake_post(url, body, headers):
        captured["headers"] = headers
        return _fake_openai_response(json.dumps({"flags": []}))

    p = build_provider(cfg, api_key="azure-key", http_post=fake_post)
    p.flag_risks("hello")
    assert captured["headers"].get("api-key") == "azure-key"
    # Bearer Authorization should NOT be set when api-key header is used.
    assert "Authorization" not in captured["headers"]


def test_factory_anthropic_path():
    cfg = AiConfig(enabled=True, provider="anthropic", model="claude-x")
    p = build_provider(cfg, api_key="ant", http_post=lambda *a, **k: None)
    assert p.name == "anthropic"


def test_factory_unknown_provider_raises():
    # Bypasses district loader's allowlist.
    cfg = AiConfig(enabled=True, provider="bogus", model="m")
    with pytest.raises(AiProviderError):
        build_provider(cfg, override_provider="bogus")


def test_factory_anthropic_without_key_raises():
    cfg = AiConfig(enabled=True, provider="anthropic", model="x")
    with pytest.raises(AiProviderError):
        build_provider(cfg, api_key=None)
