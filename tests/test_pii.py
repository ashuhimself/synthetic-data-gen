"""Tests for the PII detection layer."""

from __future__ import annotations

import pytest

from datagen_extractor.pii import load_patterns, scan_schemas
from datagen_extractor.schema import Field, TableSchema


def make_table(name, fields):
    return TableSchema(table_name=name, fields=fields)


def test_detects_common_pii_by_name():
    schema = make_table(
        "customers",
        [
            Field(name="first_name", type="string", pii=True),
            Field(name="SSN", type="string", pii=True),
            Field(name="email", type="string", pii=True),
            Field(name="date_of_birth", type="date", pii=True),
            Field(name="balance", type="decimal"),
        ],
    )
    report = scan_schemas([schema])
    flagged = {f.column for f in report.findings}
    assert {"first_name", "SSN", "email", "date_of_birth"} <= flagged
    assert "balance" not in flagged
    assert report.untagged == []


def test_untagged_pii_is_flagged_as_risk():
    schema = make_table(
        "customers",
        [Field(name="last_name", type="string", pii=False)],
    )
    report = scan_schemas([schema])
    assert len(report.untagged) == 1
    assert report.untagged[0].column == "last_name"
    assert report.untagged[0].category == "name"


def test_declared_pii_without_pattern_match_still_reported():
    schema = make_table(
        "loans",
        [Field(name="collateral_notes", type="text", pii=True)],
    )
    report = scan_schemas([schema])
    [finding] = report.findings
    assert finding.category == "declared"
    assert finding.tagged


def test_format_based_detection():
    schema = make_table(
        "contacts",
        [Field(name="contact_value", type="string", format="email", pii=False)],
    )
    report = scan_schemas([schema])
    assert report.untagged and report.untagged[0].category == "email"


def test_custom_patterns_from_yaml(tmp_path):
    pattern_file = tmp_path / "patterns.yaml"
    pattern_file.write_text("- category: internal_id\n  name_pattern: employee_badge\n")
    patterns = load_patterns(pattern_file)
    schema = make_table(
        "staff",
        [Field(name="employee_badge", type="string")],
    )
    report = scan_schemas([schema], patterns)
    assert report.untagged[0].category == "internal_id"


def test_bad_pattern_file_rejected(tmp_path):
    pattern_file = tmp_path / "patterns.yaml"
    pattern_file.write_text("not_a: list\n")
    with pytest.raises(ValueError, match="YAML list"):
        load_patterns(pattern_file)
