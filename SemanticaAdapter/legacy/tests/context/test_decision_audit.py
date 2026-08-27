import pytest

from auditgraph.context import ContextGraph, DecisionRecorder
from auditgraph.core.models import Approval, Decision, PolicyException
from auditgraph.provenance import InMemoryStorage, ProvenanceManager, SQLiteStorage


@pytest.fixture()
def recorder() -> DecisionRecorder:
    graph = ContextGraph()
    graph.add_node("fact:1", "fact", risk_score=82)
    graph.add_node("POL-001:1.0", "policy", policy_id="POL-001", version="1.0")
    return DecisionRecorder(graph, ProvenanceManager(InMemoryStorage()))


def test_decision_links_evidence_rule_and_approval(recorder: DecisionRecorder) -> None:
    decision = recorder.record_decision(
        Decision(
            category="credit_review",
            scenario="Application A-1",
            reasoning="POL-001 matched risk_score 82",
            outcome="manual_review",
            confidence=1.0,
        ),
        evidence_ids=["fact:1"],
        rule_refs=[("POL-001", "1.0")],
    )
    approval = recorder.record_approval(
        Approval(
            decision_id=decision.decision_id,
            approver="risk_manager",
            method="system",
            context="reviewed evidence",
        )
    )

    edges = recorder.graph.to_kg_dict()["relationships"]
    assert {edge["type"] for edge in edges} >= {"USED_EVIDENCE", "APPLIED_POLICY", "HAS_APPROVAL"}
    assert recorder.get_approvals(decision.decision_id)[0].approval_id == approval.approval_id
    assert recorder.provenance.trace(decision.decision_id)[-1].entity_id == "fact:1"


def test_policy_exception_is_queryable(recorder: DecisionRecorder) -> None:
    decision = recorder.record_decision(
        Decision("credit_review", "Application A-2", "policy exception", "approved", 0.8)
    )
    exception = recorder.record_policy_exception(
        PolicyException(
            decision_id=decision.decision_id,
            policy_id="POL-001",
            reason="verified compensating control",
            approver="chief_risk_officer",
            justification="cash collateral confirmed",
        )
    )
    assert recorder.get_policy_exceptions(decision.decision_id)[0].exception_id == exception.exception_id


def test_approval_rejects_unknown_decision(recorder: DecisionRecorder) -> None:
    with pytest.raises(KeyError, match="decision"):
        recorder.record_approval(Approval("missing", "risk_manager", "email"))


def test_hash_chain_detects_tampering() -> None:
    storage = InMemoryStorage()
    provenance = ProvenanceManager(storage)
    provenance.track("source:1", "source", {"uri": "file://policy.txt"})
    provenance.track(
        "decision:1",
        "decision",
        {"outcome": "review"},
        derived_from=["source:1"],
    )
    assert provenance.verify_chain().valid is True
    storage.unsafe_update_payload("decision:1", {"outcome": "approve"})
    result = provenance.verify_chain()
    assert result.valid is False
    assert result.errors[0]["reason"] == "checksum_mismatch"


def test_invalidation_appends_tombstone() -> None:
    provenance = ProvenanceManager(InMemoryStorage())
    provenance.track("fact:1", "fact", {"value": 82})
    tombstone = provenance.invalidate("fact:1", agent_id="reviewer", reason="source corrected")
    assert tombstone.invalidated is True
    assert len(provenance.storage.all()) == 2
    assert provenance.verify_chain().valid is True
    lineage = provenance.trace("fact:1")
    assert len(lineage) == 2
    assert lineage[0].invalidated is True
    assert lineage[1].invalidated is False


def test_sqlite_storage_round_trip(tmp_path) -> None:
    path = tmp_path / "audit.db"
    first = ProvenanceManager(SQLiteStorage(path))
    first.track("source:1", "source", {"uri": "file://policy.txt"})
    first.storage.close()

    second = ProvenanceManager(SQLiteStorage(path))
    assert second.verify_chain().valid is True
    assert second.storage.all()[0].entity_id == "source:1"
    second.storage.close()
