# How to Use This Tool — A Beginner's Guide

## 1. What is this tool?

This tool creates **fake (synthetic) data that looks real**, for testing
banking software.

You give it a **description of the data you need** — written in ordinary
English, for example a page copied from Confluence, or just a paragraph like
*"I need 100,000 customers, each with a few bank accounts and their
transactions"* — and the tool:

1. **Reads your description** and works out what tables and columns you need
   (using AI — GitHub Copilot).
2. **Shows you its understanding** in small files you can read and correct.
3. **Writes a small computer program** that produces the data (you can look
   at this program before it runs — nothing runs behind your back).
4. **Runs that program** to create the data files.
5. **Double-checks the result** — every link between tables is valid, no
   duplicate IDs, percentages match what you asked for, and so on.

Nothing in the data is real. No real names, no real account numbers, no real
social security numbers — ever. That is the whole point: you get realistic
test data without touching real customer information.

---

## 2. Words you will see (plain-English glossary)

| Word | What it means here |
|---|---|
| **Terminal** | The app where you type commands. On Mac it's called "Terminal". Every command in this guide is typed there, then you press Enter. |
| **`make something`** | Our commands all start with the word `make`. Think of it as "please do…". `make run` = "please run the whole thing". |
| **Table** | Like one sheet in Excel — rows and columns. Example: a `customers` table. |
| **Schema** | The *description* of a table: what columns it has, what type each is (number, date, …), and the rules (must be unique, can be empty, …). Not the data itself — the blueprint. |
| **YAML file** | A simple text file (ends in `.yaml`) that stores a schema. Human-readable. You can open and edit it in any text editor. |
| **Foreign key (FK)** | A column that points to another table. Every account has a `customer_id` that must match a real customer — that's a foreign key. |
| **Pipeline** | The whole assembly line: read description → schema → program → data → checks. |
| **Copilot** | GitHub Copilot, the AI service this tool uses. You need it installed and logged in (one-time, see below). |

---

## 3. One-time setup (do this once)

You need three things on your computer. Ask IT for help if any step fails —
each is a one-time install.

**Step 1 — Check Python is installed.** In the terminal, type:

```
python3 --version
```

You should see something like `Python 3.12.x`. Any version 3.11 or higher is
fine.

**Step 2 — Install GitHub Copilot CLI** (the AI). Follow:
https://docs.github.com/copilot/how-tos/copilot-cli — then log in when it
asks. To check it worked, type:

```
copilot --version
```

**Step 3 — Set up this tool.** In the terminal, go to the tool's folder and
type:

```
make setup
```

This installs everything the tool needs and confirms Copilot is ready. You
should see a line ending in `✓ Copilot CLI: ...`. That's it — you never need
to do this again.

> **Tip:** to "go to the tool's folder", type `cd ` (with a space), drag the
> folder from Finder into the terminal window, and press Enter.

---

## 4. Quick start — from a description to data in 3 commands

**Step 1 — Put your description in the `input` folder.**
Save your requirements as a plain text file, for example
`input/my_requirements.md`. Write it like you'd explain it to a colleague:

> We need test data for a small retail bank. About 5,000 customers with
> names, emails and addresses. Each customer has 1 to 4 accounts (checking,
> savings, or credit card — credit cards being the most common). Each
> account has transactions going back up to 2 years, a few hundred per
> account. Include some closed accounts, and closed accounts must have a
> closing date.

**Step 2 — Let the AI read it and propose the tables:**

```
make generate-schema INPUT=input/my_requirements.md
```

This takes about a minute. You'll see the tables it understood
(e.g. `customers`, `accounts`, `transactions`) appear on screen and get
saved into the `output` folder — one small file per table. **Open them and
read them** — section 7 explains what everything means. If something is
wrong (a column missing, a percentage off), you can simply edit the file in
a text editor and save it.

**Step 3 — Create the data:**

```
make run
```

This writes the data-making program, shows it runs it, and prints a big
green checklist at the end. If you see `✓ All checks passed` — your data is
ready in the folder `run_output/data`, one file per table.

**Want more or fewer rows? A different file type?** See the settings in
section 6, e.g.:

```
make run ROWS=5000 FORMAT=csv
```

---

## 5. Every command, explained

Type `make` on its own at any time to see this list in the terminal.

### Commands you'll use all the time

