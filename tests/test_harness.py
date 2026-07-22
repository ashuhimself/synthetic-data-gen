"""Tests for the post-generation validation harness."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from datagen_extractor.graph import SchemaGraph
from datagen_extractor.harness import run_checks
from datagen_extractor.schema import Field, TableSchema


@pytest.fixture
def graph() -> SchemaGraph:
    customers = TableSchema(
        table_name="customers",
        fields=[
            Field(name="customer_id", type="integer", unique=True),
            Field(name="segment", type="enum", distribution="enum:MASS,AFFLUENT"),
        ],
    )
    accounts = TableSchema(
        table_name="accounts",
        fields=[
            Field(name="account_id", type="integer", unique=True),
            Field(name="customer_id", type="integer", fk_ref="customers.customer_id"),
            Field(name="branch", type="string"),
            Field(name="number", type="string"),
        ],
        unique_together=[["branch", "number"]],
    )
    return SchemaGraph([customers, accounts])


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


GOOD_CUSTOMERS = [
    {"customer_id": "1", "segment": "MASS"},
    {"customer_id": "2", "segment": "AFFLUENT"},
]
GOOD_ACCOUNTS = [
    {"account_id": "10", "customer_id": "1", "branch": "B1", "number": "001"},
    {"account_id": "11", "customer_id": "2", "branch": "B1", "number": "002"},
]


def test_clean_data_passes(graph, tmp_path):
    write_csv(tmp_path / "customers.csv", GOOD_CUSTOMERS)
    write_csv(tmp_path / "accounts.csv", GOOD_ACCOUNTS)
    report = run_checks(graph, tmp_path)
    assert report.passed, report.failures


def test_json_data_supported(graph, tmp_path):
    (tmp_path / "customers.json").write_text(
        json.dumps(
            [{"customer_id": 1, "segment": "MASS"}, {"customer_id": 2, "segment": "AFFLUENT"}]
        )
    )
    (tmp_path / "accounts.json").write_text(
        json.dumps(
            [
                {"account_id": 10, "customer_id": 1, "branch": "B1", "number": "001"},
                {"account_id": 11, "customer_id": 2, "branch": "B2", "number": "001"},
            ]
        )
    )
    report = run_checks(graph, tmp_path)
    assert report.passed, report.failures


def test_missing_file_fails(graph, tmp_path):
    write_csv(tmp_path / "customers.csv", GOOD_CUSTOMERS)
    report = run_checks(graph, tmp_path)
    assert not report.passed
    assert any(r.check == "file_present" and r.table == "accounts" for r in report.failures)


def test_duplicate_unique_fails(graph, tmp_path):
    write_csv(
        tmp_path / "customers.csv",
        [
            {"customer_id": "1", "segment": "MASS"},
            {"customer_id": "1", "segment": "MASS"},
        ],
    )
    write_csv(tmp_path / "accounts.csv", GOOD_ACCOUNTS)
    report = run_checks(graph, tmp_path)
    assert any(r.check == "unique" and not r.passed for r in report.results)


def test_composite_unique_violation(graph, tmp_path):
    write_csv(tmp_path / "customers.csv", GOOD_CUSTOMERS)
    write_csv(
        tmp_path / "accounts.csv",
        [
            {"account_id": "10", "customer_id": "1", "branch": "B1", "number": "001"},
            {"account_id": "11", "customer_id": "2", "branch": "B1", "number": "001"},
        ],
    )
    report = run_checks(graph, tmp_path)
    assert any(r.check == "unique_together" and not r.passed for r in report.results)


def test_orphaned_fk_fails(graph, tmp_path):
    write_csv(tmp_path / "customers.csv", GOOD_CUSTOMERS)
    write_csv(
        tmp_path / "accounts.csv",
        [
            {"account_id": "10", "customer_id": "99", "branch": "B1", "number": "001"},
            {"account_id": "11", "customer_id": "2", "branch": "B1", "number": "002"},
        ],
    )
    report = run_checks(graph, tmp_path)
    [fk_fail] = [r for r in report.failures if r.check == "fk_integrity"]
    assert "1 orphaned" in fk_fail.detail


def test_null_in_non_nullable_fails(graph, tmp_path):
    write_csv(
        tmp_path / "customers.csv",
        [
            {"customer_id": "1", "segment": ""},
            {"customer_id": "2", "segment": "MASS"},
        ],
    )
    write_csv(tmp_path / "accounts.csv", GOOD_ACCOUNTS)
    report = run_checks(graph, tmp_path)
    assert any(r.check == "not_null" and not r.passed for r in report.results)


def test_enum_violation_surfaces_as_fidelity(graph, tmp_path):
    write_csv(
        tmp_path / "customers.csv",
        [
            {"customer_id": "1", "segment": "PLATINUM"},
            {"customer_id": "2", "segment": "MASS"},
        ],
    )
    write_csv(tmp_path / "accounts.csv", GOOD_ACCOUNTS)
    report = run_checks(graph, tmp_path)
    assert any(r.check == "fidelity_enum_membership" for r in report.failures)


def test_self_referencing_fk_checked(tmp_path):
    employees = TableSchema(
        table_name="employees",
        fields=[
            Field(name="employee_id", type="string", unique=True),
            Field(name="manager_id", type="string", nullable=True, fk_ref="employees.employee_id"),
        ],
    )
    graph = SchemaGraph([employees])
    write_csv(
        tmp_path / "employees.csv",
        [
            {"employee_id": "E1", "manager_id": ""},
            {"employee_id": "E2", "manager_id": "E1"},
            {"employee_id": "E3", "manager_id": "E9"},  # orphan
        ],
    )
    report = run_checks(graph, tmp_path)
    [fk_fail] = [r for r in report.failures if r.check == "fk_integrity"]
    assert "1 orphaned" in fk_fail.detail
