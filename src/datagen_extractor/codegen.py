"""Code generation stage — Copilot CLI authors a generator script.

C-2 boundary: the LLM's output here is a *Python script*, saved to disk for
review and executed separately.  Model output is never parsed as data and
never written to the data output directory.  The script contract:

    python <script> --schema-dir DIR --out-dir DIR --rows N \
                    --format csv|json|xml|parquet --seed N

Scripts build on the internal ``datagen_core.generators`` library
(``GenerationExecutor`` + primitives): the library handles all generation
mechanics from the validated YAML; the authored script only wires it up and
registers per-column semantic overrides (domain vocabularies such as lender
or agency names).  This keeps per-run AI-authored logic minimal.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import yaml

from datagen_extractor.cli_bridge import CopilotBridge
from datagen_extractor.graph import SchemaGraph

logger = logging.getLogger(__name__)


CODEGEN_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a data-generation code author for a banking synthetic-data
    platform.  Write ONE Python 3 script that generates synthetic data for
    the schema set below by DRIVING THE INTERNAL LIBRARY — do NOT
    reimplement generation mechanics.

    THE LIBRARY (already installed, importable):

        from datagen_extractor.graph import SchemaGraph
        from datagen_core.generators import GenerationExecutor

        graph = SchemaGraph.from_directory(args.schema_dir)
        executor = GenerationExecutor(graph, seed=args.seed)
        # optional, for domain realism only:
        executor.register_override("accounts", "lender_name",
                                   lambda rng, row, idx: rng.choice(LENDERS))
        data = executor.generate(base_rows=args.rows)
        executor.write(data, args.out_dir, args.format)

    The executor already handles: topological ordering, FK resolution with
    cardinality (N:1, 1:1, avg:<n>, range:<lo>-<hi>), self-referencing FKs,
    uniqueness and unique_together, when/then rules, null_rate, enum
    distributions (weighted), min/max bounds, PII synthesis, charset:
    unicode, and control_char_rate.  Overrides are ONLY for domain
    vocabulary/semantics the library cannot know (e.g. realistic lender
    names, agency names, score-model labels, correlated business logic).
    An override receives (rng, partial_row, idx) and returns the value.

    HARD RULES:
    1. Output ONLY Python code in a single ```python fenced block — no
       explanation before or after.
    2. Imports: standard library plus the two internal modules shown above.
       No other third-party imports.
    3. The script must accept (argparse):
       --schema-dir DIR  --out-dir DIR  --rows N  --format csv|json|xml|parquet  --seed N
       and pass them to the library exactly as in the example.
    4. Use register_override for columns where the schema alone would give
       generic values (lender/agency/party names, model labels, etc.).
       Override values must be PLAUSIBLE BUT ENTIRELY SYNTHETIC — never
       real institutions' data, never real people.
    5. Do not open, write, or format data files yourself — executor.write
       does that.  Do not re-seed random yourself — the executor owns rng.
    6. Keep the script short; its whole job is wiring plus overrides.

    GENERATION ORDER AND STRATEGIES (for your awareness):
    {plan_summary}

    TABLE SCHEMAS (YAML):
    ---
    {schemas_yaml}
    ---

    Return the Python script now:
""")


class CodegenError(Exception):
    """Raised when code generation fails or returns an implausible script."""


def build_prompt(graph: SchemaGraph) -> str:
    """Render the codegen prompt from the graph's generation plan."""
    plan_lines = []
    for plan in graph.generation_plan():
        deps = graph.get_dependencies(plan.table_name)
        plan_lines.append(
            f"{plan.order_index + 1}. {plan.table_name} "
            f"[{plan.generation_strategy}]" + (f" depends on: {', '.join(deps)}" if deps else "")
        )
        for group in plan.constraints.unique_together:
            plan_lines.append(f"   unique_together: {group}")

    schemas_yaml = yaml.dump(
        [s.model_dump(mode="json", exclude_none=True) for s in graph.schemas.values()],
        default_flow_style=False,
        sort_keys=False,
    )
    return CODEGEN_PROMPT_TEMPLATE.format(
        plan_summary="\n".join(plan_lines),
        schemas_yaml=schemas_yaml,
    )


def generate_script(
    graph: SchemaGraph,
    bridge: CopilotBridge,
    script_path: Path,
) -> Path:
    """Ask Copilot CLI to author the generator script and save it for review.

    The returned path is the reviewable artifact — nothing is executed here.

    Raises
    ------
    CodegenError
        If the response does not look like a runnable Python script.
    """
    prompt = build_prompt(graph)
    response = bridge.run_prompt(prompt, fence_lang="python")
    script = response.yaml_content  # payload field; holds Python here

    # Cheap plausibility gate — not a security review, just "is this code".
    if "def " not in script and "import " not in script:
        raise CodegenError(
            "Copilot response does not look like a Python script "
            f"(first 200 chars): {script[:200]!r}"
        )

    script_path = Path(script_path)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script + "\n", encoding="utf-8")
    logger.info("Generator script written to '%s' (%d chars)", script_path, len(script))
    return script_path