| You type | What happens | When to use it |
|---|---|---|
| `make generate-schema` | The AI reads your description file and writes one schema file per table into `output/` | First step for any new requirement |
| `make run` | The whole rest of the pipeline: writes the program, runs it, checks the data | When you're happy with the schemas and just want data |
| `make validate-schema` | Checks your schema files are well-formed (catches typos after hand-editing) | Every time you hand-edit a schema file |
| `make check` | Re-checks already-created data without regenerating it | After you've looked at data and want to re-verify |
| `make clean` | Deletes generated data and temporary files (never your schemas or your description) | To start fresh |

### The careful, step-by-step alternative to `make run`

If you (or your reviewers) want to inspect each stage before the next one
runs — recommended the first few times:

| Order | You type | What happens |
|---|---|---|
| 1 | `make generate-schema` | AI proposes the tables (as above) |
| 2 | `make validate-schema` | Confirms the schema files are valid |
| 3 | `make graph` | Shows which table depends on which, and the order they'll be created in |
| 4 | `make pii` | Lists every column with personal-looking data (names, emails…) and confirms each is marked to be faked safely |
| 5 | `make generate-code` | The AI writes the data-making program — **and stops** so you can read it |
| 6 | `make review` | Prints that program on screen for reading |
| 7 | `make execute` | Runs the reviewed program and prints the checklist |

### Commands for technical colleagues

`make test` (self-tests), `make lint` and `make format` (code style) — a
developer maintaining the tool uses these; you don't need them.

---

## 6. Settings (parameters) — with examples

Every setting is optional and has a sensible default. You add them after the
command as `NAME=value`, in any combination:

```
make run ROWS=1000 FORMAT=json SEED=7
```

| Setting | What it controls | Default | Examples |
|---|---|---|---|
| `ROWS` | How many rows in the *main* tables (e.g. customers). Dependent tables scale automatically — if each customer has ~3 accounts, `ROWS=1000` gives ~3,000 accounts | `150` | `ROWS=50` (tiny test), `ROWS=100000` (full volume) |
| `FORMAT` | The file type of the data produced | `csv` | `FORMAT=csv` (opens in Excel) · `FORMAT=json` (for developers/APIs) · `FORMAT=xml` (for older systems) · `FORMAT=parquet` (for data warehouses / big data tools) |
| `SEED` | The "randomness dial". Same seed = exactly the same data every run (good for repeatable tests). Change the number to get a different batch | `0` | `SEED=1`, `SEED=42` |
| `INPUT` | Which description file to read | `input/requirement_sectionA.md` | `INPUT=input/my_requirements.md` |
| `SCHEMA_DIR` | Which folder of schema files to use | `output` | `SCHEMA_DIR=samples/schemas` (the built-in demo) |
| `RUN_DIR` | Where the program + data get written | `run_output` | `RUN_DIR=run_march_test` |

**Worked examples:**

```
# Small trial batch, Excel-friendly:
make run ROWS=100 FORMAT=csv

# Full-size batch for the data warehouse team:
make run ROWS=100000 FORMAT=parquet

# Reproduce exactly the batch a colleague made (same seed = same data):
make run ROWS=1000 SEED=42

# Try the tool right now with the built-in demo (no AI needed for schemas):
make run SCHEMA_DIR=samples/schemas ROWS=200
```

---

## 7. Reading (and editing) a schema file

After `make generate-schema`, the `output` folder holds one file per table.
They're plain text — here is a small one with **every line explained**:

```yaml
table_name: accounts                # the table's name
description: Customer bank accounts # a note for humans — no effect on data
row_count_hint: 100000              # how many rows your description implied

fields:                             # the list of columns starts here
  - name: account_id                # column: the account's ID number
    type: integer                   # it's a whole number
    unique: true                    # no two rows may share a value (it's an ID)

  - name: customer_id               # column: which customer owns this account
    type: integer
    fk_ref: customers.customer_id   # must match a real row in the customers table
    cardinality: avg:3              # on average 3 accounts per customer

  - name: account_type              # column: what kind of account
    type: enum                      # "enum" = only specific values allowed
    distribution: enum:credit_card@0.6,personal_loan@0.25,home_loan@0.15
    # ^ the allowed values AND how often each appears:
    #   60% credit cards, 25% personal loans, 15% home loans

  - name: holder_name               # column: the account holder's name
    type: string                    # free text
    pii: true                       # personal info -> tool invents FAKE names
    charset: unicode                # include international names (José, Müller…)

  - name: close_date                # column: when the account was closed
    type: date
    nullable: true                  # allowed to be empty
    null_rate: 0.8                  # …and IS empty for ~80% of rows

  - name: memo                      # column: free-text note
    type: text
    control_char_rate: 0.1          # 10% of rows get "awkward" characters
                                    # (tabs/line-breaks) — great for finding
                                    # bugs in the software being tested

unique_together:                    # combinations that must not repeat
  - [branch_code, account_number]   # same branch can't have the same acct number twice

rules:                              # if-then rules between columns
  - when: {status: closed}          # IF status is "closed"
    then: {close_date: not_null}    # THEN close_date must be filled in
  - when: {status: open}            # IF status is "open"
    then: {close_date: null}        # THEN close_date must be empty
```

