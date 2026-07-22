"""Validation harness — post-generation integrity and fidelity checks.

Given a SchemaGraph and a directory of generated data files (one file per
table, CSV or JSON), verifies:

- file presence per table,
- per-column uniqueness (distinct among non-null values),
- composite uniqueness (unique_together),
- non-null columns contain no nulls,
- declared null_rate is respected within tolerance,
- charset: unicode columns actually contain non-ASCII values,
- control_char_rate columns embed control characters at the declared rate,
- cross-field rules (when/then null constraints) hold on every row,
- FK referential integrity (child values ⊆ parent values; NULL allowed
  when the FK column is nullable), including self-referencing FKs,
- statistical fidelity (distribution membership/frequency, min/max bounds)
  via the fidelity module.

Pure checking — never mutates or generates data.
"""

from __future__ import annotations

import csv
import json
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from datagen_extractor.fidelity import check_column
from datagen_extractor.graph import SchemaGraph

logger = logging.getLogger(__name__)

# null_rate / control_char_rate are statistical — only checked with a
# meaningful sample.
MIN_ROWS_FOR_NULL_RATE_CHECK = 100
NULL_RATE_TOLERANCE = 0.10
MIN_ROWS_FOR_CHARSET_CHECK = 30
MIN_ROWS_FOR_CONTROL_CHAR_CHECK = 100
CONTROL_CHAR_RATE_TOLERANCE = 0.10


def _has_control_char(value: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def _has_non_ascii(value: str) -> bool:
    return any(ord(ch) > 127 for ch in value)


@dataclass
class CheckResult:
    """Outcome of one named check on one table."""

    table: str
    check: str
    passed: bool
    detail: str = ""


@dataclass
class HarnessReport:
    """All check results for a run."""

    results: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]


