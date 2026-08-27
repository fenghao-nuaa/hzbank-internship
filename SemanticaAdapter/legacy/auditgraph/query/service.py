"""Read-only query service over ContextGraph and provenance."""

from typing import Any

from auditgraph.context import ContextGraph
from auditgraph.provenance import ProvenanceManager


class QueryService:
    VIOLATION_TYPES = {"VIOLATES", "BREACHES", "NON_COMPLIANT"}

    def __init__(self, graph: ContextGraph, provenance: ProvenanceManager) -> None:
        self.graph = graph
        self.provenance = provenance

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        node = self.graph.get_node(decision_id)
        if node is None or node["type"] != "decision":
            raise KeyError(f"unknown decision: {decision_id}")
        return node

    def get_causal_chain(self, decision_id: str) -> list[dict[str, Any]]:
        return self.graph.trace_decision_chain(decision_id)

    def get_lineage(self, entity_id: str) -> list[dict[str, Any]]:
        return [
            {
                "sequence_id": entry.sequence_id,
                "entity_id": entry.entity_id,
                "entity_type": entry.entity_type,
                "derived_from": list(entry.derived_from),
                "checksum": entry.checksum,
                "invalidated": entry.invalidated,
            }
            for entry in self.provenance.trace(entity_id)
        ]

    def compliance_report(self, decision_id: str) -> dict[str, Any]:
        self.get_decision(decision_id)
        violations = [
            edge.to_dict()
            for edge in self.graph.edges
            if edge.source == decision_id and edge.edge_type in self.VIOLATION_TYPES
        ]
        approvals = [
            edge.to_dict()
            for edge in self.graph.edges
            if edge.source == decision_id and edge.edge_type == "HAS_APPROVAL"
        ]
        return {
            "decision_id": decision_id,
            "compliant": not violations and bool(approvals),
            "violations": violations,
            "approval_count": len(approvals),
            "audit_chain": self.provenance.verify_chain().valid,
        }
