"""Tests for the district YAML config loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from foia.district import DistrictConfigError, load_district_config


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


def test_missing_file_returns_defaults(tmp_path: Path):
    cfg = load_district_config(tmp_path / "nope.yaml")
    assert cfg.name == "Unconfigured District"
    assert cfg.email_domains == ()
    assert cfg.pii.min_score == 0.3
    assert "US_SSN" in cfg.pii.builtins


def test_full_yaml(tmp_path: Path):
    path = _write(
        tmp_path / "d.yaml",
        """
        district:
          name: Test District
          email_domains:
            - test.k12.va.us
            - DISTRICT.org
        pii_detection:
          builtins: [US_SSN, EMAIL_ADDRESS]
          min_score: 0.5
          enable_ner: false
          custom_recognizers:
            - name: Student ID
              entity_type: STUDENT_ID
              patterns:
                - regex: '\\b\\d{8}\\b'
                  score: 0.7
              context: [student, id]
            - name: Staff ID
              entity_type: STAFF_ID
              patterns:
                - regex: 'STF-\\d+'
                  score: 0.9
        exemption_codes:
          - code: FERPA
            description: Schools.
        """,
    )
    cfg = load_district_config(path)
    assert cfg.name == "Test District"
    assert cfg.email_domains == ("test.k12.va.us", "district.org")  # lowercased
    assert cfg.pii.builtins == ("US_SSN", "EMAIL_ADDRESS")
    assert cfg.pii.min_score == 0.5
    assert len(cfg.pii.custom_recognizers) == 2
    first = cfg.pii.custom_recognizers[0]
    assert first.name == "Student ID"
    assert first.entity_type == "STUDENT_ID"
    assert first.context == ("student", "id")
    assert first.patterns[0].regex == r"\b\d{8}\b"
    assert first.patterns[0].score == 0.7
    # Future-phase keys are preserved on `raw` but not consumed here.
    assert cfg.raw["exemption_codes"][0]["code"] == "FERPA"


def test_custom_recognizer_needs_patterns(tmp_path: Path):
    path = _write(
        tmp_path / "bad.yaml",
        """
        pii_detection:
          custom_recognizers:
            - name: No patterns
              entity_type: FOO
        """,
    )
    with pytest.raises(DistrictConfigError):
        load_district_config(path)


def test_custom_recognizer_needs_entity_type(tmp_path: Path):
    path = _write(
        tmp_path / "bad2.yaml",
        """
        pii_detection:
          custom_recognizers:
            - name: No entity
              patterns:
                - regex: X
        """,
    )
    with pytest.raises(DistrictConfigError):
        load_district_config(path)


def test_example_config_loads(tmp_path: Path):
    """The committed example config must stay loadable."""
    from pathlib import Path as P
    root = P(__file__).resolve().parents[1]
    cfg = load_district_config(root / "config" / "district.example.yaml")
    assert cfg.name
    assert len(cfg.pii.custom_recognizers) >= 3
    # STUDENT_ID, EMPLOYEE_ID, LUNCH_ACCT, narrative DATE_TIME all present
    entity_types = {r.entity_type for r in cfg.pii.custom_recognizers}
    assert {"STUDENT_ID", "EMPLOYEE_ID", "LUNCH_ACCT", "DATE_TIME"} <= entity_types


def test_malformed_yaml_top_level_list(tmp_path: Path):
    path = _write(tmp_path / "list.yaml", "- just a list")
    with pytest.raises(DistrictConfigError):
        load_district_config(path)


def test_env_var_picked_up(tmp_path: Path, monkeypatch):
    path = _write(
        tmp_path / "e.yaml",
        """
        district: {name: Env District}
        pii_detection: {min_score: 0.7}
        """,
    )
    monkeypatch.setenv("FOIA_CONFIG_FILE", str(path))
    cfg = load_district_config()
    assert cfg.name == "Env District"
    assert cfg.pii.min_score == 0.7
