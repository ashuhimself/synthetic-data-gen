"""Tests for the statistical fidelity module."""

from __future__ import annotations

import pytest

from datagen_extractor.fidelity import (
    DistributionParseError,
    check_column,
    parse_distribution,
)
from datagen_extractor.schema import Field


class TestParseDistribution:
    def test_plain_enum(self):
        spec = parse_distribution("enum:A,B,C")
        assert spec.kind == "enum"
        assert spec.values == ["A", "B", "C"]
        assert spec.weights is None

    def test_weighted_enum(self):
        spec = parse_distribution("enum:A@0.6,B@0.3,C@0.1")
        assert spec.values == ["A", "B", "C"]
        assert spec.weights == [0.6, 0.3, 0.1]

    def test_weighted_enum_bad_sum(self):
        with pytest.raises(DistributionParseError, match="sum to 1.0"):
            parse_distribution("enum:A@0.5,B@0.1")

    def test_uniform_and_normal(self):
        assert parse_distribution("uniform").kind == "uniform"
        spec = parse_distribution("normal:mean=100,std=15")
        assert spec.kind == "normal"
        assert spec.params == {"mean": 100.0, "std": 15.0}

    @pytest.mark.parametrize("bad", ["enum:", "zipfian", "normal:mean=abc"])
    def test_bad_strings_rejected(self, bad):
        with pytest.raises(DistributionParseError):
            parse_distribution(bad)


class TestCheckColumn:
    def test_enum_membership_violation(self):
        f = Field(name="status", type="enum", distribution="enum:OPEN,CLOSED")
        violations = check_column(f, ["OPEN", "CLOSED", "FROZEN"])
        assert [v.check for v in violations] == ["enum_membership"]
        assert "FROZEN" in violations[0].detail

    def test_enum_frequency_within_tolerance_passes(self):
        f = Field(name="seg", type="enum", distribution="enum:A@0.7,B@0.3")
        values = ["A"] * 68 + ["B"] * 32 + ["A"] * 0
        # Below the 100-row threshold no check; pad to 100 rows exactly.
        assert check_column(f, values) == []

    def test_enum_frequency_violation(self):
        f = Field(name="seg", type="enum", distribution="enum:A@0.9,B@0.1")
        values = ["A"] * 50 + ["B"] * 50
        violations = check_column(f, values)
        assert any(v.check == "enum_frequency" for v in violations)

    def test_numeric_bounds(self):
        f = Field(name="amount", type="decimal", min=0, max=100)
        assert check_column(f, ["5", "99.5"]) == []
        violations = check_column(f, ["-1", "150"])
        assert {v.check for v in violations} == {"min_bound", "max_bound"}

    def test_string_length_bounds(self):
        f = Field(name="code", type="string", min=2, max=4)
        assert check_column(f, ["ab", "abcd"]) == []
        violations = check_column(f, ["a", "abcdef"])
        assert {v.check for v in violations} == {"min_length", "max_length"}

    def test_date_bounds_lexicographic(self):
        f = Field(name="opened", type="date", min="2000-01-01", max="2026-12-31")
        assert check_column(f, ["2010-06-15"]) == []
        violations = check_column(f, ["1999-12-31", "2030-01-01"])
        assert {v.check for v in violations} == {"min_bound", "max_bound"}

    def test_nulls_are_skipped(self):
        f = Field(name="status", type="enum", distribution="enum:A,B", nullable=True)
        assert check_column(f, [None, "", "A"]) == []

    def test_unparseable_distribution_reported_not_raised(self):
        f = Field(name="x", type="string", distribution="zipfian:oops")
        violations = check_column(f, ["v"])
        assert violations[0].check == "distribution_parse"
