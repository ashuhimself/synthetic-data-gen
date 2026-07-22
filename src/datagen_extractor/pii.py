"""PII detection layer — pattern-based sensitive-column tagging.

Scans validated ``TableSchema`` documents and flags fields that look like
PII based on name/format patterns.  Two outcomes per finding:

- field already has ``pii: true`` → confirmed (will be synthesized safely),
- field matches a pattern but is NOT tagged → untagged risk, surfaced for
  review before any generation run.

Default patterns are generic across schemas (C-3); a custom pattern set can
be supplied as YAML to extend or replace them per deployment.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from datagen_extractor.schema import TableSchema

logger = logging.getLogger(__name__)


# (category, field-name regex, format regex or None) — matched case-insensitively.
DEFAULT_PATTERNS: list[tuple[str, str, str | None]] = [
    ("name", r"(first|last|middle|full|sur|given|maiden)[_ ]?name", None),
    ("national_id", r"\b(ssn|sin|nino|aadhaar|tax[_ ]?id|national[_ ]?id)\b", None),
    ("email", r"e[-_]?mail", r"email"),
    ("phone", r"(phone|mobile|cell|fax)", r"E\.164"),
    ("date_of_birth", r"(dob|date[_ ]?of[_ ]?birth|birth[_ ]?date)", None),
    ("address", r"(address|street|city|zip|post[_ ]?code|postal)", None),
    ("account_secret", r"(password|secret|pin|cvv|card[_ ]?number|iban)", None),
]


@dataclass
class PIIFinding:
    """One field flagged by the PII scan."""

    table: str
    column: str
    category: str
    tagged: bool  # True if the schema already declares pii: true


@dataclass
class PIIReport:
    """Scan result across a schema set."""

    findings: list[PIIFinding]

    @property
    def untagged(self) -> list[PIIFinding]:
        """Fields matching a PII pattern but missing pii: true — review these."""
        return [f for f in self.findings if not f.tagged]

    @property
    def tagged(self) -> list[PIIFinding]:
        return [f for f in self.findings if f.tagged]


def load_patterns(path: Path) -> list[tuple[str, str, str | None]]:
    """Load custom patterns from YAML: a list of {category, name_pattern, format_pattern?}."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Pattern file '{path}' must contain a YAML list")
    patterns = []
    for entry in raw:
        patterns.append((entry["category"], entry["name_pattern"], entry.get("format_pattern")))
    return patterns


def scan_schemas(
    schemas: list[TableSchema],
    patterns: list[tuple[str, str, str | None]] | None = None,
) -> PIIReport:
    """Scan every field in every schema against the PII pattern set.

    A field is flagged when its name matches a name pattern, or its declared
    format matches a format pattern.  Fields already tagged ``pii: true``
    that match no pattern are still reported (tagged, category "declared") so
    the report reflects the full sensitive surface.
    """
    patterns = patterns if patterns is not None else DEFAULT_PATTERNS
    compiled = [
        (
            cat,
            re.compile(name_re, re.IGNORECASE),
            re.compile(fmt_re, re.IGNORECASE) if fmt_re else None,
        )
        for cat, name_re, fmt_re in patterns
    ]

    findings: list[PIIFinding] = []
    for schema in schemas:
        for field in schema.fields:
            matched_category = None
            for cat, name_re, fmt_re in compiled:
                if name_re.search(field.name):
                    matched_category = cat
                    break
                if fmt_re and field.format and fmt_re.search(field.format):
                    matched_category = cat
                    break
            if matched_category:
                findings.append(
                    PIIFinding(
                        table=schema.table_name,
                        column=field.name,
                        category=matched_category,
                        tagged=field.pii,
                    )
                )
            elif field.pii:
                findings.append(
                    PIIFinding(
                        table=schema.table_name,
                        column=field.name,
                        category="declared",
                        tagged=True,
                    )
                )

    logger.info(
        "PII scan: %d findings (%d untagged)",
        len(findings),
        sum(1 for f in findings if not f.tagged),
    )
    return PIIReport(findings=findings)
