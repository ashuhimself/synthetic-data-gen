"""Tests for the datagen_core.generators library."""

from __future__ import annotations

import random
import unicodedata

import pytest

from datagen_core.generators import (
    GenerationExecutor,
    child_counts,
    generate_value,
    resolve_fk,
    sample_distribution,
)
from datagen_extractor.graph import SchemaGraph
from datagen_extractor.harness import run_checks
from datagen_extractor.schema import Field, TableSchema


@pytest.fixture
def rng():
    return random.Random(42)


class TestSamplers:
    def test_enum_unweighted(self, rng):
        for _ in range(20):
            assert sample_distribution("enum:A,B,C", rng) in {"A", "B", "C"}

    def test_enum_weighted_skews(self, rng):
        values = [sample_distribution("enum:A@0.9,B@0.1", rng) for _ in range(500)]
        assert values.count("A") > 400

    def test_normal_clamped(self, rng):
        for _ in range(200):
            v = sample_distribution("normal:mean=50,std=30", rng, lo=0, hi=100)
            assert 0 <= v <= 100

    def test_uniform_bounds(self, rng):
        for _ in range(100):
            assert 10 <= sample_distribution("uniform", rng, lo=10, hi=20) <= 20


class TestFKResolution:
    def test_one_to_one(self, rng):
        assert child_counts(5, "1:1", rng) == [1, 1, 1, 1, 1]

    def test_range(self, rng):
        counts = child_counts(100, "range:2-4", rng)
        assert all(2 <= c <= 4 for c in counts)

    def test_avg_close_to_declared(self, rng):
        counts = child_counts(500, "avg:6", rng)
        assert all(c >= 0 for c in counts)
        assert 5 <= sum(counts) / len(counts) <= 7

    def test_resolve_fk_membership(self, rng):
        parents = [1, 2, 3]
        for key in resolve_fk(parents, "range:1-3", rng):
            assert key in parents

    def test_resolve_fk_n_rows_mode(self, rng):
        out = resolve_fk(["a", "b"], None, rng, n_rows=10)
        assert len(out) == 10 and set(out) <= {"a", "b"}

    def test_resolve_fk_empty_parents(self, rng):
        assert resolve_fk([], "avg:3", rng) == []


class TestTypedGenerators:
    def test_unique_integer_sequential(self, rng):
        f = Field(name="id", type="integer", unique=True)
        assert [generate_value(f, rng, i) for i in range(5)] == [1, 2, 3, 4, 5]

    def test_uuid_format(self, rng):
        f = Field(name="txn_id", type="uuid")
        v = generate_value(f, rng, 0)
        assert len(v) == 36 and v.count("-") == 4

    def test_date_within_bounds(self, rng):
        f = Field(name="opened", type="date", min="2020-01-01", max="2020-12-31")
        for i in range(50):
            assert "2020-01-01" <= generate_value(f, rng, i) <= "2020-12-31"

    def test_decimal_bounds_and_rounding(self, rng):
        f = Field(name="amount", type="decimal", min=10, max=20)
        for i in range(50):
            v = generate_value(f, rng, i)
            assert 10 <= v <= 20 and round(v, 2) == v

    def test_enum_from_distribution(self, rng):
        f = Field(name="status", type="enum", distribution="enum:open,closed")
        assert generate_value(f, rng, 0) in {"open", "closed"}

    def test_null_rate_applied(self, rng):
        f = Field(name="close_date", type="date", nullable=True, null_rate=0.5)
        nulls = sum(1 for i in range(400) if generate_value(f, rng, i) is None)
        assert 140 <= nulls <= 260

    def test_pii_email_synthetic(self, rng):
        f = Field(name="email", type="string", pii=True, unique=True)
        assert generate_value(f, rng, 7).endswith("@example.com")

    def test_pii_ssn_invalid_range(self, rng):
        f = Field(name="ssn", type="string", pii=True)
        assert generate_value(f, rng, 0).startswith("9")

    def test_charset_unicode_share(self, rng):
        f = Field(name="full_name", type="string", pii=True, charset="unicode")
        values = [generate_value(f, rng, i) for i in range(200)]
        assert any(any(ord(ch) > 127 for ch in v) for v in values)

    def test_control_char_rate(self, rng):
        f = Field(name="memo", type="text", control_char_rate=0.3)
        values = [generate_value(f, rng, i) for i in range(300)]
        with_cc = sum(
            1 for v in values if any(unicodedata.category(ch) == "Cc" for ch in v)
        )
        assert 60 <= with_cc <= 120

    def test_string_length_bounds(self, rng):
        f = Field(name="code", type="string", min=5, max=8)
        for i in range(50):
            assert 5 <= len(generate_value(f, rng, i)) <= 8