### Cheat-sheet: everything you can put on a column

| You write | In plain terms | Example |
|---|---|---|
| `type:` | What kind of value. Choose from: `string` (text), `integer` (whole number), `decimal` (number with cents), `date`, `datetime`/`timestamp` (date+time), `boolean` (yes/no), `uuid` (long unique code), `enum` (fixed list of values), `text` (long text) | `type: decimal` |
| `unique: true` | No duplicates allowed in this column | ID columns |
| `nullable: true` | The cell may be empty | optional fields |
| `null_rate: 0.3` | …and is empty about 30% of the time | `close_date` |
| `pii: true` | Personal information — the tool generates safe fakes (made-up names, `…@example.com` emails, impossible SSNs, 555 phone numbers) | names, emails |
| `fk_ref: customers.customer_id` | Must match a value in another table | account → customer |
| `cardinality: avg:3` | About 3 rows here per row there | 3 accounts/customer |
| `cardinality: range:1-500` | Between 1 and 500 rows per parent row | transactions/account |
| `cardinality: 1:1` | Exactly one each | one profile per customer |
| `distribution: enum:A,B,C` | Only these values, roughly evenly | statuses |
| `distribution: enum:A@0.7,B@0.3` | Only these values, 70% / 30% (numbers must add up to 1.0) | your credit-card example |
| `distribution: normal:mean=680,std=60` | Bell curve around 680 | credit scores |
| `min:` / `max:` | Lowest/highest allowed (for numbers and dates), or shortest/longest (for text) | `min: 300`, `max: 850` |
| `charset: unicode` | Include international characters | global names |
| `control_char_rate: 0.1` | 10% of values contain tricky hidden characters — stress-tests the software reading the files | memo fields |
| `confidence: 0.8` + `note:` | The AI wasn't 100% sure and explains why — **these are the lines to review most carefully** | anything inferred |

**To change something:** open the file in any text editor, change the value,
save, then run `make validate-schema`. If it prints `✓ PASS` for your file,
you're good — run `make run` next.

---

## 8. Where everything ends up

```
your-tool-folder/
├── input/                      ← YOUR description files go here
├── output/                     ← the schemas the AI proposed (1 file per table)
├── run_output/
│   ├── generated/generate_data.py   ← the program the AI wrote (readable!)
│   └── data/                        ← ★ YOUR DATA FILES ★  (customers.csv, …)
└── samples/schemas/            ← a built-in demo you can always play with
```

> ⚠ One thing to watch: `output/` is never emptied automatically. If you
> switch to a *different* requirements document, delete the old schema files
> first, or old and new tables will get mixed together.

---

## 9. What the final checklist means

After every run the tool prints a table of checks. In plain terms it
verifies:

- **Every file exists** and has the expected columns.
- **No broken links** — every account really belongs to an existing
  customer, every transaction to an existing account (including tricky
  cases like "employee's manager is another employee").
- **No illegal duplicates** — IDs are unique; combinations you declared
  unique (branch + account number) don't repeat.
- **Empty cells only where allowed**, and roughly as often as you declared.
- **Your if-then rules hold on every single row** (every closed account has
  a close date — no exceptions).
- **Percentages came out right** — if you asked for 60% credit cards, the
  data is within a small tolerance of 60%.
