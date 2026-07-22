"""Tests for multi-table extraction and the strict-format retry loop."""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest
import yaml

from datagen_extractor.cli_bridge import CopilotBridge
from datagen_extractor.extractor import ExtractionExhaustedError, Extractor
from datagen_extractor.schema import SchemaBundle

GOOD_BUNDLE = textwrap.dedent("""\
    tables:
      - table_name: consumers
        row_count_hint: 100000
        fields:
          - name: consumer_id
            type: integer
            unique: true
          - name: first_name
            type: string
            pii: true
      - table_name: credit_accounts
        fields:
          - name: account_id
            type: integer
            unique: true
          - name: consumer_id
            type: integer
            fk_ref: consumers.consumer_id
            cardinality: range:1-8
""")

# Illegal: "primary_key" is not in the contract — strict mode must reject it.
BAD_BUNDLE = textwrap.dedent("""\
    tables:
      - table_name: consumers
        primary_key: consumer_id
        fields:
          - name: consumer_id
            type: integer
""")


def make_fake_copilot(tmp_path: Path, responses: list[str]) -> str:
    """A stateful fake `copilot`: returns responses[i] on the i-th call."""
    for i, body in enumerate(responses):
        (tmp_path / f"response_{i}.txt").write_text(f"```yaml\n{body}```\n")
    counter = tmp_path / "call_count"
    counter.write_text("0")
    binary = tmp_path / "copilot"
    binary.write_text(
        "#!/bin/sh\n"
        f"n=$(cat '{counter}')\n"
        f"echo $((n + 1)) > '{counter}'\n"
        f"cat '{tmp_path}/response_'$n'.txt'\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


@pytest.fixture
def source_doc(tmp_path: Path) -> Path:
    doc = tmp_path / "requirement.md"
    doc.write_text("Generate consumers and their credit accounts.")
    return doc


def test_multi_table_extraction(tmp_path, source_doc):
    fake = make_fake_copilot(tmp_path, [GOOD_BUNDLE])
    extractor = Extractor(
        bridge=CopilotBridge(timeout=30, copilot_binary=fake),
        output_dir=tmp_path / "out",
    )
    schemas = extractor.extract_file(source_doc)
    assert [s.table_name for s in schemas] == ["consumers", "credit_accounts"]
    assert schemas[0].row_count_hint == 100000
    assert (tmp_path / "out" / "consumers.yaml").exists()
    assert (tmp_path / "out" / "credit_accounts.yaml").exists()
    # Metadata injected per table.
    written = yaml.safe_load((tmp_path / "out" / "consumers.yaml").read_text())
    assert written["source"].endswith("requirement.md")


def test_extra_key_rejected_then_corrected_on_retry(tmp_path, source_doc):
    fake = make_fake_copilot(tmp_path, [BAD_BUNDLE, GOOD_BUNDLE])
    extractor = Extractor(
        bridge=CopilotBridge(timeout=30, copilot_binary=fake),
        output_dir=tmp_path / "out",
    )
    schemas = extractor.extract_file(source_doc)
    assert len(schemas) == 2  # second attempt succeeded


def test_persistent_bad_format_exhausts_retries(tmp_path, source_doc):
    fake = make_fake_copilot(tmp_path, [BAD_BUNDLE, BAD_BUNDLE, BAD_BUNDLE])
    extractor = Extractor(
        bridge=CopilotBridge(timeout=30, copilot_binary=fake),
        output_dir=tmp_path / "out",
    )
    with pytest.raises(ExtractionExhaustedError) as exc_info:
        extractor.extract_file(source_doc)
    assert "primary_key" in str(exc_info.value)


def test_legacy_single_table_document_still_accepted(tmp_path, source_doc):
    single = textwrap.dedent("""\
        table_name: customers
        fields:
          - name: customer_id
            type: integer
            unique: true
    """)
    fake = make_fake_copilot(tmp_path, [single])
    extractor = Extractor(
        bridge=CopilotBridge(timeout=30, copilot_binary=fake),
        output_dir=tmp_path / "out",
    )
    schemas = extractor.extract_file(source_doc)
    assert [s.table_name for s in schemas] == ["customers"]


def test_bundle_rejects_duplicate_table_names():
    with pytest.raises(Exception, match="duplicate table names"):
        SchemaBundle.model_validate(
            {
                "tables": [
                    {"table_name": "t", "fields": [{"name": "a", "type": "string"}]},
                    {"table_name": "t", "fields": [{"name": "b", "type": "string"}]},
                ]
            }
        )
