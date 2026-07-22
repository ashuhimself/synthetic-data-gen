"""End-to-end pipeline test using a fake `copilot` binary.

The fake binary emits a fixed, fenced Python generator script — exactly what
the real Copilot CLI is prompted to produce — so the whole pipeline
(plan → codegen → save script → execute subprocess → harness) runs offline.
This also enforces C-2 structurally: the pipeline only ever executes the
saved script; it never treats model output as data.
"""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest
import yaml

from datagen_extractor.cli_bridge import CopilotBridge
from datagen_extractor.pipeline import run_pipeline

# The script the "model" authors — the new library-driven style: wire up the
# GenerationExecutor from the validated YAML, add a domain override.
GENERATED_SCRIPT = textwrap.dedent("""\
    import argparse

    from datagen_core.generators import GenerationExecutor
    from datagen_extractor.graph import SchemaGraph

    SEGMENTS = ["MASS", "AFFLUENT"]

    def main():
        ap = argparse.ArgumentParser()
        ap.add_argument("--schema-dir", required=True)
        ap.add_argument("--out-dir", required=True)
        ap.add_argument("--rows", type=int, default=50)
        ap.add_argument("--format", choices=["csv", "json"], default="csv")
        ap.add_argument("--seed", type=int, default=0)
        args = ap.parse_args()

        graph = SchemaGraph.from_directory(args.schema_dir)
        executor = GenerationExecutor(graph, seed=args.seed)
        executor.register_override(
            "customers", "segment", lambda rng, row, idx: rng.choice(SEGMENTS)
        )
        data = executor.generate(base_rows=args.rows)
        executor.write(data, args.out_dir, args.format)

    if __name__ == "__main__":
        main()
""")


@pytest.fixture
def fake_copilot(tmp_path: Path) -> str:
    """An executable that mimics `copilot -p ... -s` output."""
    script_file = tmp_path / "canned_response.py"
    script_file.write_text(GENERATED_SCRIPT)
    binary = tmp_path / "copilot"
    binary.write_text(f"#!/bin/sh\necho '```python'\ncat '{script_file}'\necho '```'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    d = tmp_path / "schemas"
    d.mkdir()
    (d / "customers.yaml").write_text(
        yaml.dump(
            {
                "table_name": "customers",
                "fields": [
                    {"name": "customer_id", "type": "integer", "unique": True},
                    {"name": "segment", "type": "enum", "distribution": "enum:MASS,AFFLUENT"},
                ],
            }
        )
    )
    (d / "accounts.yaml").write_text(
        yaml.dump(
            {
                "table_name": "accounts",
                "fields": [
                    {"name": "account_id", "type": "integer", "unique": True},
                    {
                        "name": "customer_id",
                        "type": "integer",
                        "fk_ref": "customers.customer_id",
                        "cardinality": "range:1-3",
                    },
                ],
            }
        )
    )
    return d


def test_full_pipeline_end_to_end(fake_copilot, schema_dir, tmp_path):
    bridge = CopilotBridge(timeout=30, copilot_binary=fake_copilot)
    out = tmp_path / "run"
    result = run_pipeline(schema_dir, out, bridge, rows=20, fmt="csv")

    # Script was saved as a reviewable artifact (C-2).
    assert result.script_path.exists()
    assert "argparse" in result.script_path.read_text()

    # Data files exist and all integrity/fidelity checks pass.
    assert (result.data_dir / "customers.csv").exists()
    assert (result.data_dir / "accounts.csv").exists()
    assert result.harness_report.passed, result.harness_report.failures


def test_manual_intercept_stop_after_code(fake_copilot, schema_dir, tmp_path):
    bridge = CopilotBridge(timeout=30, copilot_binary=fake_copilot)
    out = tmp_path / "run"
    result = run_pipeline(schema_dir, out, bridge, stop_after="code")

    assert result.stopped_after == "code"
    assert result.script_path.exists()
    # Nothing executed, no data produced.
    assert result.data_dir is None
    assert not (out / "data").exists()


def test_manual_intercept_stop_after_plan(fake_copilot, schema_dir, tmp_path):
    bridge = CopilotBridge(timeout=30, copilot_binary=fake_copilot)
    result = run_pipeline(schema_dir, tmp_path / "run", bridge, stop_after="plan")
    assert result.stopped_after == "plan"
    assert result.script_path is None


def test_deterministic_with_seed(fake_copilot, schema_dir, tmp_path):
    bridge = CopilotBridge(timeout=30, copilot_binary=fake_copilot)
    r1 = run_pipeline(schema_dir, tmp_path / "a", bridge, rows=10, seed=42)
    r2 = run_pipeline(schema_dir, tmp_path / "b", bridge, rows=10, seed=42)
    assert (r1.data_dir / "accounts.csv").read_text() == (r2.data_dir / "accounts.csv").read_text()
