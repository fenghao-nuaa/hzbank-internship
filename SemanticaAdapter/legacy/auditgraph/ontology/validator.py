"""SHACL-like validation for the subset needed by the MVP."""

from numbers import Number
from typing import Any

from auditgraph.context import ContextGraph

from .models import Ontology, PropertyConstraint, ValidationResult


class OntologyValidator:
    _TYPES: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "number": (Number,),
        "integer": (int,),
        "boolean": (bool,),
        "list": (list, tuple, set),
        "object": (dict,),
    }

    def validate(self, graph: ContextGraph, ontology: Ontology) -> ValidationResult:
        errors: list[dict[str, Any]] = []
        for node in graph.nodes.values():
            class_definition = ontology.classes.get(node.node_type)
            if not class_definition:
                continue
            for constraint in class_definition.constraints:
                self._validate_constraint(node.node_id, node.properties, constraint, errors)
        return ValidationResult(valid=not errors, errors=errors)

    def _validate_constraint(
        self,
        node_id: str,
        properties: dict[str, Any],
        constraint: PropertyConstraint,
        errors: list[dict[str, Any]],
    ) -> None:
        name = constraint.property_name
        if name not in properties:
            if constraint.required:
                errors.append({"node_id": node_id, "property": name, "error": "required property missing"})
            return
        value = properties[name]
        expected_types = self._TYPES.get(constraint.value_type or "")
        if constraint.value_type and expected_types is None:
            raise ValueError(f"unsupported ontology value_type: {constraint.value_type}")
        if expected_types and not isinstance(value, expected_types):
            errors.append(
                {
                    "node_id": node_id,
                    "property": name,
                    "error": f"expected {constraint.value_type}",
                    "actual": type(value).__name__,
                }
            )
        if constraint.allowed_values and value not in constraint.allowed_values:
            errors.append(
                {"node_id": node_id, "property": name, "error": "value not allowed", "actual": value}
            )
