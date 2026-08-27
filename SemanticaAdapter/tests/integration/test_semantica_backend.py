from dataclasses import replace
from hashlib import sha256

import pytest

from semantica_adapter.adapters.semantica import SemanticaBackend, SemanticaConfig
from semantica_adapter.domain.errors import BackendError
from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditStatus,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
)
from semantica_adapter.ports import GovernanceBackend


def _profile() -> AgentProfile:
    return AgentProfile(
        agent_id="amount-checker",
        name="Amount Checker",
        purpose="Reconcile an application amount with the bank ledger",
        profile_version="1.0",
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        ontology_id="banking",
        ontology_version="1.0",
        allowed_source_types=("ledger",),
        approval_policy="manual_on_review",
        required_fields=("declared_amount", "ledger_amount"),
        rules={
            "reasoner_rules": (
                "IF amount_mismatch THEN manual_review",
                "IF amount_match THEN auto_pass",
            )
        },
        ontology={"types": {"declared_amount": "int", "ledger_amount": "int"}},
    )


def test_semantica_backend_records_explainable_governance_graph(tmp_path) -> None:
    backend = SemanticaBackend(
        SemanticaConfig(provenance_storage_path=tmp_path / "provenance.db")
    )
    assert isinstance(backend, GovernanceBackend)
    assert backend.health_check()["healthy"] is True

    profile = _profile()
    profile_node = backend.record_profile_snapshot("audit-1", profile)
    evidence = EvidenceRef(
        evidence_id="ledger-1",
        source_type="ledger",
        source_uri="ledger://entry/1",
        content_hash=sha256(b"100").hexdigest(),
    )
    assert backend.record_evidence("audit-1", evidence) == evidence.evidence_id
    assert backend.graph.get_node_attributes(profile_node)["rule_set_version"] == "2026.08"

    assert backend.validate_ontology(
        "audit-1", profile, {"declared_amount": "100", "ledger_amount": 100}
    ) == ("declared_amount must be int",)
    evaluation = backend.evaluate_rules(
        "audit-1", profile, {"declared_amount": 101, "ledger_amount": 100}
    )
    assert evaluation.conclusions == ("manual_review",)
    assert evaluation.matched_rules == ("rule_1",)
    assert "amount_mismatch" in evaluation.explanation_steps[0]

    decision = DecisionRecord(
        decision_id="decision-1",
        audit_id="audit-1",
        agent_id=profile.agent_id,
        profile_version=profile.profile_version,
        category="amount_reconciliation",
        scenario="Compare declared and ledger amounts",
        outcome="manual_review",
        reasoning_summary="The declared amount differs from the ledger amount.",
        confidence=1.0,
        evidence_ids=(evidence.evidence_id,),
        rule_evaluation=evaluation,
        backend_name=backend.name,
        backend_version=backend.version,
        status=AuditStatus.PENDING_APPROVAL,
    )
    assert backend.record_decision(decision) == decision.decision_id
    assert backend.graph.get_node_attributes(decision.decision_id)["outcome"] == "manual_review"

    approval = ApprovalRecord(
        "approval-1",
        decision.decision_id,
        "risk-manager",
        "reviewer",
        "approve",
        "email",
        "ledger evidence reviewed",
    )
    exception = PolicyExceptionRecord(
        "exception-1",
        decision.decision_id,
        "POL-1",
        "temporary override",
        "risk-manager",
        "email",
        "compensating control applied",
    )
    assert backend.record_approval(approval) == approval.approval_id
    assert backend.record_exception(exception) == exception.exception_id

    trace = backend.trace_decision(decision.decision_id)
    assert trace.evidence == (evidence,)
    assert trace.approvals == (approval,)
    assert trace.exceptions == (exception,)
    node_types = {node["type"] for node in trace.nodes}
    assert {"Decision", "ApprovalChain", "Exception"} <= node_types

    json_export = backend.export_decision(decision.decision_id, tmp_path, "json")
    rdf_export = backend.export_decision(decision.decision_id, tmp_path, "turtle")
    assert json_export.path.read_text(encoding="utf-8").startswith("{")
    rdf_text = rdf_export.path.read_text(encoding="utf-8")
    assert "@prefix" in rdf_text
    assert "http://www.w3.org/ns/prov#" in rdf_text
    assert sha256(json_export.path.read_bytes()).hexdigest() == json_export.sha256
    assert sha256(rdf_export.path.read_bytes()).hexdigest() == rdf_export.sha256


