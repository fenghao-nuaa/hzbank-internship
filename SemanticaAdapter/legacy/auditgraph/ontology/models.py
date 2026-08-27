"""Compact ontology contracts modeled after Semantica's governance layer."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PropertyConstraint:
    property_name: str
    required: bool = False
    value_type: str | None = None
    allowed_values: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class OntologyClass:
    name: str
    constraints: list[PropertyConstraint] = field(default_factory=list)
    parent: str | None = None


@dataclass(frozen=True, slots=True)
class Ontology:
    ontology_id: str
    version: str
    classes: dict[str, OntologyClass]
    source_ref: str | None = None


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
