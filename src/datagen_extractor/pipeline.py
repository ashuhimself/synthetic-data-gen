"""Pipeline orchestrator — Plan → Generate Code → Execute → Check.

Two execution modes per the requirements:

- automated one-shot: all stages run end-to-end (``stop_after=None``),
- manual intercept: ``stop_after="plan"`` or ``"code"`` halts the run at a
  checkpoint so the plan / generated script can be reviewed; execution of a
  reviewed script is then a separate call (``execute_script`` +
  ``check_output``).

C-2: the only thing that writes data files is the generated script, run as a
subprocess.  C-3: everything is driven by the YAML schema directory.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from datagen_extractor.cli_bridge import CopilotBridge
from datagen_extractor.codegen import generate_script
from datagen_extractor.graph import SchemaGraph
from datagen_extractor.harness import HarnessReport, run_checks
from datagen_extractor.pii import PIIReport, scan_schemas

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """Raised when the generated script fails at runtime."""


@dataclass
class PipelineResult:
    """What each stage produced; later fields are None if the run stopped early."""

    graph: SchemaGraph
    pii_report: PIIReport
    stopped_after: str | None = None
    script_path: Path | None = None
    data_dir: Path | None = None
    harness_report: HarnessReport | None = None
    executed_output: str = ""


def execute_script(
    script_path: Path,
    out_dir: Path,
    rows: int,
    fmt: str,
    seed: int = 0,
    timeout: int = 300,
    schema_dir: Path | None = None,
) -> str:
    """Run a (reviewed) generator script as a subprocess.  Returns its stdout.

    ``schema_dir`` is forwarded as ``--schema-dir`` so scripts drive the
    ``datagen_core.generators`` executor from the validated YAML (C-3).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(script_path),
        "--out-dir",
        str(out_dir),
        "--rows",
        str(rows),
        "--format",
        fmt,
        "--seed",
        str(seed),
    ]
    if schema_dir is not None:
        cmd += ["--schema-dir", str(schema_dir)]
    logger.info("Executing generator script: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionError(f"Generator script timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise ExecutionError(
            f"Generator script exited with code {result.returncode}.\n"
            f"stderr: {result.stderr.strip()[-2000:]}"
        )
    return result.stdout


def run_pipeline(
    schema_dir: Path,
    out_dir: Path,
    bridge: CopilotBridge,
    rows: int = 100,
    fmt: str = "csv",
    seed: int = 0,
    stop_after: str | None = None,  # None | "plan" | "code"
    script_path: Path | None = None,
    exec_timeout: int = 300,
) -> PipelineResult:
    """Run the pipeline from a validated schema directory.

    Raises SchemaGraphError / CodegenError / ExecutionError on stage failure.
    """
    graph = SchemaGraph.from_directory(Path(schema_dir))
    pii_report = scan_schemas(list(graph.schemas.values()))
    if pii_report.untagged:
        for f in pii_report.untagged:
            logger.warning(
                "PII risk: %s.%s looks like %s but is not tagged pii: true",
                f.table,
                f.column,
                f.category,
            )

    result = PipelineResult(graph=graph, pii_report=pii_report)

    if stop_after == "plan":
        result.stopped_after = "plan"
        return result

    script_path = script_path or Path(out_dir) / "generated" / "generate_data.py"
    result.script_path = generate_script(graph, bridge, script_path)

    if stop_after == "code":
        result.stopped_after = "code"
        return result

    data_dir = Path(out_dir) / "data"
    result.executed_output = execute_script(
        result.script_path,
        data_dir,
        rows=rows,
        fmt=fmt,
        seed=seed,
        timeout=exec_timeout,
        schema_dir=Path(schema_dir),
    )
    result.data_dir = data_dir
    result.harness_report = run_checks(graph, data_dir)
    return result
