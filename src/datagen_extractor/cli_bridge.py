"""Subprocess wrapper for the standalone Copilot CLI binary.

Invokes ``copilot -p "<prompt>" -s --allow-all-tools`` to run extraction
prompts in non-interactive mode.  The ``-s`` (silent) flag suppresses
everything except the agent's response text.  ``--allow-all-tools`` prevents
the process from blocking on interactive permission prompts.

All prompts and raw responses are logged for compliance traceability.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Exceptions


class CopilotCLIError(Exception):
    """Raised when the Copilot CLI subprocess fails."""


class CopilotNotFoundError(CopilotCLIError):
    """Raised when the copilot binary is not on PATH."""


# Prompt template

DEFAULT_PROMPT_TEMPLATE = textwrap.dedent("""\
    You are a schema extraction engine for a banking synthetic-data platform.
    You will be given a requirements document (a Confluence export or a
    free-text requirement).  Extract EVERY table it describes or implies into
    ONE YAML document conforming EXACTLY to the contract below.  The YAML is
    machine-validated with Pydantic in strict mode: any missing required key,
    any extra/unknown key, or any out-of-vocabulary value is a hard failure.

    STRICT OUTPUT CONTRACT — the top-level key MUST be `tables`, a list:

    tables:
      - table_name: <snake_case string>        # REQUIRED
        description: <string>                  # optional
        row_count_hint: <integer>              # optional — approximate rows implied by the doc
        unique_together:                       # optional — composite uniqueness
          - [<column>, <column>]
        rules:                                 # optional — cross-field conditions
          - when: {{<column>: <value>}}        #   all pairs must match (AND)
            then: {{<column>: not_null}}       #   effect: not_null | null
        fields:                                # REQUIRED, at least one
          - name: <exact column name>          # REQUIRED
            type: <one of: string, integer, decimal, date, datetime,
                   boolean, uuid, enum, text, timestamp>   # REQUIRED
            format: <string>                   # optional — e.g. "YYYY-MM-DD", "email", "E.164"
            distribution: <string>             # optional — "uniform" | "normal" |
                                               #   "enum:V1,V2" | weighted "enum:V1@0.7,V2@0.3"
            min: <value>                       # optional — min value / length / date
            max: <value>                       # optional — max value / length / date
            nullable: <bool>                   # default false
            null_rate: <float 0.0-1.0>         # optional, only with nullable: true —
                                               #   fraction of rows that should be NULL
            charset: <ascii | unicode>         # optional, string/text only —
                                               #   unicode = values include non-ASCII text
            control_char_rate: <float 0.0-1.0> # optional, string/text only — fraction of
                                               #   rows embedding control chars (edge-case testing)
            unique: <bool>                     # default false
            pii: <bool>                        # default false
            fk_ref: <"table.column">           # optional — FK target
            cardinality: <string>              # optional, only with fk_ref —
                                               #   "N:1" | "1:1" | "avg:<int>" | "range:<lo>-<hi>"
            confidence: <float 0.0-1.0>        # default 1.0; < 1.0 when inferred
            note: <string>                     # optional — required when confidence < 1.0

    NO OTHER KEYS ARE ALLOWED at any level.  Do not add keys like
    "primary_key", "indexes", "constraints", "example", or "comment".

    RULES:
    1. Return ONLY the YAML document inside one ```yaml fenced block —
       no explanatory text before or after.
    2. Output ALL tables in ONE document under `tables` — never one document
       per table.
    3. Use the EXACT column names from the document; if names are only
       implied, derive snake_case names and set confidence < 1.0 with a note.
    4. Mark primary keys as unique: true.  Express foreign keys as
       fk_ref: "parent_table.parent_column", and estimate cardinality
       (children per parent) with "avg:<n>" or "range:<lo>-<hi>" whenever the
       document gives volume/ratio hints.
    5. Set row_count_hint per table from any volume statements in the doc.
    6. Set pii: true for names, SSNs/national IDs, emails, phones, addresses,
       and dates of birth — even synthetic ones.
    7. For enum/categorical fields use distribution "enum:V1,V2,..." with the
       exact value vocabulary; add @weights when the doc implies proportions.
    8. When the document states or implies how often a nullable column is
       empty, set null_rate.  When it describes conditional structure
       ("closed accounts have a close date", "charged-off accounts link to a
       collections record"), express it as a table-level rule with when/then.
    9. Ambiguous or conflicting requirements: pick the most defensible
       reading, set confidence < 1.0, and explain the ambiguity in note —
       never silently resolve it.

    {error_feedback}

    REQUIREMENTS DOCUMENT:
    ---
    {confluence_content}
    ---

    Return the YAML now:
""")


# Response dataclass


@dataclass
class CopilotResponse:
    """Captures a single Copilot CLI invocation's result."""

    raw_output: str
    yaml_content: str
    exit_code: int
    stderr: str = ""


# Bridge


@dataclass
class CopilotBridge:
    """Wraps the standalone ``copilot`` binary for non-interactive extraction.

    Parameters
    ----------
    timeout:
        Subprocess timeout in seconds.  Default 120; increase for large docs.
    copilot_binary:
        Name or path of the copilot binary.  Resolved via ``shutil.which``.
    prompt_template:
        The prompt template string with ``{confluence_content}`` and
        ``{error_feedback}`` placeholders.
    """

    timeout: int = 120
    copilot_binary: str = "copilot"
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE

    def __post_init__(self) -> None:
        self._preflight_check()

    # preflight -----------

    def _preflight_check(self) -> None:
        """Fail fast if the copilot binary is not on PATH."""
        if shutil.which(self.copilot_binary) is None:
            raise CopilotNotFoundError(
                f"'{self.copilot_binary}' not found on PATH. "
                f"Install the standalone Copilot CLI: "
                f"https://docs.github.com/copilot/how-tos/copilot-cli\n"
                f"NOTE: This is the standalone 'copilot' binary, "
                f"NOT 'gh copilot suggest'."
            )

    # command construction

    def _build_command(self, prompt: str) -> list[str]:
        """Construct the subprocess command with non-interactive flags."""
        return [
            self.copilot_binary,
            "-p",
            prompt,
            "-s",  # silent: output only the agent response
            "--allow-all-tools",  # prevent interactive permission prompts
            "--model",
            "auto",  # let Copilot pick an available model
        ]

    # response parsing ----

    _FENCE_LANGS = {"yaml": r"ya?ml", "python": r"py(?:thon)?"}

    @classmethod
    def _strip_fences(cls, raw_response: str, lang: str = "yaml") -> str:
        """Extract fenced code-block content from a markdown response.

        Handles ``` with or without a language tag; falls back to the raw
        response if no fences are found.
        """
        alias = cls._FENCE_LANGS.get(lang, re.escape(lang))
        pattern = rf"```(?:{alias})?\s*\n(.*?)\n```"
        match = re.search(pattern, raw_response, re.DOTALL)
        if match:
            return match.group(1).strip()
        # No fences — assume the entire response is the payload.
        return raw_response.strip()

    @classmethod
    def _strip_yaml_fences(cls, raw_response: str) -> str:
        return cls._strip_fences(raw_response, "yaml")

    # generic invocation --

    def run_prompt(self, prompt: str, fence_lang: str = "yaml") -> CopilotResponse:
        """Send an arbitrary prompt through Copilot CLI.

        The response's fenced code block (``fence_lang``) is extracted into
        ``yaml_content`` (kept under that name for backward compatibility —
        it holds whatever payload language was requested).

        Raises
        ------
        CopilotCLIError
            On non-zero exit code or subprocess timeout.
        """
        cmd = self._build_command(prompt)

        logger.info(
            "Copilot CLI invocation",
            extra={
                "cmd": cmd[0],
                "prompt_length": len(prompt),
                "timeout": self.timeout,
            },
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CopilotCLIError(
                f"Copilot CLI timed out after {self.timeout}s.  "
                f"Try increasing --timeout for large documents."
            ) from exc

        if result.returncode != 0:
            raise CopilotCLIError(
                f"Copilot CLI exited with code {result.returncode}.\n"
                f"stderr: {result.stderr.strip()}"
            )

        raw_output = result.stdout
        yaml_content = self._strip_fences(raw_output, fence_lang)

        logger.info(
            "Copilot CLI response received",
            extra={
                "raw_length": len(raw_output),
                "yaml_length": len(yaml_content),
                "exit_code": result.returncode,
            },
        )

        return CopilotResponse(
            raw_output=raw_output,
            yaml_content=yaml_content,
            exit_code=result.returncode,
            stderr=result.stderr,
        )

    # extraction ----------

    def extract(
        self,
        confluence_markdown: str,
        error_feedback: str = "",
    ) -> CopilotResponse:
        """Send Confluence content through Copilot CLI and return the response.

        Parameters
        ----------
        confluence_markdown:
            The raw Confluence data contract markdown.
        error_feedback:
            Optional error context from a prior failed attempt, injected
            into the prompt for retry correction.

        Raises
        ------
        CopilotCLIError
            On non-zero exit code or subprocess timeout.
        """
        prompt = self.prompt_template.format(
            confluence_content=confluence_markdown,
            error_feedback=error_feedback,
        )
        return self.run_prompt(prompt, fence_lang="yaml")
