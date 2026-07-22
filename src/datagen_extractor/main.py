"""Typer CLI entry point for the DataGen Extractor.

Commands
--------
extract      Extract a single Confluence markdown file → validated YAML.
validate     Re-validate an existing YAML schema file.
extract-all  Batch-extract all .md files in a directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from datagen_extractor.cli_bridge import CopilotBridge, CopilotCLIError
from datagen_extractor.codegen import CodegenError
from datagen_extractor.extractor import ExtractionExhaustedError, Extractor
from datagen_extractor.graph import SchemaGraph, SchemaGraphError
from datagen_extractor.harness import run_checks
from datagen_extractor.pii import load_patterns, scan_schemas
from datagen_extractor.pipeline import ExecutionError, execute_script, run_pipeline
from datagen_extractor.validate import validate_directory, validate_file

app = typer.Typer(
    name="datagen-extractor",
    help="Extract field-level schema from Confluence data contracts into validated YAML.",
    add_completion=False,
)
console = Console()

# Shared options

TIMEOUT_OPTION = typer.Option(120, "--timeout", help="Copilot CLI subprocess timeout in seconds.")
OUTPUT_OPTION = typer.Option("output", "--output", help="Directory for extracted YAML files.")
BINARY_OPTION = typer.Option(
    "copilot", "--copilot-binary", help="Path/name of the Copilot CLI binary."
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# extract


@app.command()
def extract(
    source: Path = typer.Argument(..., help="Path to a Confluence markdown file."),
    output: Path = OUTPUT_OPTION,
    timeout: int = TIMEOUT_OPTION,
    copilot_binary: str = BINARY_OPTION,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Extract a single Confluence file → validated YAML."""
    _setup_logging(verbose)

    if not source.exists():
        console.print(f"[red]Error:[/red] File not found: {source}")
        raise typer.Exit(code=1)

    try:
        bridge = CopilotBridge(timeout=timeout, copilot_binary=copilot_binary)
    except CopilotCLIError as exc:
        console.print(f"[red]Preflight failed:[/red] {exc}")
        raise typer.Exit(code=1)

    extractor = Extractor(bridge=bridge, output_dir=output)

    with console.status(f"[bold green]Extracting[/bold green] {source.name}..."):
        try:
            schemas = extractor.extract_file(source)
        except ExtractionExhaustedError as exc:
            _report_extraction_failure(exc, output)
            raise typer.Exit(code=1)
        except CopilotCLIError as exc:
            console.print(f"[red]Copilot CLI error:[/red] {exc}")
            raise typer.Exit(code=1)

    # Display results.
    for schema in schemas:
        _display_schema(schema)
    console.print(
        f"\n[green]✓[/green] {len(schemas)} table(s) written to [bold]{output}/[/bold]: "
        + ", ".join(f"{s.table_name}.yaml" for s in schemas)
    )


# validate


