# datagen-extract — How to Use

Enterprise synthetic data generation for banking systems. A requirements document
(Confluence export or free text) goes in; referentially-intact, statistically
realistic, PII-safe synthetic data files come out. GitHub Copilot CLI is the only
AI runtime (invoked via subprocess), the model only ever authors *code* — never
data values — and all business logic lives in YAML.

```
Ingest → Generate Schema (YAML) → Validate/Refine ⏸ → Plan (FK graph)
       → Generate Code ⏸ → Execute Code → Output Data → Integrity Checks
                          (⏸ = manual-intercept checkpoints)
```

---

## 1. Installation

Prerequisites: Python ≥ 3.11, GNU make, and the **standalone GitHub Copilot CLI**
(`copilot` on PATH — not `gh copilot`): https://docs.github.com/copilot/how-tos/copilot-cli

```bash
make setup        # creates .venv, installs the package + dev deps, verifies copilot
```

Everything below can be run through `make` (recommended) or the
`datagen-extractor` CLI directly (`.venv/bin/datagen-extractor --help`).

---

## 2. Makefile — all commands

`make` (or `make help`) prints this list. Variables can be overridden per
invocation: `make run SCHEMA_DIR=samples/schemas ROWS=500 FORMAT=json`.

| Target | What it does |
|---|---|
| `make install` | Create venv, install package + dev dependencies |
| `make setup` | `install` + verify the Copilot CLI is available |
| `make generate-schema` | Extract schemas from `INPUT` via Copilot CLI into `SCHEMA_DIR` |
| `make validate-schema` | Strict Pydantic validation of every YAML in `SCHEMA_DIR` |
| `make graph` | Show the FK dependency graph and generation order |
| `make pii` | PII scan; **fails** if PII-like columns aren't tagged `pii: true` |
| `make generate-code` | Run pipeline up to code generation, then stop (manual intercept) |
| `make review` | Print the generated script for human review |
| `make run` | Full automated pipeline: plan → codegen → execute → checks |
| `make execute` | Execute the reviewed script, then run integrity checks |
| `make check` | Re-run integrity + fidelity checks on existing data |
| `make test` | Full pytest suite (offline — uses a fake Copilot binary) |
| `make lint` / `make format` | ruff check / ruff format on `src/` and `tests/` |
| `make clean` | Delete run outputs and caches (keeps schemas and venv) |

**Variables** (defaults): `INPUT=input/requirement_sectionA.md`,
`SCHEMA_DIR=output`, `RUN_DIR=run_output`, `ROWS=150`, `FORMAT=csv` (choices:
`csv`, `json`, `xml`, `parquet`), `SEED=0`.

### Typical flows

```bash
# Automated one-shot
make generate-schema && make run

# Manual intercept (review at each checkpoint)
make generate-schema        # 1. extract schema YAML
make validate-schema        # 2. validate; hand-edit output/*.yaml if needed
make graph                  #    (inspect FK graph & generation order)
make pii                    #    (confirm PII tagging)
make generate-code          # 3. Copilot authors the generator script, then stops
make review                 # 4. read the script
make execute                # 5. run it + integrity checks
```

---

## 3. Where everything is stored

| Path | Contents |
|---|---|
| `input/` | Requirements documents you feed in (Confluence dumps, free text) |
| `output/` (= `SCHEMA_DIR`) | Extracted, validated YAML schemas — **one file per table**, named `<table_name>.yaml`. Editable by hand; re-run `make validate-schema` after editing. ⚠ Extraction is additive: if a new requirements doc produces different table names, stale files from a previous doc remain — delete them before running `make graph`/`run` |
| `run_output/` (= `RUN_DIR`) | One pipeline run: |
| `run_output/generated/generate_data.py` | The Copilot-authored generator script (the reviewable C-2 artifact). Scripts are thin: they drive the internal `datagen_core.generators` library (`GenerationExecutor`) from the validated YAML and add domain overrides only. Contract: `--schema-dir --out-dir --rows --format --seed` |
| `run_output/data/` | The generated data files, `<table_name>.<fmt>` — format is user-selected at run time: **csv, json, xml, or parquet** (UTF-8; Parquet needs `pip install "datagen-extractor[parquet]"`, already included in dev installs). Files only — direct DB/warehouse load is not supported (§8/§9) |
| `samples/schemas/` | Ready-made 4-table demo schema set exercising every YAML feature |
| `tests/` | Pytest suite (runs fully offline) |