- **Numbers, dates and text lengths stay inside the min/max you set.**
- **International characters and tricky characters** appear where you asked
  for them (and nowhere you didn't).

`✓ All checks passed` = safe to hand the data over. Any `✗` line names the
table, the problem, and the numbers, e.g.
`accounts | unique | ✗ | account_id: 2 duplicate values`.

---

## 10. Common problems, in plain terms

| What you see | What it means | What to do |
|---|---|---|
| `'copilot' not found on PATH` | The AI isn't installed (or not logged in) | Redo Step 2 of setup; then `make setup` to confirm |
| `Extraction failed after 3 attempts` | The AI couldn't turn your description into valid schemas | Your description is probably ambiguous — add specifics (table names, rough volumes, which value lists) and retry |
| `Circular FK dependency detected` | Two tables each claim to depend on the other — impossible to build | Open the named schema files; remove one of the two `fk_ref` lines (a table pointing at *itself*, like employee → manager, is fine and not this error) |
| A `✗` in the final checklist | The data broke one of your declared rules | Read the row — it names the table and column. Usually fixed by correcting the schema and re-running |
| `Copilot CLI timed out` | The AI took too long (big document) | Just run the command again; if it persists, split the description into smaller parts |
| Strange `✗ FAIL` after you edited a schema | A typo in the file (YAML is picky about spaces) | `make validate-schema` shows the exact line; compare indentation with the examples in section 7 |
| Excel shows odd characters | Your data intentionally contains international/tricky characters (you asked for `charset: unicode` / `control_char_rate`) | That's the feature working; open with "UTF-8" encoding, or remove those lines from the schema if unwanted |

---

## 11. Rules the tool never breaks (why you can trust it)

1. **The AI never writes data directly.** It writes a *program* you can
   read; only that program produces data. What you review is what runs.
2. **All personal-looking data is invented.** Fake names, `example.com`
   emails, impossible SSNs (900-range), 555 phone numbers.
3. **Everything is driven by the schema files you can read and edit.**
   Change the file → change the data. No hidden behavior.
4. **Data goes to files only** — the tool never writes into a database.

---

## Appendix — for technical users

<details>
<summary>Click to expand: Python API, internals, developer docs</summary>

### Python API

Everything the commands do is importable. The generation library:

```python
import random
from datagen_core.generators import (
    GenerationExecutor, generate_value, resolve_fk, child_counts,
    sample_distribution,
)
from datagen_extractor.graph import SchemaGraph

rng = random.Random(42)
sample_distribution("enum:credit_card@0.6,personal_loan@0.4", rng)
child_counts(100, "avg:3", rng)          # children per parent, ~3 each
resolve_fk([1, 2, 3], "1:1", rng)        # → [1, 2, 3]

graph = SchemaGraph.from_directory("samples/schemas")
executor = GenerationExecutor(graph, seed=42)
executor.register_override(              # domain flavor only
    "accounts", "branch_code", lambda rng, row, idx: rng.choice(["BR-01", "BR-02"])
)
data = executor.generate(base_rows=150)  # dict[table, list[row]]
executor.write(data, "out_dir", "csv")   # csv | json | xml | parquet
```

Extraction, planning, orchestration:

```python
from pathlib import Path
from datagen_extractor.cli_bridge import CopilotBridge
from datagen_extractor.extractor import Extractor
from datagen_extractor.graph import SchemaGraph
from datagen_extractor.harness import run_checks
from datagen_extractor.pii import scan_schemas
from datagen_extractor.pipeline import run_pipeline, execute_script

bridge = CopilotBridge(timeout=420)
schemas = Extractor(bridge=bridge, output_dir=Path("output")).extract_file(
    Path("input/requirement.md")
)
graph = SchemaGraph.from_directory(Path("output"))
graph.topological_order(); graph.get_dependencies("accounts")
report = scan_schemas(list(graph.schemas.values()))     # PII scan
result = run_pipeline(Path("output"), Path("run_output"), bridge,
                      rows=1000, fmt="csv", seed=42, stop_after=None)
checks = run_checks(graph, result.data_dir)
```

Key exceptions: `CopilotCLIError`, `ExtractionExhaustedError`,
`SchemaGraphError`/`CycleError`/`UnknownTableError`, `CodegenError`,
`ExecutionError`.

### Generated-script contract

`python generate_data.py --schema-dir DIR --out-dir DIR --rows N
--format csv|json|xml|parquet --seed N`. Scripts are thin wrappers over
`GenerationExecutor` + `register_override` — see `AGENTS.md`.

### Hard constraints

| ID | Constraint |
|---|---|
| C-1 | GitHub Copilot CLI is the only AI runtime (subprocess) |
| C-2 | The LLM authors reviewable code; only that code produces data |
| C-3 | All business logic lives in the YAML schemas |

### More

- `AGENTS.md` — architecture map, conventions, test patterns (also read
  automatically by Copilot CLI and other AI assistants working in this repo).
- Parquet support needs `pyarrow`: `pip install "datagen-extractor[parquet]"`
  (already included in dev installs).
- `datagen-extract-requirements.md` — the full requirements specification.

</details>
