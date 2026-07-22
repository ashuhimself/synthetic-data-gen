"""Referential integrity engine — FK dependency graph and generation planning.

Builds a directed graph (parent → child) from the ``fk_ref`` declarations in
validated ``TableSchema`` documents and produces:

- a topological generation order (Kahn's algorithm, deterministic),
- per-table constraint sets (uniqueness, composite uniqueness, FK edges,
  cardinality ratios) for the code-generation stage to embed,
- special-case handling for self-referencing FKs (root rows first, then
  batches referencing previously generated rows).

This module never produces data values (C-2) and contains no table- or
column-specific logic — everything is driven by the YAML schemas (C-3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from datagen_extractor.schema import TableSchema
from datagen_extractor.validate import validate_directory

logger = logging.getLogger(__name__)


# Exceptions


class SchemaGraphError(Exception):
    """Base error for schema graph construction and queries."""


class CycleError(SchemaGraphError):
    """Raised when FK dependencies form a cycle (self-references excluded)."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__("Circular FK dependency detected: " + " → ".join(cycle))


class UnknownTableError(SchemaGraphError):
    """Raised when an fk_ref targets a table absent from the schema set."""


# Data classes


@dataclass(frozen=True)
class FKEdge:
    """A single FK relationship: child_table.child_column → parent_table.parent_column."""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    cardinality: str | None = None
    nullable: bool = False


@dataclass
class ConstraintSet:
    """Constraints a table's generated rows must honor, resolved from YAML."""

    unique_columns: list[str] = field(default_factory=list)
    unique_together: list[list[str]] = field(default_factory=list)
    fk_edges_in: list[FKEdge] = field(default_factory=list)
    self_ref_edges: list[FKEdge] = field(default_factory=list)


@dataclass
class TablePlan:
    """One table's slot in the generation plan, in dependency order."""

    table_name: str
    order_index: int
    constraints: ConstraintSet
    self_referencing: bool
    generation_strategy: str  # "single_pass" | "root_then_batches"


# Graph


class SchemaGraph:
    """FK dependency graph over a set of validated table schemas.

    Edges point parent → child: a child table carrying an ``fk_ref`` to a
    parent depends on the parent being generated first.  Self-referencing
    FKs are tracked separately and excluded from the ordering edge set.
    """

    def __init__(self, schemas: list[TableSchema]) -> None:
        self.schemas: dict[str, TableSchema] = {s.table_name: s for s in schemas}
        # parent → set of children; children → set of parents
        self._children: dict[str, set[str]] = {t: set() for t in self.schemas}
        self._parents: dict[str, set[str]] = {t: set() for t in self.schemas}
        # per-child-table FK edges (self-refs kept separate)
        self._fk_edges_in: dict[str, list[FKEdge]] = {t: [] for t in self.schemas}
        self._self_ref_edges: dict[str, list[FKEdge]] = {t: [] for t in self.schemas}

        missing_refs: list[str] = []

        for schema in schemas:
            for f in schema.fields:
                if not f.fk_ref:
                    continue
                parent_table, _, parent_column = f.fk_ref.rpartition(".")
                if not parent_table:
                    raise SchemaGraphError(
                        f"Malformed fk_ref '{f.fk_ref}' on "
                        f"{schema.table_name}.{f.name} — expected 'table.column'"
                    )
                edge = FKEdge(
                    child_table=schema.table_name,
                    child_column=f.name,
                    parent_table=parent_table,
                    parent_column=parent_column,
                    cardinality=f.cardinality,
                    nullable=f.nullable,
                )
                if parent_table == schema.table_name:
                    self._self_ref_edges[schema.table_name].append(edge)
                    continue
                if parent_table not in self.schemas:
                    missing_refs.append(f"{schema.table_name}.{f.name} → {f.fk_ref}")
                    continue
                self._fk_edges_in[schema.table_name].append(edge)
                self._children[parent_table].add(schema.table_name)
                self._parents[schema.table_name].add(parent_table)

        if missing_refs:
            raise UnknownTableError(
                "fk_ref targets not found in schema set: " + "; ".join(sorted(missing_refs))
            )

        logger.info(
            "SchemaGraph built: %d tables, %d FK edges, %d self-referencing",
            len(self.schemas),
            sum(len(e) for e in self._fk_edges_in.values()),
            sum(1 for e in self._self_ref_edges.values() if e),
        )

    # construction helpers

    @classmethod
    def from_directory(cls, path: Path) -> "SchemaGraph":
        """Load every .yaml in a directory via the standard validation path."""
        results = validate_directory(Path(path))
        if not results:
            raise SchemaGraphError(f"No .yaml schema files found in '{path}'")
        failures = [r for r in results if not r.valid]
        if failures:
            detail = "; ".join(f"{r.path.name}: {r.error}" for r in failures)
            raise SchemaGraphError(f"Schema validation failed: {detail}")
        return cls([r.schema for r in results])

    # queries -------------

    def _require_table(self, table: str) -> None:
        if table not in self.schemas:
            raise UnknownTableError(
                f"Unknown table '{table}' — known tables: {sorted(self.schemas)}"
            )

    def get_dependencies(self, table: str) -> list[str]:
        """Direct parent tables this table FK-depends on (self excluded)."""
        self._require_table(table)
        return sorted(self._parents[table])

    def get_dependents(self, table: str) -> list[str]:
        """Direct child tables that FK-depend on this table (self excluded)."""
        self._require_table(table)
        return sorted(self._children[table])

    def topological_order(self) -> list[str]:
        """Generation order via Kahn's algorithm; ties broken alphabetically.

        Raises
        ------
        CycleError
            If the FK graph (self-references excluded) contains a cycle.
        """
        in_degree = {t: len(self._parents[t]) for t in self.schemas}
        ready = sorted(t for t, d in in_degree.items() if d == 0)
        order: list[str] = []

        while ready:
            table = ready.pop(0)
            order.append(table)
            newly_ready = []
            for child in self._children[table]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    newly_ready.append(child)
            if newly_ready:
                ready = sorted(ready + newly_ready)

        if len(order) < len(self.schemas):
            remaining = {t for t in self.schemas if t not in set(order)}
            raise CycleError(self._find_cycle(remaining))

        return order

    def _find_cycle(self, remaining: set[str]) -> list[str]:
        """Walk parent links within the leftover subgraph to name one cycle."""
        start = sorted(remaining)[0]
        path = [start]
        seen = {start}
        current = start
        while True:
            current = sorted(p for p in self._parents[current] if p in remaining)[0]
            if current in seen:
                return path[path.index(current) :] + [current]
            path.append(current)
            seen.add(current)

    def generation_plan(self) -> list[TablePlan]:
        """Tables in generation order, each with its resolved constraint set."""
        plans: list[TablePlan] = []
        for idx, table in enumerate(self.topological_order()):
            schema = self.schemas[table]
            self_refs = self._self_ref_edges[table]
            constraints = ConstraintSet(
                unique_columns=[f.name for f in schema.fields if f.unique],
                unique_together=[list(g) for g in (schema.unique_together or [])],
                fk_edges_in=list(self._fk_edges_in[table]),
                self_ref_edges=list(self_refs),
            )
            plans.append(
                TablePlan(
                    table_name=table,
                    order_index=idx,
                    constraints=constraints,
                    self_referencing=bool(self_refs),
                    generation_strategy=("root_then_batches" if self_refs else "single_pass"),
                )
            )
        return plans