class TestExecutor:
    @pytest.fixture
    def graph(self) -> SchemaGraph:
        customers = TableSchema(
            table_name="customers",
            fields=[
                Field(name="customer_id", type="integer", unique=True),
                Field(name="full_name", type="string", pii=True, charset="unicode"),
                Field(name="email", type="string", unique=True, pii=True),
                Field(name="memo", type="text", control_char_rate=0.15),
            ],
        )
        accounts = TableSchema(
            table_name="accounts",
            fields=[
                Field(name="account_id", type="integer", unique=True),
                Field(name="customer_id", type="integer",
                      fk_ref="customers.customer_id", cardinality="avg:3"),
                Field(name="branch_code", type="string"),
                Field(name="account_number", type="string"),
                Field(name="account_type", type="enum",
                      distribution="enum:credit_card@0.6,personal_loan@0.25,home_loan@0.15"),
                Field(name="status", type="enum", distribution="enum:open@0.8,closed@0.2"),
                Field(name="close_date", type="date", nullable=True, null_rate=0.8),
            ],
            unique_together=[["branch_code", "account_number"]],
            rules=[
                {"when": {"status": "closed"}, "then": {"close_date": "not_null"}},
                {"when": {"status": "open"}, "then": {"close_date": "null"}},
            ],
        )
        employees = TableSchema(
            table_name="employees",
            fields=[
                Field(name="employee_id", type="string", unique=True),
                Field(name="manager_id", type="string", nullable=True,
                      fk_ref="employees.employee_id"),
            ],
        )
        return SchemaGraph([customers, accounts, employees])

    def test_output_passes_full_harness(self, graph, tmp_path):
        executor = GenerationExecutor(graph, seed=7)
        data = executor.generate(base_rows=150)
        executor.write(data, tmp_path, "csv")
        report = run_checks(graph, tmp_path)
        assert report.passed, report.failures

    def test_deterministic_by_seed(self, graph, tmp_path):
        d1 = GenerationExecutor(graph, seed=5).generate(base_rows=30)
        d2 = GenerationExecutor(graph, seed=5).generate(base_rows=30)
        assert d1 == d2
        d3 = GenerationExecutor(graph, seed=6).generate(base_rows=30)
        assert d1 != d3

    def test_override_wins(self, graph):
        executor = GenerationExecutor(graph, seed=1)
        executor.register_override(
            "accounts", "branch_code", lambda rng, row, idx: f"BR-{idx % 5}"
        )
        data = executor.generate(base_rows=20)
        assert all(r["branch_code"].startswith("BR-") for r in data["accounts"])

    def test_self_ref_roots_and_children(self, graph):
        data = GenerationExecutor(graph, seed=3).generate(base_rows=50)
        employees = data["employees"]
        ids = {r["employee_id"] for r in employees}
        roots = [r for r in employees if r["manager_id"] is None]
        children = [r for r in employees if r["manager_id"] is not None]
        assert roots and children
        assert all(r["manager_id"] in ids for r in children)

    def test_rules_hold_by_construction(self, graph):
        data = GenerationExecutor(graph, seed=9).generate(base_rows=100)
        for r in data["accounts"]:
            if r["status"] == "closed":
                assert r["close_date"] is not None
            else:
                assert r["close_date"] is None

    def test_json_output(self, graph, tmp_path):
        executor = GenerationExecutor(graph, seed=2)
        executor.write(executor.generate(base_rows=40), tmp_path, "json")
        report = run_checks(graph, tmp_path)
        assert report.passed, report.failures