@app.command("validate")
def validate_cmd(
    path: Path = typer.Argument(..., help="Path to a YAML schema file or directory."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-validate an existing YAML schema file or directory."""
    _setup_logging(verbose)

    if path.is_dir():
        results = validate_directory(path)
    elif path.is_file():
        results = [validate_file(path)]
    else:
        console.print(f"[red]Error:[/red] Path not found: {path}")
        raise typer.Exit(code=1)

    if not results:
        console.print("[yellow]No .yaml files found.[/yellow]")
        raise typer.Exit(code=0)

    # Display results table.
    table = Table(title="Validation Results")
    table.add_column("File", style="cyan")
    table.add_column("Status")
    table.add_column("Fields", justify="right")
    table.add_column("Error")

    any_failed = False
    for r in results:
        if r.valid:
            table.add_row(
                r.path.name,
                "[green]✓ PASS[/green]",
                str(len(r.schema.fields)) if r.schema else "—",
                "",
            )
        else:
            any_failed = True
            table.add_row(
                r.path.name,
                "[red]✗ FAIL[/red]",
                "—",
                r.error[:80] + "..." if r.error and len(r.error) > 80 else (r.error or ""),
            )

    console.print(table)

    if any_failed:
        raise typer.Exit(code=1)


# extract-all


@app.command("extract-all")
def extract_all(
    source_dir: Path = typer.Argument(..., help="Directory containing Confluence markdown files."),
    output: Path = OUTPUT_OPTION,
    timeout: int = TIMEOUT_OPTION,
    copilot_binary: str = BINARY_OPTION,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Batch-extract all .md files in a directory."""
    _setup_logging(verbose)

    if not source_dir.is_dir():
        console.print(f"[red]Error:[/red] Not a directory: {source_dir}")
        raise typer.Exit(code=1)

    try:
        bridge = CopilotBridge(timeout=timeout, copilot_binary=copilot_binary)
    except CopilotCLIError as exc:
        console.print(f"[red]Preflight failed:[/red] {exc}")
        raise typer.Exit(code=1)

    extractor = Extractor(bridge=bridge, output_dir=output)

    md_files = sorted(source_dir.glob("*.md"))
    if not md_files:
        console.print(f"[yellow]No .md files found in {source_dir}[/yellow]")
        raise typer.Exit(code=0)

    console.print(f"Found [bold]{len(md_files)}[/bold] markdown files in {source_dir}\n")

    schemas = []
    for md_file in md_files:
        with console.status(f"[bold green]Extracting[/bold green] {md_file.name}..."):
            try:
                extracted = extractor.extract_file(md_file)
                schemas.extend(extracted)
                for schema in extracted:
                    console.print(
                        f"  [green]✓[/green] {md_file.name} → {schema.table_name}.yaml ({len(schema.fields)} fields)"
                    )
            except ExtractionExhaustedError as exc:
                console.print(f"  [red]✗[/red] {md_file.name} — exhausted retries")
                _report_extraction_failure(exc, output)
                raise typer.Exit(code=1)
            except CopilotCLIError as exc:
                console.print(f"  [red]✗[/red] {md_file.name} — CLI error: {exc}")
                raise typer.Exit(code=1)

    console.print(f"\n[green]✓[/green] Extracted [bold]{len(schemas)}[/bold] schemas to {output}/")


# graph


@app.command("graph")
def graph_cmd(
    schema_dir: Path = typer.Argument(..., help="Directory of validated YAML schema files."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Build the FK dependency graph and show the generation order."""
    _setup_logging(verbose)

    if not schema_dir.is_dir():
        console.print(f"[red]Error:[/red] Not a directory: {schema_dir}")
        raise typer.Exit(code=1)

    try:
        graph = SchemaGraph.from_directory(schema_dir)
        plans = graph.generation_plan()
    except SchemaGraphError as exc:
        console.print(Panel(str(exc), title="[red]Graph Error[/red]", border_style="red"))
        raise typer.Exit(code=1)

    table = Table(title="Generation Order")
    table.add_column("#", justify="right")
    table.add_column("Table", style="cyan")
    table.add_column("Strategy")
    table.add_column("Depends on")
    table.add_column("Unique", justify="right")
    table.add_column("Composite", justify="right")

    for plan in plans:
        deps = graph.get_dependencies(plan.table_name)
        table.add_row(
            str(plan.order_index + 1),
            plan.table_name,
            plan.generation_strategy,
            ", ".join(deps) if deps else "—",
            str(len(plan.constraints.unique_columns)),
            str(len(plan.constraints.unique_together)),
        )

    console.print(table)

    edges = [
        e for plan in plans for e in plan.constraints.fk_edges_in + plan.constraints.self_ref_edges
    ]
    if edges:
        edge_table = Table(title="FK Edges")
        edge_table.add_column("Child", style="cyan")
        edge_table.add_column("Parent", style="green")
        edge_table.add_column("Cardinality")
        edge_table.add_column("Nullable")
        for e in edges:
            edge_table.add_row(
                f"{e.child_table}.{e.child_column}",
                f"{e.parent_table}.{e.parent_column}",
                e.cardinality or "—",
                "yes" if e.nullable else "no",
            )
        console.print(edge_table)


# pii-scan


@app.command("pii-scan")
def pii_scan_cmd(
    schema_dir: Path = typer.Argument(..., help="Directory of validated YAML schema files."),
    patterns: Path = typer.Option(None, "--patterns", help="Custom PII pattern YAML file."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scan schemas for PII columns; fail if untagged PII-like fields exist."""
    _setup_logging(verbose)
    try:
        graph = SchemaGraph.from_directory(schema_dir)
        pattern_set = load_patterns(patterns) if patterns else None
        report = scan_schemas(list(graph.schemas.values()), pattern_set)
    except (SchemaGraphError, ValueError) as exc:
        console.print(Panel(str(exc), title="[red]PII Scan Error[/red]", border_style="red"))
        raise typer.Exit(code=1)

    table = Table(title="PII Scan")
    table.add_column("Table", style="cyan")
    table.add_column("Column")
    table.add_column("Category")
    table.add_column("Status")
    for f in report.findings:
        table.add_row(
            f.table,
            f.column,
            f.category,
            "[green]tagged[/green]" if f.tagged else "[red]UNTAGGED[/red]",
        )
    console.print(table)

    if report.untagged:
        console.print(f"[red]✗[/red] {len(report.untagged)} PII-like fields missing pii: true")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] All {len(report.tagged)} PII fields tagged")


# generate (full pipeline)


@app.command("generate")
def generate_cmd(
    schema_dir: Path = typer.Argument(..., help="Directory of validated YAML schema files."),
    out: Path = typer.Option(
        Path("run_output"), "--out", help="Run output directory (script + data)."
    ),
    rows: int = typer.Option(100, "--rows", help="Base row count for root tables."),
    fmt: str = typer.Option("csv", "--format", help="Output format: csv, json, xml, or parquet."),
    seed: int = typer.Option(0, "--seed", help="Random seed passed to the generated script."),
    stop_after: str = typer.Option(
        None, "--stop-after", help="Manual intercept: 'plan' or 'code'."
    ),
    timeout: int = TIMEOUT_OPTION,
    exec_timeout: int = typer.Option(
        300, "--exec-timeout", help="Generated-script execution timeout (s)."
    ),
    copilot_binary: str = BINARY_OPTION,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the pipeline: plan → generate code → execute → integrity checks."""
    _setup_logging(verbose)

    if fmt not in ("csv", "json", "xml", "parquet"):
        console.print("[red]Error:[/red] --format must be csv, json, xml, or parquet")
        raise typer.Exit(code=1)
    if stop_after not in (None, "plan", "code"):
        console.print("[red]Error:[/red] --stop-after must be 'plan' or 'code'")
        raise typer.Exit(code=1)

    try:
        bridge = CopilotBridge(timeout=timeout, copilot_binary=copilot_binary)
    except CopilotCLIError as exc:
        console.print(f"[red]Preflight failed:[/red] {exc}")
        raise typer.Exit(code=1)

    try:
        with console.status("[bold green]Running pipeline...[/bold green]"):
            result = run_pipeline(
                schema_dir,
                out,
                bridge,
                rows=rows,
                fmt=fmt,
                seed=seed,
                stop_after=stop_after,
                exec_timeout=exec_timeout,
            )
    except (SchemaGraphError, CodegenError, ExecutionError, CopilotCLIError) as exc:
        console.print(Panel(str(exc), title="[red]Pipeline Failed[/red]", border_style="red"))
        raise typer.Exit(code=1)

    if result.pii_report.untagged:
        console.print(
            f"[yellow]⚠[/yellow] {len(result.pii_report.untagged)} untagged PII-like "
            f"fields (run pii-scan for details)"
        )

    if result.stopped_after == "plan":
        console.print("[green]✓[/green] Stopped after planning. Run 'graph' to inspect the order.")
        return
    if result.stopped_after == "code":
        console.print(
            f"[green]✓[/green] Generator script written to [bold]{result.script_path}[/bold]\n"
            f"Review it, then run: datagen-extractor execute {result.script_path} "
            f"--schema-dir {schema_dir} --out {out / 'data'}"
        )
        return

    _display_harness(result.harness_report)
    console.print(
        f"\n[green]✓[/green] Data in [bold]{result.data_dir}[/bold], "
        f"script in [bold]{result.script_path}[/bold]"
    )
    if not result.harness_report.passed:
        raise typer.Exit(code=1)


# execute (manual intercept: run a reviewed script)


@app.command("execute")
def execute_cmd(
    script: Path = typer.Argument(..., help="Path to the reviewed generator script."),
    schema_dir: Path = typer.Option(
        ..., "--schema-dir", help="Schema directory for post-run checks."
    ),
    out: Path = typer.Option(Path("run_output/data"), "--out", help="Data output directory."),
    rows: int = typer.Option(100, "--rows"),
    fmt: str = typer.Option("csv", "--format"),
    seed: int = typer.Option(0, "--seed"),
    exec_timeout: int = typer.Option(300, "--exec-timeout"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Execute a reviewed generator script, then run integrity checks."""
    _setup_logging(verbose)
    if not script.is_file():
        console.print(f"[red]Error:[/red] Script not found: {script}")
        raise typer.Exit(code=1)

    try:
        graph = SchemaGraph.from_directory(schema_dir)
        execute_script(
            script, out, rows=rows, fmt=fmt, seed=seed, timeout=exec_timeout,
            schema_dir=schema_dir,
        )
        report = run_checks(graph, out)
    except (SchemaGraphError, ExecutionError) as exc:
        console.print(Panel(str(exc), title="[red]Execution Failed[/red]", border_style="red"))
        raise typer.Exit(code=1)

    _display_harness(report)
    if not report.passed:
        raise typer.Exit(code=1)
    console.print(f"\n[green]✓[/green] Data written to [bold]{out}[/bold]")


# check (harness only)


@app.command("check")
def check_cmd(
    schema_dir: Path = typer.Argument(..., help="Directory of validated YAML schema files."),
    data_dir: Path = typer.Argument(..., help="Directory of generated data files."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run integrity + fidelity checks on existing generated data."""
    _setup_logging(verbose)
    try:
        graph = SchemaGraph.from_directory(schema_dir)
        report = run_checks(graph, data_dir)
    except SchemaGraphError as exc:
        console.print(Panel(str(exc), title="[red]Check Failed[/red]", border_style="red"))
        raise typer.Exit(code=1)

    _display_harness(report)
    if not report.passed:
        raise typer.Exit(code=1)


# Helpers


RAW_OUTPUT_PREVIEW_CHARS = 3000


def _report_extraction_failure(exc: ExtractionExhaustedError, output_dir: Path) -> None:
    """Show why extraction failed AND the raw Copilot CLI output, for debugging.

    Every attempt's full raw response is saved to <output_dir>/_failed/ so
    nothing is lost even if the terminal truncates the preview.
    """
    console.print(
        Panel(str(exc), title="[red]Extraction Failed[/red]", border_style="red")
    )

    debug_dir = Path(output_dir) / "_failed"
    paths = exc.write_debug_files(debug_dir)

    raw = exc.last_raw_response
    if raw.strip():
        preview = raw[:RAW_OUTPUT_PREVIEW_CHARS]
        truncated = len(raw) > RAW_OUTPUT_PREVIEW_CHARS
        console.print(
            Panel(
                preview + ("\n… (truncated)" if truncated else ""),
                title=f"[yellow]Raw Copilot CLI output — attempt {exc.attempts[-1].attempt}[/yellow]",
                border_style="yellow",
            )
        )
    else:
        console.print("[yellow]Copilot CLI returned no output on the final attempt.[/yellow]")

    console.print(
        f"[dim]Full output from all {len(exc.attempts)} attempt(s) saved to: "
        + ", ".join(str(p) for p in paths)
        + "[/dim]"
    )


def _display_harness(report) -> None:
    """Pretty-print a HarnessReport to the console."""
    table = Table(title="Integrity & Fidelity Checks")
    table.add_column("Table", style="cyan")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for r in report.results:
        table.add_row(
            r.table,
            r.check,
            "[green]✓[/green]" if r.passed else "[red]✗[/red]",
            r.detail,
        )
    console.print(table)
    if report.passed:
        console.print(f"[green]✓[/green] All {len(report.results)} checks passed")
    else:
        console.print(f"[red]✗[/red] {len(report.failures)} of {len(report.results)} checks failed")


def _display_schema(schema) -> None:
    """Pretty-print a TableSchema to the console."""
    data = schema.model_dump(mode="json", exclude_none=True)
    yaml_str = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    console.print(
        Panel(
            Syntax(yaml_str, "yaml", theme="monokai"),
            title=f"[bold]{schema.table_name}[/bold] — {len(schema.fields)} fields",
            border_style="green",
        )
    )


# Entry point

if __name__ == "__main__":
    app()
