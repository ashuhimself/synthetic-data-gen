"""Tests for the referential integrity engine (SchemaGraph)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from datagen_extractor.graph import (
    CycleError,
    SchemaGraph,
    SchemaGraphError,
    UnknownTableError,
)
from datagen_extractor.schema import Field, TableSchema

# Helpers


def make_field(name: str, **kwargs) -> Field:
    return Field(name=name, type=kwargs.pop("type", "string"), **kwargs)


def make_table(name: str, fields: list[Field], **kwargs) -> TableSchema:
    return TableSchema(table_name=name, fields=fields, **kwargs)


@pytest.fixture
def banking_schemas() -> list[TableSchema]:
    """3-table chain with a diamond: transactions → accounts → customers,
    plus transactions → customers directly."""
    customers = make_table(
        "customers",
        [
            make_field("customer_id", type="integer", unique=True),
            make_field("email", unique=True),
        ],
    )
    accounts = make_table(
        "accounts",
        [
            make_field("account_id", type="integer", unique=True),
            make_field(
                "customer_id",
                type="integer",
                fk_ref="customers.customer_id",
                cardinality="avg:12",
            ),
            make_field("branch_code"),
            make_field("account_number"),
        ],
        unique_together=[["branch_code", "account_number"]],
    )
    transactions = make_table(
        "transactions",
        [
            make_field("txn_id", type="uuid", unique=True),
            make_field("account_id", type="integer", fk_ref="accounts.account_id"),
            make_field(
                "customer_id",
                type="integer",
                fk_ref="customers.customer_id",
                cardinality="N:1",
            ),
        ],
    )
    return [customers, accounts, transactions]


# 3-table chain


class TestThreeTableChain:
    def test_topological_order_respects_edges(self, banking_schemas):
        order = SchemaGraph(banking_schemas).topological_order()
        assert order.index("customers") < order.index("accounts")
        assert order.index("accounts") < order.index("transactions")
        assert order.index("customers") < order.index("transactions")

    def test_get_dependencies(self, banking_schemas):
        graph = SchemaGraph(banking_schemas)
        assert graph.get_dependencies("customers") == []
        assert graph.get_dependencies("accounts") == ["customers"]
        assert graph.get_dependencies("transactions") == ["accounts", "customers"]

    def test_get_dependents(self, banking_schemas):
        graph = SchemaGraph(banking_schemas)
        assert graph.get_dependents("customers") == ["accounts", "transactions"]
        assert graph.get_dependents("transactions") == []

    def test_deterministic_tie_breaking(self):
        # Three independent tables — order must be alphabetical.
        schemas = [
            make_table(n, [make_field("id", unique=True)]) for n in ("zebra", "apple", "mango")
        ]
        assert SchemaGraph(schemas).topological_order() == ["apple", "mango", "zebra"]

    def test_generation_plan_constraints(self, banking_schemas):
        plans = {p.table_name: p for p in SchemaGraph(banking_schemas).generation_plan()}

        customers = plans["customers"]
        assert customers.order_index == 0
        assert customers.constraints.unique_columns == ["customer_id", "email"]
        assert customers.generation_strategy == "single_pass"
        assert not customers.self_referencing

        accounts = plans["accounts"]
        assert accounts.constraints.unique_together == [["branch_code", "account_number"]]
        [fk] = accounts.constraints.fk_edges_in
        assert fk.parent_table == "customers"
        assert fk.parent_column == "customer_id"
        assert fk.cardinality == "avg:12"

        txns = plans["transactions"]
        assert len(txns.constraints.fk_edges_in) == 2
        assert txns.order_index == 2


# Self-referencing FK


class TestSelfReferencingFK:
    @pytest.fixture
    def employees(self) -> TableSchema:
        return make_table(
            "employees",
            [
                make_field("employee_id", unique=True),
                make_field(
                    "manager_id",
                    fk_ref="employees.employee_id",
                    nullable=True,
                ),
            ],
        )

    def test_no_cycle_error(self, employees):
        assert SchemaGraph([employees]).topological_order() == ["employees"]

    def test_plan_marks_self_referencing(self, employees):
        [plan] = SchemaGraph([employees]).generation_plan()
        assert plan.self_referencing
        assert plan.generation_strategy == "root_then_batches"
        assert plan.constraints.fk_edges_in == []
        [edge] = plan.constraints.self_ref_edges
        assert edge.child_column == "manager_id"
        assert edge.parent_column == "employee_id"
        assert edge.nullable is True

    def test_self_ref_excluded_from_dependencies(self, employees):
        graph = SchemaGraph([employees])
        assert graph.get_dependencies("employees") == []
        assert graph.get_dependents("employees") == []

    def test_self_ref_alongside_normal_fk(self, employees):
        departments = make_table("departments", [make_field("dept_id", unique=True)])
        employees_with_dept = make_table(
            "employees",
            employees.fields + [make_field("dept_id", fk_ref="departments.dept_id")],
        )
        graph = SchemaGraph([departments, employees_with_dept])
        assert graph.topological_order() == ["departments", "employees"]
        plan = {p.table_name: p for p in graph.generation_plan()}["employees"]
        assert plan.self_referencing
        assert len(plan.constraints.fk_edges_in) == 1


# Cycle detection


class TestCycleDetection:
    def test_three_table_cycle(self):
        schemas = [
            make_table("a", [make_field("id", unique=True), make_field("b_id", fk_ref="b.id")]),
            make_table("b", [make_field("id", unique=True), make_field("c_id", fk_ref="c.id")]),
            make_table("c", [make_field("id", unique=True), make_field("a_id", fk_ref="a.id")]),
        ]
        graph = SchemaGraph(schemas)
        with pytest.raises(CycleError) as exc_info:
            graph.topological_order()
        for name in ("a", "b", "c"):
            assert name in str(exc_info.value)

    def test_two_table_mutual_fk(self):
        schemas = [
            make_table("x", [make_field("id", unique=True), make_field("y_id", fk_ref="y.id")]),
            make_table("y", [make_field("id", unique=True), make_field("x_id", fk_ref="x.id")]),
        ]
        with pytest.raises(CycleError):
            SchemaGraph(schemas).topological_order()

    def test_generation_plan_also_raises(self):
        schemas = [
            make_table("x", [make_field("id"), make_field("y_id", fk_ref="y.id")]),
            make_table("y", [make_field("id"), make_field("x_id", fk_ref="x.id")]),
        ]
        with pytest.raises(CycleError):
            SchemaGraph(schemas).generation_plan()


# Error cases


class TestErrorCases:
    def test_unknown_fk_target(self):
        schemas = [
            make_table("orders", [make_field("id"), make_field("cust_id", fk_ref="ghosts.id")]),
        ]
        with pytest.raises(UnknownTableError) as exc_info:
            SchemaGraph(schemas)
        assert "ghosts" in str(exc_info.value)

    def test_malformed_fk_ref(self):
        schemas = [make_table("orders", [make_field("cust_id", fk_ref="nodothere")])]
        with pytest.raises(SchemaGraphError, match="Malformed fk_ref"):
            SchemaGraph(schemas)

    def test_unknown_table_query(self, banking_schemas):
        graph = SchemaGraph(banking_schemas)
        with pytest.raises(UnknownTableError):
            graph.get_dependencies("nonexistent")

    def test_from_directory_invalid_yaml(self, tmp_path: Path):
        (tmp_path / "bad.yaml").write_text("table_name: broken\nfields: []\n")
        with pytest.raises(SchemaGraphError, match="bad.yaml"):
            SchemaGraph.from_directory(tmp_path)

    def test_from_directory_empty(self, tmp_path: Path):
        with pytest.raises(SchemaGraphError, match="No .yaml"):
            SchemaGraph.from_directory(tmp_path)

    def test_from_directory_valid(self, tmp_path: Path, banking_schemas):
        for schema in banking_schemas:
            data = schema.model_dump(mode="json", exclude_none=True)
            (tmp_path / f"{schema.table_name}.yaml").write_text(yaml.dump(data))
        graph = SchemaGraph.from_directory(tmp_path)
        assert graph.topological_order() == ["customers", "accounts", "transactions"]


# Schema extensions


class TestSchemaExtensions:
    @pytest.mark.parametrize("value", ["N:1", "1:1", "avg:12", "range:1-40"])
    def test_valid_cardinality(self, value):
        assert make_field("f", cardinality=value).cardinality == value

    @pytest.mark.parametrize("value", ["1:N", "avg:", "range:1", "lots", "N:M"])
    def test_invalid_cardinality(self, value):
        with pytest.raises(ValidationError):
            make_field("f", cardinality=value)

    def test_unique_together_unknown_column(self):
        with pytest.raises(ValidationError, match="unknown columns"):
            make_table(
                "t",
                [make_field("a"), make_field("b")],
                unique_together=[["a", "missing"]],
            )

    def test_unique_together_single_column_group(self):
        with pytest.raises(ValidationError, match="at least 2"):
            make_table(
                "t",
                [make_field("a"), make_field("b")],
                unique_together=[["a"]],
            )
