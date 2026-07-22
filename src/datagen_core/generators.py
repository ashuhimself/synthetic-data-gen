"""Reusable generation primitives + the topological generation executor.

Layers (all driven by validated YAML ``Field``/``TableSchema`` models — C-3):

- distribution samplers: ``sample_distribution`` (uniform / normal / enum,
  weighted or not),
- typed value generators: ``generate_value`` (uuid, date, datetime/timestamp,
  decimal, integer, enum, boolean, string/text — honoring unique, min/max,
  pii, charset, control_char_rate, null_rate),
- FK resolution: ``child_counts`` / ``resolve_fk`` for cardinality specs
  ``N:1``, ``1:1``, ``avg:N``, ``range:N-M``,
- ``GenerationExecutor``: walks a ``SchemaGraph``'s generation plan in
  topological order and produces per-table rows honoring every constraint
  (uniqueness, unique_together, rules, self-referencing FKs, text profiles).

AI-authored scripts (C-2) compose these primitives and may register
per-column overrides for domain realism; they never reimplement mechanics.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import random
import uuid as uuid_mod
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from datagen_extractor.fidelity import DistributionSpec, parse_distribution
from datagen_extractor.graph import FKEdge, SchemaGraph
from datagen_extractor.schema import Field

logger = logging.getLogger(__name__)

# Output formats per requirements §8 — files only, user-selected at run time.
OUTPUT_FORMATS: frozenset[str] = frozenset({"csv", "json", "xml", "parquet"})

DEFAULT_CHILDREN_PER_PARENT = 3
DEFAULT_DATE_MIN = dt.date(2000, 1, 1)
DEFAULT_DATE_MAX = dt.date(2026, 1, 1)
UNIQUE_TOGETHER_MAX_RETRIES = 25
SELF_REF_ROOT_FRACTION = 0.1

# Synthetic-only value pools (never real people; SSNs use the invalid 900
# range, phones the reserved 555 prefix, emails example.com).
_FIRST_ASCII = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Avery",
                "Quinn", "Drew", "Cameron", "Hayden", "Reese", "Dakota", "Rowan"]
_FIRST_UNICODE = ["José", "Müller", "Céline", "Søren", "Åsa", "Nadia", "Дмитрий",
                  "Ελένη", "美咲", "François", "Zoë", "Renée"]
_LAST_ASCII = ["Shaw", "Patel", "Nguyen", "Lopez", "Khan", "Reed", "Brooks",
               "Singh", "Kim", "Carter", "Diaz", "Bennett", "Ward", "Perry"]
_LAST_UNICODE = ["Fernández", "Müller", "Øberg", "Çelik", "Дубров", "Škoda",
                 "Nuñez", "Björk"]
_STREETS = ["Main St", "Oak Ave", "Cedar Ln", "Elm Dr", "Maple Ct", "Pine Rd"]
_CITIES = ["Springfield", "Riverton", "Fairview", "Lakeside", "Georgetown"]
_WORDS = ["ledger", "branch", "note", "review", "pending", "batch", "audit",
          "transfer", "manual", "system"]
_CONTROL_CHARS = ["\t", "\n", "\x1b"]


# Distribution samplers


def sample_distribution(
    spec: DistributionSpec | str,
    rng: random.Random,
    lo: float | None = None,
    hi: float | None = None,
) -> Any:
    """Sample one value from a parsed (or raw-string) distribution spec.

    enum → one of the declared values (weighted when weights present);
    normal → gauss(mean, std) clamped to [lo, hi] when given;
    uniform → uniform(lo, hi) (defaults 0..1).
    """
    if isinstance(spec, str):
        spec = parse_distribution(spec)
    if spec.kind == "enum":
        if spec.weights:
            return rng.choices(spec.values, weights=spec.weights, k=1)[0]
        return rng.choice(spec.values)
    if spec.kind == "normal":
        mean = spec.params.get("mean", (lo or 0) + ((hi or 100) - (lo or 0)) / 2)
        std = spec.params.get("std", max(1.0, mean / 10))
        value = rng.gauss(mean, std)
    else:  # uniform
        value = rng.uniform(lo if lo is not None else 0.0, hi if hi is not None else 1.0)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


# FK resolution


def child_counts(n_parents: int, cardinality: str | None, rng: random.Random) -> list[int]:
    """How many child rows each of ``n_parents`` parents gets.

    ``1:1`` → exactly one each; ``avg:N`` → gauss around N (≥0);
    ``range:N-M`` → randint in [N, M]; ``N:1`` / None → default average.
    """
    if cardinality == "1:1":
        return [1] * n_parents
    if cardinality and cardinality.startswith("avg:"):
        avg = int(cardinality.split(":", 1)[1])
        return [max(0, round(rng.gauss(avg, max(1.0, avg / 3)))) for _ in range(n_parents)]
    if cardinality and cardinality.startswith("range:"):
        lo, hi = (int(x) for x in cardinality.split(":", 1)[1].split("-"))
        return [rng.randint(lo, hi) for _ in range(n_parents)]
    return [DEFAULT_CHILDREN_PER_PARENT] * n_parents


def resolve_fk(
    parent_keys: list[Any],
    cardinality: str | None,
    rng: random.Random,
    n_rows: int | None = None,
) -> list[Any]:
    """Produce the FK column for a child table: one parent key per child row.

    With ``n_rows`` given (N:1 style), parents are sampled uniformly for
    exactly that many rows.  Otherwise the child row count is derived from
    ``child_counts`` and each parent key is repeated accordingly.
    """
    if not parent_keys:
        return []
    if n_rows is not None:
        return [rng.choice(parent_keys) for _ in range(n_rows)]
    assignments: list[Any] = []
    for key, count in zip(parent_keys, child_counts(len(parent_keys), cardinality, rng)):
        assignments.extend([key] * count)
    return assignments


# Typed value generators


def _random_date(rng: random.Random, lo: dt.date, hi: dt.date) -> dt.date:
    return lo + dt.timedelta(days=rng.randint(0, max(0, (hi - lo).days)))


def _date_bounds(field: Field) -> tuple[dt.date, dt.date]:
    lo = dt.date.fromisoformat(str(field.min)) if field.min else DEFAULT_DATE_MIN
    hi = dt.date.fromisoformat(str(field.max)) if field.max else DEFAULT_DATE_MAX
    return lo, hi


def _pii_value(field: Field, rng: random.Random, idx: int) -> str | None:
    """Synthetic value for a PII column, chosen by column-name convention."""
    name = field.name.lower()
    unicode_ok = field.charset == "unicode"
    first_pool = _FIRST_ASCII + (_FIRST_UNICODE if unicode_ok else [])
    last_pool = _LAST_ASCII + (_LAST_UNICODE if unicode_ok else [])
    if "first" in name and "name" in name:
        return rng.choice(first_pool)
    if ("last" in name or "sur" in name) and "name" in name:
        return rng.choice(last_pool)
    if "name" in name:
        return f"{rng.choice(first_pool)} {rng.choice(last_pool)}"
    if "email" in name or (field.format or "").lower() == "email":
        return f"user{idx + 1}@example.com"
    if "ssn" in name:
        return f"9{rng.randint(0, 99):02d}-{rng.randint(10, 99)}-{idx % 10000:04d}"
    if "phone" in name or "mobile" in name:
        return f"+1-555-{rng.randint(100, 999)}-{idx % 10000:04d}"
    if "birth" in name or name == "dob":
        return str(_random_date(rng, dt.date(1940, 1, 1), dt.date(2005, 12, 31)))
    if "address" in name or "street" in name:
        return f"{rng.randint(1, 9999)} {rng.choice(_STREETS)}"
    if "city" in name:
        return rng.choice(_CITIES)
    if "zip" in name or "postal" in name:
        return f"{rng.randint(10000, 99999)}"
    return None  # fall through to generic text generation


def generate_value(field: Field, rng: random.Random, idx: int) -> Any:
    """Generate one typed value for ``field`` at row index ``idx``.

    ``unique: true`` columns embed ``idx`` so uniqueness holds by
    construction.  null_rate / charset / control_char_rate are applied here;
    FK columns are the executor's job, not this function's.
    """
    if field.nullable and field.null_rate is not None and rng.random() < field.null_rate:
        return None

    value = _generate_base(field, rng, idx)

    if isinstance(value, str) and field.charset == "unicode" and not field.pii:
        # Guarantee a non-ASCII share even for non-PII text columns.
        if rng.random() < 0.4:
            value = f"{value} №{idx}"
    if isinstance(value, str) and field.control_char_rate is not None:
        if rng.random() < field.control_char_rate:
            mid = max(1, len(value) // 2)
            value = value[:mid] + rng.choice(_CONTROL_CHARS) + value[mid:]
    return value


def _generate_base(field: Field, rng: random.Random, idx: int) -> Any:
    ftype = field.type

    if ftype == "uuid":
        return str(uuid_mod.UUID(int=rng.getrandbits(128)))

    if ftype == "boolean":
        return rng.choice([True, False])

    if ftype == "enum":
        if field.distribution:
            return sample_distribution(field.distribution, rng)
        return f"VALUE_{rng.randint(1, 3)}"

    if ftype == "integer":
        if field.unique:
            return idx + 1
        lo = int(field.min) if field.min is not None else 0
        hi = int(field.max) if field.max is not None else 1_000_000
        if field.distribution:
            return int(round(sample_distribution(field.distribution, rng, lo, hi)))
        return rng.randint(lo, hi)

    if ftype == "decimal":
        lo = float(field.min) if field.min is not None else 0.0
        hi = float(field.max) if field.max is not None else 100_000.0
        if field.distribution:
            return round(float(sample_distribution(field.distribution, rng, lo, hi)), 2)
        return round(rng.uniform(lo, hi), 2)

    if ftype == "date":
        lo, hi = _date_bounds(field)
        return str(_random_date(rng, lo, hi))

    if ftype in ("datetime", "timestamp"):
        lo, hi = _date_bounds(field)
        day = _random_date(rng, lo, hi)
        moment = dt.datetime(day.year, day.month, day.day,
                             rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
                             tzinfo=dt.timezone.utc)
        return moment.isoformat()

    # string / text
    if field.pii:
        pii = _pii_value(field, rng, idx)
        if pii is not None:
            return pii
    if field.distribution:
        spec = parse_distribution(field.distribution)
        if spec.kind == "enum":
            return sample_distribution(spec, rng)
    if field.unique:
        return f"{field.name[:3].upper()}{idx + 1:08d}"
    lo = int(field.min) if field.min is not None else 4
    hi = int(field.max) if field.max is not None else 24
    words = []
    while sum(len(w) + 1 for w in words) < lo + 1:
        words.append(rng.choice(_WORDS))
    text = " ".join(words)
    while len(text) < lo:
        text += rng.choice("abcdefgh")
    return text[: max(lo, hi)]


# Executor


class GenerationExecutor:
    """Topological, constraint-honoring data generation over a SchemaGraph.

    Walks ``graph.generation_plan()`` in dependency order.  Per table:
    row count comes from the driver FK edge's cardinality (root tables use
    ``base_rows``); every column is filled via ``generate_value`` unless an
    override is registered; FK columns are resolved against already-generated
    parent keys; self-referencing FKs use root-then-batches; unique_together
    and rules are enforced by construction.

    AI-authored scripts customize semantics only via ``register_override``:

        executor.register_override("accounts", "lender_name",
                                   lambda rng, row, idx: rng.choice(LENDERS))
    """

    def __init__(self, graph: SchemaGraph, seed: int = 0) -> None:
        self.graph = graph
        self.rng = random.Random(seed)
        self._overrides: dict[tuple[str, str], Callable[[random.Random, dict, int], Any]] = {}

    def register_override(
        self, table: str, column: str, fn: Callable[[random.Random, dict, int], Any]
    ) -> None:
        """Register ``fn(rng, partial_row, idx) -> value`` for one column."""
        self._overrides[(table, column)] = fn

    # generation 

    def generate(self, base_rows: int = 100) -> dict[str, list[dict]]:
        """Generate all tables in topological order.  Returns rows per table."""
        data: dict[str, list[dict]] = {}
        for plan in self.graph.generation_plan():
            data[plan.table_name] = self._generate_table(plan.table_name, plan, data, base_rows)
            logger.info("generated %-24s %6d rows", plan.table_name, len(data[plan.table_name]))
        return data

    def _generate_table(self, table: str, plan, data: dict, base_rows: int) -> list[dict]:
        schema = self.graph.schemas[table]
        fields = {f.name: f for f in schema.fields}
        fk_edges = plan.constraints.fk_edges_in

        # Driver edge (prefers one with a cardinality declaration) sets the
        # row count; remaining FK columns are sampled N:1.
        driver: FKEdge | None = None
        for edge in fk_edges:
            if edge.cardinality:
                driver = edge
                break
        if driver is None and fk_edges:
            driver = fk_edges[0]

        if driver is not None:
            parent_keys = [
                r[driver.parent_column]
                for r in data[driver.parent_table]
                if r.get(driver.parent_column) is not None
            ]
            fk_assignments = resolve_fk(parent_keys, driver.cardinality, self.rng)
            n_rows = len(fk_assignments)
        else:
            fk_assignments = []
            n_rows = base_rows

        seen_together: dict[int, set[tuple]] = {
            i: set() for i, _ in enumerate(schema.unique_together or [])
        }

        rows: list[dict] = []
        for idx in range(n_rows):
            row: dict[str, Any] = {}
            for f in schema.fields:
                if driver is not None and f.name == driver.child_column:
                    row[f.name] = fk_assignments[idx]
                    continue
                edge = next((e for e in fk_edges if e.child_column == f.name), None)
                self_edge = next(
                    (e for e in plan.constraints.self_ref_edges if e.child_column == f.name), None
                )
                if self_edge is not None:
                    row[f.name] = None  # filled in the self-ref pass below
                    continue
                if edge is not None:
                    row[f.name] = self._sample_other_fk(edge, fields[f.name], data)
                    continue
                row[f.name] = self._value_for(table, f, row, idx)

            self._enforce_unique_together(table, schema, row, seen_together, idx)
            self._apply_rules(schema, row, idx)
            rows.append(row)

        self._fill_self_refs(plan, fields, rows)
        return rows

    def _value_for(self, table: str, field: Field, row: dict, idx: int) -> Any:
        """Override-aware value generation for one non-FK column."""
        override = self._overrides.get((table, field.name))
        if override is not None:
            return override(self.rng, row, idx)
        return generate_value(field, self.rng, idx)

    def _sample_other_fk(self, edge: FKEdge, field: Field, data: dict) -> Any:
        if field.nullable:
            rate = field.null_rate if field.null_rate is not None else 0.5
            if self.rng.random() < rate:
                return None
        parent_keys = [
            r[edge.parent_column]
            for r in data[edge.parent_table]
            if r.get(edge.parent_column) is not None
        ]
        return self.rng.choice(parent_keys) if parent_keys else None

    def _enforce_unique_together(
        self, table: str, schema, row: dict, seen: dict, idx: int
    ) -> None:
        for gi, group in enumerate(schema.unique_together or []):
            combo = tuple(row.get(c) for c in group)
            tries = 0
            while combo in seen[gi] and tries < UNIQUE_TOGETHER_MAX_RETRIES:
                fields = {f.name: f for f in schema.fields}
                for col in group:
                    if not fields[col].fk_ref:
                        row[col] = self._value_for(table, fields[col], row, idx)
                combo = tuple(row.get(c) for c in group)
                tries += 1
            if combo in seen[gi]:
                # Deterministic last resort: suffix the first non-FK column.
                fields = {f.name: f for f in schema.fields}
                for col in group:
                    if not fields[col].fk_ref and isinstance(row.get(col), str):
                        row[col] = f"{row[col]}-{idx}"
                        break
                combo = tuple(row.get(c) for c in group)
            seen[gi].add(combo)

    def _apply_rules(self, schema, row: dict, idx: int) -> None:
        fields = {f.name: f for f in schema.fields}
        for rule in schema.rules or []:
            if not all(str(row.get(c)) == v for c, v in rule.when.items()):
                continue
            for col, effect in rule.then.items():
                if effect == "null":
                    row[col] = None
                elif effect == "not_null" and row.get(col) is None:
                    f = fields[col]
                    # Generate ignoring null_rate so the value is present.
                    stripped = f.model_copy(update={"null_rate": None, "nullable": False})
                    row[col] = generate_value(stripped, self.rng, idx)

    def _fill_self_refs(self, plan, fields: dict[str, Field], rows: list[dict]) -> None:
        for edge in plan.constraints.self_ref_edges:
            n_roots = max(1, int(len(rows) * SELF_REF_ROOT_FRACTION))
            for i, row in enumerate(rows):
                if i < n_roots:
                    row[edge.child_column] = (
                        None if edge.nullable else row[edge.parent_column]
                    )
                else:
                    parent_row = rows[self.rng.randrange(0, i)]
                    row[edge.child_column] = parent_row[edge.parent_column]

    # output ----

    def write(self, data: dict[str, list[dict]], out_dir: Path, fmt: str = "csv") -> None:
        """Write one UTF-8 file per table: <table>.<fmt>.

        Formats (requirements §8): csv | json | xml | parquet.
        Parquet needs the optional ``pyarrow`` dependency
        (``pip install "datagen-extractor[parquet]"``).
        """
        if fmt not in OUTPUT_FORMATS:
            raise ValueError(f"Unsupported format '{fmt}' — choose one of {sorted(OUTPUT_FORMATS)}")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for table, rows in data.items():
            columns = [f.name for f in self.graph.schemas[table].fields]
            path = out_dir / f"{table}.{fmt}"
            if fmt == "csv":
                with path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=columns)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow(
                            {c: ("" if row.get(c) is None else row[c]) for c in columns}
                        )
            elif fmt == "json":
                path.write_text(
                    json.dumps([{c: row.get(c) for c in columns} for row in rows],
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            elif fmt == "xml":
                _write_xml(path, table, columns, rows)
            elif fmt == "parquet":
                _write_parquet(path, columns, rows)
            logger.info("wrote %s (%d rows)", path, len(rows))


def _write_xml(path: Path, table: str, columns: list[str], rows: list[dict]) -> None:
    """<table><row><col>value</col>…</row>…</table>; NULL columns are omitted."""
    root = ET.Element(table)
    for row in rows:
        row_el = ET.SubElement(root, "row")
        for col in columns:
            value = row.get(col)
            if value is None:
                continue
            ET.SubElement(row_el, col).text = str(value)
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _write_parquet(path: Path, columns: list[str], rows: list[dict]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ValueError(
            "Parquet output requires the optional 'pyarrow' dependency — "
            "install with: pip install 'datagen-extractor[parquet]'"
        ) from exc
    table = pa.table({c: [row.get(c) for row in rows] for c in columns})
    pq.write_table(table, path)
