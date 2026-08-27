from datetime import datetime, timedelta, timezone

import pytest

from auditgraph.conflicts import ConflictDetector
from auditgraph.context import ContextGraph
from auditgraph.core.models import Entity, ExtractionResult, Relation, Triplet
from auditgraph.deduplication import EntityResolver
from auditgraph.kg import KnowledgeGraph


def test_conflicts_are_flagged_without_overwrite() -> None:
    triplets = [
        Triplet("A-1", "risk_level", "medium", "crm"),
        Triplet("A-1", "risk_level", "high", "core"),
    ]
    conflicts = ConflictDetector().detect(triplets)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "value"
    assert set(conflicts[0].values) == {"medium", "high"}
    assert conflicts[0].source_ids == {"crm", "core"}
    assert [triplet.object for triplet in triplets] == ["medium", "high"]


def test_aliases_merge_and_keep_sources() -> None:
    entities = [
        Entity("bank-1", "Acme Bank", "Organization", aliases={"ACME"}, source_ids={"crm"}),
        Entity("bank-2", "  ACME   BANK ", "Organization", aliases={"Acme"}, source_ids={"core"}),
    ]
    resolved = EntityResolver().resolve(entities)
    assert len(resolved) == 1
    assert resolved[0].source_ids == {"crm", "core"}
    assert {"ACME", "Acme"}.issubset(resolved[0].aliases)


def test_entity_resolution_returns_referential_mapping() -> None:
    entities = [
        Entity("A-1", "Application A-1", "LoanApplication", source_ids={"database"}),
        Entity("A-ALIAS", "Application A-1", "LoanApplication", source_ids={"api"}),
    ]
    resolved, mapping = EntityResolver().resolve_with_mapping(entities)
    assert len(resolved) == 1
    assert mapping == {"A-1": "A-1", "A-ALIAS": "A-1"}


def test_knowledge_graph_builds_from_extraction() -> None:
    extraction = ExtractionResult(
        entities=[
            Entity("A-1", "Application A-1", "LoanApplication", source_ids={"file:1"}),
            Entity("POL-1", "Risk Policy", "Policy", source_ids={"file:1"}),
        ],
        relations=[Relation("r-1", "A-1", "governed_by", "POL-1", "file:1")],
        triplets=[Triplet("A-1", "risk_score", 82, "file:1")],
    )
    graph = KnowledgeGraph.from_extraction(extraction)
    assert graph.get_node("A-1")["properties"]["risk_score"] == 82
    assert graph.get_neighbors("A-1")[0]["id"] == "POL-1"


def test_context_graph_traces_explicit_causal_edges() -> None:
    graph = ContextGraph()
    graph.add_node("d1", "decision", outcome="review")
    graph.add_node("d2", "decision", outcome="approved")
    graph.add_causal_relationship("d1", "d2", "CAUSED")
    chain = graph.trace_decision_chain("d2")
    assert chain[0]["source"] == "d1"
    assert chain[0]["target"] == "d2"


def test_context_graph_rejects_unknown_causal_type() -> None:
    graph = ContextGraph()
    graph.add_node("d1", "decision")
    graph.add_node("d2", "decision")
    with pytest.raises(ValueError, match="relationship_type"):
        graph.add_causal_relationship("d1", "d2", "MAYBE")


def test_context_graph_supports_historical_state() -> None:
    graph = ContextGraph()
    before = datetime.now(timezone.utc)
    graph.add_node("n1", "entity")
    after = datetime.now(timezone.utc) + timedelta(microseconds=1)
    assert graph.state_at(before)["nodes"] == []
    assert graph.state_at(after)["nodes"][0]["id"] == "n1"
