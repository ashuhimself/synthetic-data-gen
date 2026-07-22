"""Tests for the four output formats: csv, json, xml, parquet (§8)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from datagen_core.generators import GenerationExecutor
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
            Field(name="closed_on", type="date", nullable=True, null_rate=0.5),
        ],
    )
    accounts = TableSchema(
        table_name="accounts",
        fields=[
            Field(name="account_id", type="integer", unique=True),
            Field(
                name="customer_id",
                type="integer",
                fk_ref="customers.customer_id",
                cardinality="range:1-3",
            ),
        ],
    )
    return SchemaGraph([customers, accounts])


@pytest.mark.parametrize("fmt", ["csv", "json", "xml", "parquet"])
def test_roundtrip_passes_harness(graph, tmp_path, fmt):
    if fmt == "parquet":
        pytest.importorskip("pyarrow")
    executor = GenerationExecutor(graph, seed=11)
    executor.write(executor.generate(base_rows=120), tmp_path, fmt)

    assert (tmp_path / f"customers.{fmt}").exists()
    assert (tmp_path / f"accounts.{fmt}").exists()
    report = run_checks(graph, tmp_path)
    assert report.passed, report.failures


def test_xml_structure_and_null_handling(graph, tmp_path):
    executor = GenerationExecutor(graph, seed=11)
    data = executor.generate(base_rows=50)
    executor.write(data, tmp_path, "xml")

    root = ET.parse(tmp_path / "customers.xml").getroot()
    assert root.tag == "customers"
    rows = root.findall("row")
    assert len(rows) == 50
    # Null closed_on → element omitted; non-null → present.
    n_nulls = sum(1 for r in data["customers"] if r["closed_on"] is None)
    n_missing = sum(1 for r in rows if r.find("closed_on") is None)
    assert n_missing == n_nulls > 0


def test_parquet_preserves_nulls_and_types(graph, tmp_path):
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    executor = GenerationExecutor(graph, seed=11)
    data = executor.generate(base_rows=50)
    executor.write(data, tmp_path, "parquet")

    table = pq.read_table(tmp_path / "customers.parquet")
    assert table.num_rows == 50
    assert pa.types.is_integer(table.schema.field("customer_id").type)
    n_nulls = sum(1 for r in data["customers"] if r["closed_on"] is None)
    assert table.column("closed_on").null_count == n_nulls


def test_unknown_format_rejected(graph, tmp_path):
    executor = GenerationExecutor(graph, seed=1)
    with pytest.raises(ValueError, match="Unsupported format"):
        executor.write(executor.generate(base_rows=5), tmp_path, "avro")
