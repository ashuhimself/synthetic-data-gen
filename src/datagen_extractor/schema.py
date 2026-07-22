"""Pydantic models for the extraction schema contract.

Every field extracted from a Confluence data contract must conform to these
models. Validation is strict — partial schemas are rejected.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# Allowed type set — expand as new source contracts introduce new types.
ALLOWED_TYPES: frozenset[str] = frozenset(
    {
        "string",
        "integer",
        "decimal",
        "date",
        "datetime",
        "boolean",
        "uuid",
        "enum",
        "text",
        "timestamp",
    }
)

# Cardinality declarations allowed on an FK field:
#   "N:1"  — many children per parent (default FK semantics)
#   "1:1"  — exactly one child per parent
#   "avg:<int>"       — ratio hint: average children per parent
#   "range:<lo>-<hi>" — ratio hint: children per parent within [lo, hi]
CARDINALITY_PATTERN = re.compile(r"^(N:1|1:1|avg:\d+|range:\d+-\d+)$")

# Character-set profiles for text columns.
ALLOWED_CHARSETS: frozenset[str] = frozenset({"ascii", "unicode"})

# charset / control_char_rate only make sense on free-text column types.
TEXT_TYPES: frozenset[str] = frozenset({"string", "text"})


class Field(BaseModel):
    """Canonical descriptor for a single column / field in a table schema.

    ``extra="forbid"``: unknown keys are a validation error, so the Copilot
    retry loop corrects format drift instead of silently accepting it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    format: Optional[str] = None
    distribution: Optional[str] = None
    min: Optional[Any] = None
    max: Optional[Any] = None
    nullable: bool = False
    null_rate: Optional[float] = None
    charset: Optional[str] = None
    control_char_rate: Optional[float] = None
    unique: bool = False
    pii: bool = False
    fk_ref: Optional[str] = None
    cardinality: Optional[str] = None
    confidence: float = 1.0
    note: Optional[str] = None

    # validators

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        return v

    @field_validator("type")
    @classmethod
    def type_must_be_known(cls, v: str) -> str:
        normalised = v.strip().lower()
        if normalised not in ALLOWED_TYPES:
            raise ValueError(f"type '{v}' not in allowed set: {sorted(ALLOWED_TYPES)}")
        return normalised

    @model_validator(mode="after")
    def text_profile_requires_text_type(self) -> "Field":
        if self.charset is not None:
            if self.charset not in ALLOWED_CHARSETS:
                raise ValueError(
                    f"charset '{self.charset}' not in allowed set: {sorted(ALLOWED_CHARSETS)}"
                )
            if self.type not in TEXT_TYPES:
                raise ValueError(
                    f"charset set on '{self.name}' but type '{self.type}' is "
                    f"not a text type ({sorted(TEXT_TYPES)})"
                )
        if self.control_char_rate is not None:
            if not 0.0 <= self.control_char_rate <= 1.0:
                raise ValueError("control_char_rate must be between 0.0 and 1.0")
            if self.type not in TEXT_TYPES:
                raise ValueError(
                    f"control_char_rate set on '{self.name}' but type "
                    f"'{self.type}' is not a text type ({sorted(TEXT_TYPES)})"
                )
        return self

    @model_validator(mode="after")
    def null_rate_requires_nullable(self) -> "Field":
        if self.null_rate is not None:
            if not 0.0 <= self.null_rate <= 1.0:
                raise ValueError("null_rate must be between 0.0 and 1.0")
            if not self.nullable:
                raise ValueError(f"null_rate set on '{self.name}' but nullable is false")
        return self

    @field_validator("cardinality")
    @classmethod
    def cardinality_must_match_pattern(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not CARDINALITY_PATTERN.match(v):
            raise ValueError(
                f"cardinality '{v}' invalid — expected 'N:1', '1:1', "
                f"'avg:<int>', or 'range:<lo>-<hi>'"
            )
        return v


ALLOWED_RULE_EFFECTS: frozenset[str] = frozenset({"not_null", "null"})


class Rule(BaseModel):
    """Cross-field conditional constraint, declared in YAML (C-3).

    ``when`` maps columns to required values (all must match — AND);
    ``then`` maps columns to an effect: "not_null" or "null".

    Example: close_date must be set exactly when status is closed:
        when: {status: closed}
        then: {close_date: not_null}
    """

    model_config = ConfigDict(extra="forbid")

    when: dict[str, str]
    then: dict[str, str]

    @field_validator("then", mode="before")
    @classmethod
    def coerce_yaml_null_effect(cls, v: object) -> object:
        # YAML parses a bare `null` effect as None — normalise to the string.
        if isinstance(v, dict):
            return {k: ("null" if val is None else val) for k, val in v.items()}
        return v

    @field_validator("when", mode="before")
    @classmethod
    def coerce_when_values_to_str(cls, v: object) -> object:
        # YAML may type condition values (ints, bools); data files hold strings.
        if isinstance(v, dict):
            return {k: (val if isinstance(val, str) else str(val)) for k, val in v.items()}
        return v

    @field_validator("when", "then")
    @classmethod
    def must_not_be_empty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("rule 'when' and 'then' must not be empty")
        return v

    @field_validator("then")
    @classmethod
    def effects_must_be_known(cls, v: dict[str, str]) -> dict[str, str]:
        bad = {e for e in v.values() if e not in ALLOWED_RULE_EFFECTS}
        if bad:
            raise ValueError(
                f"rule effects {sorted(bad)} not in allowed set: {sorted(ALLOWED_RULE_EFFECTS)}"
            )
        return v


class TableSchema(BaseModel):
    """Container for a full table extracted from a Confluence data contract."""

    model_config = ConfigDict(extra="forbid")

    table_name: str
    description: Optional[str] = None
    fields: list[Field]
    unique_together: Optional[list[list[str]]] = None
    rules: Optional[list[Rule]] = None
    row_count_hint: Optional[int] = None
    source: Optional[str] = None
    extracted_at: Optional[str] = None

    @field_validator("fields")
    @classmethod
    def must_have_fields(cls, v: list[Field]) -> list[Field]:
        if len(v) == 0:
            raise ValueError("TableSchema must contain at least one field")
        return v

    @model_validator(mode="after")
    def unique_together_columns_must_exist(self) -> "TableSchema":
        if not self.unique_together:
            return self
        known = {f.name for f in self.fields}
        for group in self.unique_together:
            if len(group) < 2:
                raise ValueError(f"unique_together group {group} must contain at least 2 columns")
            missing = [c for c in group if c not in known]
            if missing:
                raise ValueError(f"unique_together references unknown columns: {missing}")
        return self

    @model_validator(mode="after")
    def rule_columns_must_exist(self) -> "TableSchema":
        if not self.rules:
            return self
        known = {f.name for f in self.fields}
        for rule in self.rules:
            missing = [c for c in list(rule.when) + list(rule.then) if c not in known]
            if missing:
                raise ValueError(f"rule references unknown columns: {missing}")
        return self


class SchemaBundle(BaseModel):
    """Multi-table extraction result: one document, many tables."""

    model_config = ConfigDict(extra="forbid")

    tables: list[TableSchema]

    @field_validator("tables")
    @classmethod
    def must_have_tables(cls, v: list[TableSchema]) -> list[TableSchema]:
        if len(v) == 0:
            raise ValueError("SchemaBundle must contain at least one table")
        names = [t.table_name for t in v]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate table names in bundle: {sorted(dupes)}")
        return v