def test_semantica_backend_reports_missing_required_fields(tmp_path) -> None:
    backend = SemanticaBackend(
        SemanticaConfig(provenance_storage_path=tmp_path / "provenance.db")
    )
    evaluation = backend.evaluate_rules("audit-2", _profile(), {"declared_amount": 100})
    assert evaluation.missing_fields == ("ledger_amount",)
    assert evaluation.conclusions == ()


def test_semantica_backend_creates_provenance_database_parent(tmp_path) -> None:
    database = tmp_path / "not-created-yet" / "provenance.db"
    backend = SemanticaBackend(SemanticaConfig(provenance_storage_path=database))
    assert backend.health_check()["healthy"] is True
    assert database.is_file()


def test_trace_and_rdf_export_are_scoped_to_one_decision(tmp_path) -> None:
    backend = SemanticaBackend(
        SemanticaConfig(provenance_storage_path=tmp_path / "provenance.db")
    )
    profile = _profile()
    for number in (1, 2):
        audit_id = f"audit-{number}"
        evidence = EvidenceRef(
            "ledger-shared",
            "ledger",
            f"ledger://entry/{number}",
            sha256(str(number).encode()).hexdigest(),
        )
        backend.record_profile_snapshot(audit_id, profile)
        backend.record_evidence(audit_id, evidence)
        evaluation = backend.evaluate_rules(
            audit_id, profile, {"declared_amount": number, "ledger_amount": number}
        )
        backend.record_decision(
            DecisionRecord(
                decision_id=f"decision-{number}",
                audit_id=audit_id,
                agent_id=profile.agent_id,
                profile_version=profile.profile_version,
                category="amount_reconciliation",
                scenario="isolation test",
                outcome="auto_pass",
                reasoning_summary="Amounts match",
                confidence=1.0,
                evidence_ids=(evidence.evidence_id,),
                rule_evaluation=evaluation,
                backend_name=backend.name,
                backend_version=backend.version,
            )
        )

    trace = backend.trace_decision("decision-1")
    node_ids = {node["id"] for node in trace.nodes}
    assert "decision-1" in node_ids
    assert "decision-2" not in node_ids
    evidence_node = next(node for node in trace.nodes if node["type"] == "Evidence")
    assert evidence_node["properties"]["source_uri"] == "ledger://entry/1"
    second_trace = backend.trace_decision("decision-2")
    second_evidence_node = next(
        node for node in second_trace.nodes if node["type"] == "Evidence"
    )
    assert second_evidence_node["properties"]["source_uri"] == "ledger://entry/2"
    rdf = backend.export_decision("decision-1", tmp_path, "turtle")
    rdf_text = rdf.path.read_text(encoding="utf-8")
    assert "decision-1" in rdf_text
    assert "decision-2" not in rdf_text
    assert "ledger://entry/2" not in rdf_text


def test_semantica_runtime_errors_are_translated(tmp_path, monkeypatch) -> None:
    backend = SemanticaBackend(
        SemanticaConfig(provenance_storage_path=tmp_path / "provenance.db")
    )
    profile = _profile()
    backend.record_profile_snapshot("audit-1", profile)
    evaluation = backend.evaluate_rules(
        "audit-1", profile, {"declared_amount": 1, "ledger_amount": 1}
    )
    decision = DecisionRecord(
        decision_id="decision-error",
        audit_id="audit-1",
        agent_id=profile.agent_id,
        profile_version=profile.profile_version,
        category="amount_reconciliation",
        scenario="backend error",
        outcome="auto_pass",
        reasoning_summary="test",
        confidence=1.0,
        evidence_ids=(),
        rule_evaluation=evaluation,
        backend_name=backend.name,
        backend_version=backend.version,
    )

    def fail(*args, **kwargs):
        raise RuntimeError("Semantica failed")

    monkeypatch.setattr(backend.recorder, "record_decision", fail)
    with pytest.raises(BackendError, match="record_decision"):
        backend.record_decision(decision)
