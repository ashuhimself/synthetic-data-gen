"""Standalone re-validation of existing YAML schema files.

Use cases:
- Post-hoc validation after manual edits to extracted YAML.
- CI/CD gate: validate all schemas in output/ before downstream consumption.
- Schema evolution: re-validate after Pydantic model changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from datagen_extractor.schema import TableSchema

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validating a single YAML schema file."""

    path: Path
    valid: bool
    schema: TableSchema | None = None
    error: str | None = None


def validate_file(path: Path) -> ValidationResult:
    """Validate a single YAML schema file against the Pydantic model.

    Parameters
    ----------
    path:
        Path to the YAML schema file.

    Returns
    -------
    ValidationResult
        Contains the parsed schema on success, or the error message on failure.
    """
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationResult(path=path, valid=False, error=f"Read error: {exc}")

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return ValidationResult(path=path, valid=False, error=f"YAML parse error: {exc}")

    if parsed is None:
        return ValidationResult(path=path, valid=False, error="YAML file is empty")

    try:
        schema = TableSchema.model_validate(parsed)
    except ValidationError as exc:
        return ValidationResult(path=path, valid=False, error=str(exc))

    logger.info("Validated '%s': %d fields", path.name, len(schema.fields))
    return ValidationResult(path=path, valid=True, schema=schema)


def validate_directory(directory: Path) -> list[ValidationResult]:
    """Validate all .yaml files in a directory.

    Parameters
    ----------
    directory:
        Directory containing YAML schema files.

    Returns
    -------
    list[ValidationResult]
        One result per file found.
    """
    directory = Path(directory)
    yaml_files = sorted(directory.glob("*.yaml"))

    if not yaml_files:
        logger.warning("No .yaml files found in '%s'", directory)
        return []

    return [validate_file(f) for f in yaml_files]
