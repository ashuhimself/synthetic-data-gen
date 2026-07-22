"""Tests for charset and control_char_rate (schema + harness enforcement)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from pydantic import ValidationError

from datagen_extractor.graph import SchemaGraph
from datagen_extractor.harness import run_checks
from datagen_extractor.schema import Field, TableSchema

# Schema validation


class TestSchemaValidation:
    def test_charset_unicode_on_string(self):
        f = Field(name="full_name", type="string", charset="unicode")
        assert f.charset == "unicode"

    def test_charset_rejected_on_non_text_type(self):
        with pytest.raises(ValidationError, match="not a text type"):
            Field(name="amount", type="decimal", charset="unicode")

    def test_unknown_charset_rejected(self):
        with pytest.raises(ValidationError, match="not in allowed set"):
            Field(name="full_name", type="string", charset="utf16")

    def test_control_char_rate_on_text(self):
        f = Field(name="memo", type="text", control_char_rate=0.05)
        assert f.control_char_rate == 0.05

    def test_control_char_rate_rejected_on_non_text(self):
        with pytest.raises(ValidationError, match="not a text type"):
            Field(name="opened", type="date", control_char_rate=0.1)

    @pytest.mark.parametrize("rate", [-0.1, 1.5])
    def test_control_char_rate_out_of_range(self, rate):
        with pytest.raises(ValidationError, match="between 0.0 and 1.0"):
            Field(name="memo", type="text", control_char_rate=rate)


# Harness enforcement


@pytest.fixture
def schema() -> TableSchema:
    return TableSchema(
        table_name="customers",
        fields=[
            Field(name="customer_id", type="integer", unique=True),
            Field(name="full_name", type="string", charset="unicode", pii=True),
            Field(name="memo", type="text", control_char_rate=0.2),
            Field(name="code", type="string", charset="ascii"),
        ],
    )


def write_rows(path: Path, rows: list[dict]) -> None:
    with (path / "customers.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["customer_id", "full_name", "memo", "code"])
        w.writeheader()
        w.writerows(rows)


def make_rows(n, unicode_every=3, control_every=5, ascii_clean=True):
    rows = []
    for i in range(n):
        rows.append(
            {
                "customer_id": str(i),
                "full_name": (
                    "José Müller"
                    if unicode_every is not None and i % unicode_every == 0
                    else "John Smith"
                ),
                "memo": (
                    "line1\tline2"
                    if control_every is not None and i % control_every == 0
                    else "plain memo"
                ),
                "code": "C123" if ascii_clean else "Cé23",
            }
        )
    return rows


def test_conforming_data_passes(schema, tmp_path):
    write_rows(tmp_path, make_rows(200))  # 20% control chars, 33% unicode
    report = run_checks(SchemaGraph([schema]), tmp_path)
    assert report.passed, report.failures
    checks = {r.check for r in report.results}
    assert "charset" in checks and "control_char_rate" in checks


def test_unicode_column_with_only_ascii_fails(schema, tmp_path):
    rows = make_rows(200, unicode_every=None)  # never unicode
    write_rows(tmp_path, rows)
    report = run_checks(SchemaGraph([schema]), tmp_path)
    [fail] = [r for r in report.failures if r.check == "charset"]
    assert "pure ASCII" in fail.detail


def test_ascii_column_with_unicode_fails(schema, tmp_path):
    write_rows(tmp_path, make_rows(200, ascii_clean=False))
    report = run_checks(SchemaGraph([schema]), tmp_path)
    fails = [r for r in report.failures if r.check == "charset"]
    assert any("declared charset ascii" in f.detail for f in fails)


def test_control_char_rate_drift_fails(schema, tmp_path):
    rows = make_rows(200, control_every=None)  # 0% observed vs declared 0.2
    write_rows(tmp_path, rows)
    report = run_checks(SchemaGraph([schema]), tmp_path)
    [fail] = [r for r in report.failures if r.check == "control_char_rate"]
    assert "declared 0.20" in fail.detail


def test_embedded_newline_survives_csv_roundtrip(schema, tmp_path):
    rows = make_rows(200)
    rows[0]["memo"] = "first line\nsecond line"
    write_rows(tmp_path, rows)
    report = run_checks(SchemaGraph([schema]), tmp_path)
    # File loads fine (csv quoting) and row count is preserved.
    [presence] = [r for r in report.results if r.check == "file_present"]
    assert presence.passed and "200 rows" in presence.detail


def test_checks_skipped_below_thresholds(schema, tmp_path):
    write_rows(tmp_path, make_rows(20))
    report = run_checks(SchemaGraph([schema]), tmp_path)
    assert not any(r.check == "control_char_rate" for r in report.results)
    # unicode charset check needs 30+; ascii check always runs
    assert not any(r.check == "charset" and "non-ASCII values" in r.detail for r in report.results)
