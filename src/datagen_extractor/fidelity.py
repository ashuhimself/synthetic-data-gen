"""Statistical fidelity module — distribution spec parsing and data checks.

Parses the ``distribution`` strings declared in YAML schemas into structured
specs, and checks generated data against them:

- enum membership (values ⊆ declared set),
- enum frequency tolerance when weights are declared,
- numeric / length / date bounds from ``min`` / ``max``.

Supported distribution strings (all declared in YAML — C-3):
    "uniform"
    "normal"                      or "normal:mean=100,std=15"
    "enum:A,B,C"                  (unweighted)
    "enum:A@0.6,B@0.3,C@0.1"      (weighted — weights must sum to ~1.0)
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from datagen_extractor.schema import Field

logger = logging.getLogger(__name__)

# Frequency checks only kick in with a meaningful sample.
MIN_ROWS_FOR_FREQUENCY_CHECK = 100
DEFAULT_FREQUENCY_TOLERANCE = 0.10

NUMERIC_TYPES = {"integer", "decimal"}
LENGTH_BOUND_TYPES = {"string", "text"}
ORDERED_STRING_TYPES = {"date", "datetime", "timestamp"}  # ISO strings compare lexicographically


class DistributionParseError(ValueError):
    """Raised when a distribution string cannot be parsed."""


@dataclass
class DistributionSpec:
    """Structured form of a YAML distribution declaration."""

    kind: str  # "uniform" | "normal" | "enum"
    values: list[str] = field(default_factory=list)
    weights: list[float] | None = None
    params: dict[str, float] = field(default_factory=dict)


def parse_distribution(raw: str) -> DistributionSpec:
    """Parse a distribution string from a YAML schema field."""
    raw = raw.strip()
    head, sep, rest = raw.partition(":")
    head = head.lower()

    if head == "enum":
        if not rest:
            raise DistributionParseError(f"enum distribution has no values: '{raw}'")
        values, weights = [], []
        weighted = "@" in rest
        for token in rest.split(","):
            token = token.strip()
            if weighted:
                value, _, w = token.rpartition("@")
                if not value:
                    raise DistributionParseError(f"bad weighted enum token '{token}' in '{raw}'")
                values.append(value)
                try:
                    weights.append(float(w))
                except ValueError as exc:
                    raise DistributionParseError(f"bad weight '{w}' in '{raw}'") from exc
            else:
                values.append(token)
        if weighted and abs(sum(weights) - 1.0) > 0.01:
            raise DistributionParseError(f"enum weights must sum to 1.0 in '{raw}'")
        return DistributionSpec(kind="enum", values=values, weights=weights if weighted else None)

    if head in ("uniform", "normal"):
        params: dict[str, float] = {}
        if rest:
            for token in rest.split(","):
                key, _, value = token.partition("=")
                try:
                    params[key.strip()] = float(value)
                except ValueError as exc:
                    raise DistributionParseError(f"bad parameter '{token}' in '{raw}'") from exc
        return DistributionSpec(kind=head, params=params)

    raise DistributionParseError(f"unknown distribution kind '{head}' in '{raw}'")


@dataclass
class FidelityViolation:
    """One fidelity check failure for a column."""

    column: str
    check: str
    detail: str


def check_column(
    fld: Field,
    values: list[str | None],
    tolerance: float = DEFAULT_FREQUENCY_TOLERANCE,
) -> list[FidelityViolation]:
    """Check one column's generated values against its declared shape.

    ``values`` are raw strings (CSV) or JSON scalars stringified by the
    caller; ``None`` entries are nulls and are skipped (nullability is the
    integrity harness's job, not fidelity's).
    """
    violations: list[FidelityViolation] = []
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return violations

    # Distribution checks.
    if fld.distribution:
        try:
            spec = parse_distribution(fld.distribution)
        except DistributionParseError as exc:
            return [FidelityViolation(fld.name, "distribution_parse", str(exc))]

        if spec.kind == "enum":
            allowed = set(spec.values)
            bad = sorted({v for v in present if v not in allowed})
            if bad:
                violations.append(
                    FidelityViolation(
                        fld.name,
                        "enum_membership",
                        f"values outside declared set: {bad[:5]}",
                    )
                )
            elif spec.weights and len(present) >= MIN_ROWS_FOR_FREQUENCY_CHECK:
                counts = Counter(present)
                total = len(present)
                for value, expected in zip(spec.values, spec.weights):
                    observed = counts.get(value, 0) / total
                    if abs(observed - expected) > tolerance:
                        violations.append(
                            FidelityViolation(
                                fld.name,
                                "enum_frequency",
                                f"'{value}' observed {observed:.2f} vs declared {expected:.2f} "
                                f"(tolerance {tolerance})",
                            )
                        )

    # Bounds checks from min/max.
    if fld.min is not None or fld.max is not None:
        violations.extend(_check_bounds(fld, present))

    return violations


def _check_bounds(fld: Field, present: list[str]) -> list[FidelityViolation]:
    violations: list[FidelityViolation] = []

    if fld.type in NUMERIC_TYPES:
        try:
            nums = [float(v) for v in present]
        except ValueError:
            return [
                FidelityViolation(fld.name, "numeric_parse", "non-numeric value in numeric column")
            ]
        if fld.min is not None and min(nums) < float(fld.min):
            violations.append(
                FidelityViolation(fld.name, "min_bound", f"min {min(nums)} < declared {fld.min}")
            )
        if fld.max is not None and max(nums) > float(fld.max):
            violations.append(
                FidelityViolation(fld.name, "max_bound", f"max {max(nums)} > declared {fld.max}")
            )

    elif fld.type in LENGTH_BOUND_TYPES:
        lengths = [len(v) for v in present]
        if fld.min is not None and min(lengths) < int(fld.min):
            violations.append(
                FidelityViolation(
                    fld.name, "min_length", f"shortest {min(lengths)} < declared {fld.min}"
                )
            )
        if fld.max is not None and max(lengths) > int(fld.max):
            violations.append(
                FidelityViolation(
                    fld.name, "max_length", f"longest {max(lengths)} > declared {fld.max}"
                )
            )

    elif fld.type in ORDERED_STRING_TYPES:
        if fld.min is not None and min(present) < str(fld.min):
            violations.append(
                FidelityViolation(
                    fld.name, "min_bound", f"earliest {min(present)} < declared {fld.min}"
                )
            )
        if fld.max is not None and max(present) > str(fld.max):
            violations.append(
                FidelityViolation(
                    fld.name, "max_bound", f"latest {max(present)} > declared {fld.max}"
                )
            )

    return violations