`run_output/` and `run_credit/` are gitignored; schemas in `output/` are meant to
be reviewed/committed.

---

## 4. The YAML schema format — full reference

One file per table. Strictly validated (Pydantic `extra="forbid"`): unknown keys
are rejected — both for hand-edited files and for Copilot output (which gets the
validation error fed back and retried, max 3 attempts).

```yaml
table_name: accounts              # REQUIRED, snake_case
description: Retail accounts      # optional
row_count_hint: 100000            # optional — volume implied by the source doc
unique_together:                  # optional — composite uniqueness
  - [branch_code, account_number]
rules:                            # optional — cross-field conditions (see 4.3)
  - when: {status: closed}
    then: {close_date: not_null}
fields:                           # REQUIRED — at least one
  - name: account_id              # REQUIRED — exact column name
    type: integer                 # REQUIRED — see allowed types below
    unique: true                  # single-column uniqueness (default false)
  - name: customer_id
    type: integer
    fk_ref: customers.customer_id # FK target "table.column"
    cardinality: avg:3            # children per parent (see 4.2)
  - name: account_type
    type: enum
    distribution: enum:credit_card@0.6,personal_loan@0.25,home_loan@0.15
  - name: close_date
    type: date
    nullable: true                # NULLs allowed (default false)
    null_rate: 0.8                # ~80% of rows NULL (requires nullable: true)
  - name: holder_name
    type: string
    pii: true                     # synthesized as plausible-but-fake values
    charset: unicode              # values include non-ASCII text (see 4.5)
  - name: memo
    type: text
    control_char_rate: 0.1       # ~10% of rows embed control chars (see 4.5)
  - name: opened
    type: date
    format: YYYY-MM-DD            # free-text format hint
    min: "2000-01-01"             # bounds: value for numerics/dates,
    max: "2026-12-31"             #         length for string/text
  - name: score
    type: integer
    distribution: normal:mean=680,std=60
    confidence: 0.8               # < 1.0 when inferred, not stated
    note: Range inferred from FICO convention.
```

**Allowed `type` values:** `string`, `integer`, `decimal`, `date`, `datetime`,
`boolean`, `uuid`, `enum`, `text`, `timestamp`.

### 4.1 Distributions

| Declaration | Meaning | Enforced by harness |
|---|---|---|
| `uniform` | uniform spread | — (advisory to codegen) |
| `normal` / `normal:mean=680,std=60` | bell curve | — (advisory to codegen) |
| `enum:A,B,C` | values only from this set | membership: any value outside the set fails |
| `enum:A@0.6,B@0.3,C@0.1` | weighted categories (weights must sum to 1.0) | membership + frequency within ±0.10 (needs ≥100 rows) |

### 4.2 Foreign keys & cardinality

`fk_ref: parent_table.parent_column` puts the table into the dependency graph;
generation order is computed topologically (cycles are detected and rejected;
self-referencing FKs like `manager_id → employees.employee_id` are handled with
a root-rows-first strategy). `cardinality` shapes volume:

| Value | Meaning |
|---|---|
| `N:1` | plain many-to-one (default FK semantics) |
| `1:1` | exactly one child per parent |
| `avg:12` | about 12 children per parent |
| `range:1-500` | children per parent within [1, 500] |

### 4.3 Cross-field rules

Table-level `rules` express conditional structure. All `when` pairs must match
(AND); each `then` effect is `not_null` or `null`:

```yaml
rules:
  - when: {status: closed}
    then: {close_date: not_null}   # every closed account has a close_date
  - when: {status: open}
    then: {close_date: null}       # open accounts never have one
```

The harness checks every row deterministically — one violating row fails the run.

### 4.4 Null frequency

`nullable: true` allows NULLs; `null_rate: 0.8` says *how often* (~80%).
Checked within ±0.10 tolerance once there are ≥100 rows.

### 4.5 Text profile — Unicode & control characters

For robustness/edge-case testing of downstream parsers:

```yaml
- name: full_name
  type: string
  pii: true
  charset: unicode          # ascii | unicode
- name: memo
  type: text
  control_char_rate: 0.15   # 0.0–1.0
```

- `charset: unicode` — a meaningful share of values contain non-ASCII text
  (José, Müller, Cyrillic/CJK names). Harness fails if **all** values are pure
  ASCII (needs ≥30 rows). `charset: ascii` — harness fails if **any** value
  contains non-ASCII.
- `control_char_rate` — that fraction of rows embed a control character
  (tab/newline/escape — never NUL) *inside* the value. CSV output stays
  parseable via standard quoting. Checked within ±0.10 (needs ≥100 rows).
  Only valid on `string`/`text` columns.

### 4.6 Metadata (written by the extractor)

`source` (the input document path) and `extracted_at` (UTC timestamp) are
injected automatically; `confidence` < 1.0 plus `note` flag anything the model
inferred rather than read — review these at the schema checkpoint.

---

## 5. Python API

### 5.1 `datagen_core.generators` — the generation library

The primitives every generated script builds on (also usable directly):

```python
import random
from datagen_core.generators import (
    GenerationExecutor, generate_value, resolve_fk, child_counts,
    sample_distribution,
)
from datagen_extractor.graph import SchemaGraph

rng = random.Random(42)
sample_distribution("enum:credit_card@0.6,personal_loan@0.4", rng)  # weighted pick
child_counts(100, "avg:3", rng)          # children per parent, ~3 each
child_counts(100, "range:1-500", rng)    # within [1, 500]
resolve_fk([1, 2, 3], "1:1", rng)        # → [1, 2, 3]

graph = SchemaGraph.from_directory("samples/schemas")
executor = GenerationExecutor(graph, seed=42)          # topological executor
executor.register_override(                            # domain flavor only
    "accounts", "branch_code", lambda rng, row, idx: rng.choice(["BR-01", "BR-02"])
)
data = executor.generate(base_rows=150)                # dict[table, list[row]]
executor.write(data, "out_dir", "csv")                 # csv | json | xml | parquet
```

The executor honors everything declared in YAML: topological order, FK
cardinality, self-referencing FKs, uniqueness + `unique_together`, `rules`,
`null_rate`, weighted enums, min/max bounds, PII synthesis, `charset`, and
`control_char_rate`. Deterministic per seed.

### 5.2 `datagen_extractor` — extraction, planning, orchestration

