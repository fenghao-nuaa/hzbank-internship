"""Ontology models and validation."""

from .models import Ontology, OntologyClass, PropertyConstraint, ValidationResult
from .validator import OntologyValidator

__all__ = ["Ontology", "OntologyClass", "OntologyValidator", "PropertyConstraint", "ValidationResult"]
