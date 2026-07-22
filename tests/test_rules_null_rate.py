"""Tests for null_rate and cross-field rules (schema + harness enforcement)."""

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
    def test_null_rate_valid(self):
        f = Field(name="close_date", type="date", nullable=True, null_rate=0.7)
        assert f.null_rate == 0.7

    def test_null_rate_requires_nullable(self):
        with pytest.raises(ValidationError, match="nullable is false"):
            Field(name="close_date", type="date", null_rate=0.7)

    @pytest.mark.parametrize("rate", [-0.1, 1.5])
    def test_null_rate_out_of_range(self, rate):
        with pytest.raises(ValidationError, match="between 0.0 and 1.0"):
            Field(name="x", type="date", nullable=True, null_rate=rate)

    def test_rule_valid(self):
        t = TableSchema(
            table_name="accounts",
            fields=[
                Field(name="status", type="enum", distribution="enum:open,closed"),
                Field(name="close_date", type="date", nullable=True),
            ],
            rules=[{"when": {"status": "closed"}, "then": {"close_date": "not_null"}}],
        )
        assert t.rules[0].then == {"close_date": "not_null"}

    def test_rule_unknown_column_rejected(self):
        with pytest.raises(ValidationError, match="unknown columns"):
            TableSchema(
                table_name="accounts",
                fields=[Field(name="status", type="string")],
                rules=[{"when": {"status": "closed"}, "then": {"ghost": "not_null"}}],
            )

    def test_rule_unknown_effect_rejected(self):
        with pytest.raises(ValidationError, match="not in allowed set"):
            TableSchema(
                table_name="accounts",
                fields=[
                    Field(name="status", type="string"),
                    Field(name="close_date", type="date", nullable=True),
                ],
                rules=[{"when": {"status": "closed"}, "then": {"close_date": "must_exist"}}],
            )

    def test_rule_empty_when_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            TableSchema(
                table_name="accounts",
                fields=[Field(name="close_date", type="date", nullable=True)],
                rules=[{"when": {}, "then": {"close_date": "null"}}],
            )


# Harness enforcement


@pytest.fixture
def accounts_schema() -> TableSchema:
    return TableSchema(
        table_name="accounts",
        fields=[
            Field(name="account_id", type="integer", unique=True),
            Field(name="status", type="enum", distribution="enum:open,closed"),
            Field(name="close_date", type="date", nullable=True, null_rate=0.5),
        ],
        rules=[
            {"when": {"status": "closed"}, "then": {"close_date": "not_null"}},
            {"when": {"status": "open"}, "then": {"close_date": "null"}},
        ],
    )


def write_accounts(path: Path, rows: list[dict]) -> None:
    with (path / "accounts.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["account_id", "status", "close_date"])
        w.writeheader()
        w.writerows(rows)


def make_rows(n: int, closed_fraction: float = 0.5, violate_rule: bool = False):
    rows = []
    n_closed = int(n * closed_fraction)
    for i in range(n):
        closed = i < n_closed
        close_date = "2020-01-01" if closed else ""
        if violate_rule and i == 0:
            close_date = ""  # closed account with no close_date
        rows.append(
            {
                "account_id": str(i),
                "status": "closed" if closed else "open",
                "close_date": close_date,
            }
        )
    return rows


def test_rules_and_null_rate_pass(accounts_schema, tmp_path):
    write_accounts(tmp_path, make_rows(200))
    report = run_checks(SchemaGraph([accounts_schema]), tmp_path)
    assert report.passed, report.failures
    checks = {r.check for r in report.results}
    assert "rule" in checks and "null_rate" in checks


def test_rule_violation_detected(accounts_schema, tmp_path):
    write_accounts(tmp_path, make_rows(200, violate_rule=True))
    report = run_checks(SchemaGraph([accounts_schema]), tmp_path)
    [fail] = [r for r in report.failures if r.check == "rule"]
    assert "1 violations" in fail.detail


def test_null_rate_drift_detected(accounts_schema, tmp_path):
    # 90% closed → close_date null only 10% of the time vs declared 0.5.
    write_accounts(tmp_path, make_rows(200, closed_fraction=0.9))
    report = run_checks(SchemaGraph([accounts_schema]), tmp_path)
    [fail] = [r for r in report.failures if r.check == "null_rate"]
    assert "declared 0.50" in fail.detail


def test_null_rate_skipped_below_threshold(accounts_schema, tmp_path):
    write_accounts(tmp_path, make_rows(20, closed_fraction=0.9))
    report = run_checks(SchemaGraph([accounts_schema]), tmp_path)
    assert not any(r.check == "null_rate" for r in report.results)