```python
from pathlib import Path

from datagen_extractor.cli_bridge import CopilotBridge
from datagen_extractor.extractor import Extractor
from datagen_extractor.graph import SchemaGraph
from datagen_extractor.harness import run_checks
from datagen_extractor.pii import scan_schemas
from datagen_extractor.pipeline import run_pipeline, execute_script

# Schema extraction (Copilot CLI subprocess) --------------------------
bridge = CopilotBridge(timeout=420)              # preflights `copilot` on PATH
extractor = Extractor(bridge=bridge, output_dir=Path("output"))
schemas = extractor.extract_file(Path("input/requirement.md"))  # list[TableSchema]

# Dependency graph & generation plan ----------------------------------
graph = SchemaGraph.from_directory(Path("output"))
graph.topological_order()          # ['consumers', 'accounts', ...]
graph.get_dependencies("accounts") # ['consumers']
graph.get_dependents("consumers")  # ['accounts', ...]
for plan in graph.generation_plan():
    print(plan.table_name, plan.generation_strategy, plan.constraints.unique_columns)

# PII scan ----
report = scan_schemas(list(graph.schemas.values()))
assert not report.untagged, f"untagged PII: {report.untagged}"

# Full pipeline (automated or manual intercept) ------------------------
result = run_pipeline(
    schema_dir=Path("output"),
    out_dir=Path("run_output"),
    bridge=bridge,
    rows=1000, fmt="csv", seed=42,
    stop_after=None,        # or "plan" / "code" for manual intercept
)
print(result.script_path)             # the reviewable generated script
print(result.harness_report.passed)   # all integrity/fidelity checks

# Execute a reviewed script + re-check separately ----------------------
execute_script(result.script_path, Path("run_output/data"), rows=1000, fmt="csv")
checks = run_checks(graph, Path("run_output/data"))
for failure in checks.failures:
    print(failure.table, failure.check, failure.detail)
```

Key exceptions to handle: `CopilotCLIError`, `ExtractionExhaustedError`
(retries exhausted — carries the full attempt trail), `SchemaGraphError` /
`CycleError` / `UnknownTableError`, `CodegenError`, `ExecutionError`.

---

## 6. What the harness checks after every run

Per table: file presence & row count, expected columns, single-column
uniqueness, composite (`unique_together`) uniqueness, non-null enforcement,
`null_rate` tolerance, FK integrity (orphan detection, incl. self-referencing
FKs), cross-field `rules`, enum membership & weighted-enum frequency, min/max
bounds (numeric / string-length / date), `charset` conformance, and
`control_char_rate` tolerance. Any failure → non-zero exit (CI-friendly).

---

## 7. Hard constraints (always in force)

| ID | Constraint | How it's honored |
|---|---|---|
| C-1 | GitHub Copilot CLI is the only AI runtime | single subprocess wrapper (`cli_bridge.py`); no other provider anywhere |
| C-2 | LLM never outputs data values | the model authors a Python script saved to `run_output/generated/` for review; only that script, run as a subprocess, writes data |
| C-3 | Business logic lives in YAML | schemas, distributions, rules, cardinality, text profiles are all YAML; the engine is generic |

---

## 8. AI helper — `AGENTS.md`

`AGENTS.md` at the repo root is a machine-oriented companion to this document:
architecture map, YAML vocabulary summary, code conventions, the offline
fake-copilot test pattern, and the checklist of files to touch when extending
the schema vocabulary. Two audiences:

- **Copilot CLI itself** — it reads `AGENTS.md` automatically when the pipeline
  invokes it inside this repo, which makes schema extraction and code
  generation more consistent with our contracts.
- **Any AI coding assistant** (Claude Code, Copilot, etc.) working on this
  codebase — point it at `AGENTS.md` first to generate correct code faster.

Keep it up to date when adding modules or YAML features.

---

## 9. Troubleshooting

- **`'copilot' not found on PATH`** — install the *standalone* Copilot CLI
  (not `gh copilot`); `make setup` verifies it.
- **Extraction fails after 3 attempts** — the error shows the per-attempt
  failure trail; usually the requirements doc is too ambiguous. Refine the doc
  or hand-write the YAML and continue from `make validate-schema`.
- **`Circular FK dependency detected`** — the schema set has a real FK cycle;
  break it by removing/redirecting one `fk_ref` (self-references are fine and
  don't count as cycles).
- **Harness frequency checks "unexpectedly" pass/fail near the threshold** —
  statistical checks (`enum` weights, `null_rate`, `control_char_rate`) need
  ≥100 rows (charset: ≥30) and use ±0.10 tolerance; small runs skip them.
- **Stale schemas after re-extracting a different document** — extraction adds
  files per table and doesn't delete old ones; clear `output/` of leftovers
  (check the `extracted_at` timestamp) before `make graph` / `make run`.
