"""Extract → Validate → Retry loop.

Orchestrates the full extraction pipeline:
1. Load Confluence markdown from disk.
2. Call CopilotBridge.extract() with the prompt.
3. Parse the returned YAML.
4. Validate against TableSchema via Pydantic.
5. On validation failure: feed the LATEST error back (not cumulative) and retry.
6. On success: write validated YAML to output/.
7. On exhaustion (3 failures): raise ExtractionExhaustedError with full trail.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from datagen_extractor.cli_bridge import CopilotBridge, CopilotResponse
from datagen_extractor.schema import SchemaBundle, TableSchema

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


# Exceptions


@dataclass
class AttemptRecord:
    """Record of a single extraction attempt."""

    attempt: int
    raw_response: str
    yaml_content: str
    error: Optional[str] = None
    succeeded: bool = False


class ExtractionExhaustedError(Exception):
    """Raised after all retry attempts are exhausted.

    Carries the full failure trail for debugging prompt quality.
    """

    def __init__(self, source: str, attempts: list[AttemptRecord]) -> None:
        self.source = source
        self.attempts = attempts
        trail = "\n".join(f"  Attempt {a.attempt}: {a.error}" for a in attempts)
        super().__init__(
            f"Extraction failed after {len(attempts)} attempts for '{source}'.\n"
            f"Failure trail:\n{trail}"
        )

    @property
    def last_raw_response(self) -> str:
        """The raw Copilot CLI output from the final (still-failing) attempt."""
        return self.attempts[-1].raw_response if self.attempts else ""

    def write_debug_files(self, debug_dir: Path) -> list[Path]:
        """Write every attempt's raw CLI output to disk for inspection.

        One file per attempt: ``<debug_dir>/<source-stem>.attempt<N>.raw.txt``.
        Returns the written paths in attempt order.
        """
        debug_dir = Path(debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(self.source).stem
        paths = []
        for a in self.attempts:
            path = debug_dir / f"{stem}.attempt{a.attempt}.raw.txt"
            body = a.raw_response
            if a.error:
                body = f"# validation error:\n# {a.error}\n\n{body}"
            path.write_text(body, encoding="utf-8")
            paths.append(path)
        return paths


# Extractor


class Extractor:
    """Runs the extract → validate → retry loop.

    Parameters
    ----------
    bridge:
        A configured CopilotBridge instance.
    output_dir:
        Directory to write validated YAML schemas to.
    max_retries:
        Maximum number of extraction attempts (default 3).
    """

    def __init__(
        self,
        bridge: CopilotBridge,
        output_dir: Path,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.bridge = bridge
        self.output_dir = Path(output_dir)
        self.max_retries = max_retries
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def extract_file(self, source_path: Path) -> list[TableSchema]:
        """Extract a Confluence/free-text file into validated TableSchemas.

        The document may describe one table or many; the Copilot response is
        a strict ``tables:`` bundle.  A bare single-table document (legacy
        format) is also accepted.

        Parameters
        ----------
        source_path:
            Path to the requirements markdown file.

        Returns
        -------
        list[TableSchema]
            The validated schemas, one per table, each written to output/.

        Raises
        ------
        ExtractionExhaustedError
            If all retry attempts fail validation.
        CopilotCLIError
            If the Copilot CLI subprocess itself fails (not a validation error).
        """
        source_path = Path(source_path)
        confluence_md = source_path.read_text(encoding="utf-8")

        attempts: list[AttemptRecord] = []
        latest_error: str = ""

        for attempt_num in range(1, self.max_retries + 1):
            logger.info(
                "Extraction attempt %d/%d for '%s'",
                attempt_num,
                self.max_retries,
                source_path.name,
            )

            # Build error feedback — latest only, not cumulative.
            error_feedback = ""
            if latest_error:
                error_feedback = (
                    f"IMPORTANT — your previous attempt failed Pydantic validation "
                    f"with this error:\n{latest_error}\n\n"
                    f"Fix these issues in your response."
                )

            # Call Copilot CLI.
            response: CopilotResponse = self.bridge.extract(
                confluence_markdown=confluence_md,
                error_feedback=error_feedback,
            )

            # Parse YAML.
            try:
                parsed = yaml.safe_load(response.yaml_content)
            except yaml.YAMLError as exc:
                error_msg = f"YAML parse error: {exc}"
                logger.warning("Attempt %d: %s", attempt_num, error_msg)
                attempts.append(
                    AttemptRecord(
                        attempt=attempt_num,
                        raw_response=response.raw_output,
                        yaml_content=response.yaml_content,
                        error=error_msg,
                    )
                )
                latest_error = error_msg
                continue

            # Validate against Pydantic schema (strict — extra keys rejected).
            try:
                if not isinstance(parsed, dict):
                    raise ValueError(
                        "Response must be a YAML mapping with a top-level 'tables' list"
                    )

                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if "tables" in parsed:
                    for entry in parsed.get("tables") or []:
                        if isinstance(entry, dict):
                            entry.setdefault("source", str(source_path))
                            entry.setdefault("extracted_at", now)
                    schemas = SchemaBundle.model_validate(parsed).tables
                else:
                    # Legacy single-table document.
                    parsed.setdefault("source", str(source_path))
                    parsed.setdefault("extracted_at", now)
                    schemas = [TableSchema.model_validate(parsed)]

            except (ValidationError, Exception) as exc:
                error_msg = str(exc)
                logger.warning("Attempt %d: validation failed: %s", attempt_num, error_msg)
                attempts.append(
                    AttemptRecord(
                        attempt=attempt_num,
                        raw_response=response.raw_output,
                        yaml_content=response.yaml_content,
                        error=error_msg,
                    )
                )
                latest_error = error_msg
                continue

            # Success.
            logger.info(
                "Extraction succeeded on attempt %d for '%s' (%d tables)",
                attempt_num,
                source_path.name,
                len(schemas),
            )
            attempts.append(
                AttemptRecord(
                    attempt=attempt_num,
                    raw_response=response.raw_output,
                    yaml_content=response.yaml_content,
                    succeeded=True,
                )
            )

            # Write output — one file per table.
            for schema in schemas:
                self._write_output(schema)
            return schemas

        # All attempts exhausted.
        raise ExtractionExhaustedError(
            source=str(source_path),
            attempts=attempts,
        )

    def extract_all(self, source_dir: Path) -> list[TableSchema]:
        """Batch-extract all .md files in a directory.

        Parameters
        ----------
        source_dir:
            Directory containing Confluence markdown files.

        Returns
        -------
        list[TableSchema]
            Successfully extracted schemas.

        Raises
        ------
        ExtractionExhaustedError
            On first file that exhausts retries (fail-fast).
        """
        source_dir = Path(source_dir)
        md_files = sorted(source_dir.glob("*.md"))

        if not md_files:
            logger.warning("No .md files found in '%s'", source_dir)
            return []

        schemas: list[TableSchema] = []
        for md_file in md_files:
            schemas.extend(self.extract_file(md_file))

        return schemas

    def _write_output(self, schema: TableSchema) -> Path:
        """Write validated schema to output/ as YAML."""
        output_path = self.output_dir / f"{schema.table_name}.yaml"

        # Serialize with Pydantic, then dump as clean YAML.
        data = schema.model_dump(mode="json", exclude_none=True)
        yaml_str = yaml.dump(
            data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

        output_path.write_text(yaml_str, encoding="utf-8")
        logger.info("Wrote validated schema to '%s'", output_path)
        return output_path