def _load_rows(path: Path) -> list[dict]:
    """Load a table file into a list of row dicts with string/None values."""
    if path.suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            return [
                {k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(fh)
            ]
    if path.suffix == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [{k: (None if v is None else str(v)) for k, v in row.items()} for row in rows]
    raise ValueError(f"Unsupported data file format: {path.suffix}")


def _find_table_file(data_dir: Path, table: str) -> Path | None:
    for ext in (".csv", ".json"):
        candidate = data_dir / f"{table}{ext}"
        if candidate.exists():
            return candidate
    return None


def run_checks(graph: SchemaGraph, data_dir: Path) -> HarnessReport:
    """Run all integrity + fidelity checks over a generated data directory."""
    data_dir = Path(data_dir)
    results: list[CheckResult] = []
    tables: dict[str, list[dict]] = {}

    # Load phase — file presence.
    for table in graph.schemas:
        path = _find_table_file(data_dir, table)
        if path is None:
            results.append(
                CheckResult(table, "file_present", False, f"no {table}.csv/.json in {data_dir}")
            )
            continue
        try:
            tables[table] = _load_rows(path)
            results.append(CheckResult(table, "file_present", True, f"{len(tables[table])} rows"))
        except (ValueError, json.JSONDecodeError, csv.Error) as exc:
            results.append(CheckResult(table, "file_present", False, f"load error: {exc}"))

    plans = {p.table_name: p for p in graph.generation_plan()}

    for table, rows in tables.items():
        schema = graph.schemas[table]
        plan = plans[table]

        columns = {f.name for f in schema.fields}
        if rows:
            missing_cols = columns - set(rows[0].keys())
            if missing_cols:
                results.append(
                    CheckResult(
                        table, "columns_present", False, f"missing columns: {sorted(missing_cols)}"
                    )
                )
                continue
            results.append(CheckResult(table, "columns_present", True))

        # Non-null columns.
        for f in schema.fields:
            if f.nullable:
                continue
            null_count = sum(1 for r in rows if r.get(f.name) is None)
            if null_count:
                results.append(
                    CheckResult(
                        table,
                        "not_null",
                        False,
                        f"{f.name}: {null_count} NULLs in non-nullable column",
                    )
                )

        # Declared null_rate within tolerance.
        for f in schema.fields:
            if f.null_rate is None or len(rows) < MIN_ROWS_FOR_NULL_RATE_CHECK:
                continue
            observed = sum(1 for r in rows if r.get(f.name) is None) / len(rows)
            if abs(observed - f.null_rate) > NULL_RATE_TOLERANCE:
                results.append(
                    CheckResult(
                        table,
                        "null_rate",
                        False,
                        f"{f.name}: observed null rate {observed:.2f} vs declared "
                        f"{f.null_rate:.2f} (tolerance {NULL_RATE_TOLERANCE})",
                    )
                )
            else:
                results.append(CheckResult(table, "null_rate", True, f.name))

        # Text profile: charset and control-character injection.
        for f in schema.fields:
            values = [r.get(f.name) for r in rows if r.get(f.name) is not None]

            if f.charset == "unicode" and len(values) >= MIN_ROWS_FOR_CHARSET_CHECK:
                non_ascii = sum(1 for v in values if _has_non_ascii(v))
                if non_ascii == 0:
                    results.append(
                        CheckResult(
                            table,
                            "charset",
                            False,
                            f"{f.name}: declared charset unicode but all "
                            f"{len(values)} values are pure ASCII",
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            table,
                            "charset",
                            True,
                            f"{f.name}: {non_ascii}/{len(values)} non-ASCII values",
                        )
                    )
            elif f.charset == "ascii" and values:
                offenders = sum(1 for v in values if _has_non_ascii(v))
                if offenders:
                    results.append(
                        CheckResult(
                            table,
                            "charset",
                            False,
                            f"{f.name}: declared charset ascii but {offenders} "
                            f"values contain non-ASCII characters",
                        )
                    )
                else:
                    results.append(CheckResult(table, "charset", True, f.name))

            if f.control_char_rate is not None and len(values) >= MIN_ROWS_FOR_CONTROL_CHAR_CHECK:
                observed = sum(1 for v in values if _has_control_char(v)) / len(values)
                if abs(observed - f.control_char_rate) > CONTROL_CHAR_RATE_TOLERANCE:
                    results.append(
                        CheckResult(
                            table,
                            "control_char_rate",
                            False,
                            f"{f.name}: observed control-char rate {observed:.2f} vs "
                            f"declared {f.control_char_rate:.2f} "
                            f"(tolerance {CONTROL_CHAR_RATE_TOLERANCE})",
                        )
                    )
                else:
                    results.append(CheckResult(table, "control_char_rate", True, f.name))

        # Cross-field rules: rows matching `when` must satisfy `then`.
        for rule in schema.rules or []:
            violations = 0
            matched = 0
            for r in rows:
                if all(r.get(col) == val for col, val in rule.when.items()):
                    matched += 1
                    for col, effect in rule.then.items():
                        is_null = r.get(col) is None
                        if (effect == "not_null" and is_null) or (effect == "null" and not is_null):
                            violations += 1
            label = f"when {rule.when} then {rule.then}"
            if violations:
                results.append(
                    CheckResult(
                        table,
                        "rule",
                        False,
                        f"{label}: {violations} violations in {matched} matching rows",
                    )
                )
            else:
                results.append(CheckResult(table, "rule", True, f"{label} ({matched} rows)"))

        # Single-column uniqueness (among non-null values).
        for col in plan.constraints.unique_columns:
            values = [r[col] for r in rows if r.get(col) is not None]
            dupes = len(values) - len(set(values))
            if dupes:
                results.append(
                    CheckResult(table, "unique", False, f"{col}: {dupes} duplicate values")
                )
            else:
                results.append(CheckResult(table, "unique", True, col))

        # Composite uniqueness.
        for group in plan.constraints.unique_together:
            combos = [tuple(r.get(c) for c in group) for r in rows]
            dupes = len(combos) - len(set(combos))
            if dupes:
                results.append(
                    CheckResult(
                        table, "unique_together", False, f"{group}: {dupes} duplicate combinations"
                    )
                )
            else:
                results.append(CheckResult(table, "unique_together", True, str(group)))

        # FK integrity (cross-table and self-referencing).
        for edge in plan.constraints.fk_edges_in + plan.constraints.self_ref_edges:
            parent_rows = tables.get(edge.parent_table)
            if parent_rows is None:
                results.append(
                    CheckResult(
                        table,
                        "fk_integrity",
                        False,
                        f"{edge.child_column}: parent table '{edge.parent_table}' has no data file",
                    )
                )
                continue
            parent_values = {
                r[edge.parent_column] for r in parent_rows if r.get(edge.parent_column) is not None
            }
            orphans = 0
            null_violations = 0
            for r in rows:
                v = r.get(edge.child_column)
                if v is None:
                    if not edge.nullable:
                        null_violations += 1
                    continue
                if v not in parent_values:
                    orphans += 1
            if orphans or null_violations:
                results.append(
                    CheckResult(
                        table,
                        "fk_integrity",
                        False,
                        f"{edge.child_column} → {edge.parent_table}.{edge.parent_column}: "
                        f"{orphans} orphaned values, {null_violations} NULLs in non-nullable FK",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        table,
                        "fk_integrity",
                        True,
                        f"{edge.child_column} → {edge.parent_table}.{edge.parent_column}",
                    )
                )

        # Statistical fidelity per column.
        for f in schema.fields:
            values = [r.get(f.name) for r in rows]
            for violation in check_column(f, values):
                results.append(
                    CheckResult(
                        table,
                        f"fidelity_{violation.check}",
                        False,
                        f"{violation.column}: {violation.detail}",
                    )
                )

    report = HarnessReport(results=results)
    logger.info("Harness: %d checks, %d failures", len(report.results), len(report.failures))
    return report
