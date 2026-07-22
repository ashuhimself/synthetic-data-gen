"""Regression guard: existing extracted schemas in output/ must still validate
after the cardinality / unique_together model extensions."""

from __future__ import annotations

from pathlib import Path

import pytest

from datagen_extractor.validate import validate_file

OUTPUT_DIR = Path(__file__).parent.parent / "output"


@pytest.mark.parametrize(
    "yaml_file",
    sorted(OUTPUT_DIR.glob("*.yaml")),
    ids=lambda p: p.name,
)
def test_existing_output_still_validates(yaml_file: Path):
    result = validate_file(yaml_file)
    assert result.valid, result.error
