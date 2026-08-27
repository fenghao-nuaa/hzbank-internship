from dataclasses import replace
from hashlib import sha256

from semantica_adapter.adapters.memory.backend import FakeGovernanceBackend
from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditStatus,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
)


def _profile() -> AgentProfile:
    return AgentProfile(
        agent_id="amount-checker",
        name="Amount Checker",
        purpose="Reconcile amounts",
        profile_version="1.0",
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        ontology_id="banking",
        ontology_version="1.0",
        allowed_source_types=("ledger",),
        approval_policy="always",
        required_fields=("declared_amount", "ledger_amount"),
    )


def test_backend_round_trips_governance_records(tmp_path) -> None:
    backend = FakeGovernanceBackend()
    profile = _profile()
    evidence = EvidenceRef(
        "ledger-1",
        "ledger",
        "ledger://entry/1",
        sha256(b"100.00").hexdigest(),
    )
    assert backend.record_profile_snapshot("audit-1", profile)
    assert backend.record_evidence("audit-1", evidence) == "ledger-1"

    evaluation = backend.evaluate_rules("audit-1", profile, {"declared_amount": 100})
    assert evaluation.missing_fields == ("ledger_amount",)

    complete = replace(evaluation, missing_fields=())
    decision = DecisionRecord(
        decision_id="decision-1",
        audit_id="audit-1",
        agent_id=profile.agent_id,
        profile_version=profile.profile_version,
        category="amount_reconciliation",
        scenario="Compare declared and ledger amounts",
        outcome="matched",
        reasoning_summary="Amounts match",
        confidence=1.0,
        evidence_ids=(evidence.evidence_id,),
        rule_evaluation=complete,
        backend_name=backend.name,
        backend_version=backend.version,
        status=AuditStatus.PENDING_APPROVAL,
    )
    assert backend.record_decision(decision) == decision.decision_id

    approval = ApprovalRecord(
        "approval-1",
        decision.decision_id,
        "risk-manager",
        "reviewer",
        "approve",
        "email",
        "evidence reviewed",
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
    assert backend.record_approval(approval) == "approval-1"
    assert backend.record_exception(exception) == "exception-1"

    trace = backend.trace_decision(decision.decision_id)
    assert trace.agent_id == profile.agent_id
    assert trace.evidence == (evidence,)
    assert trace.approvals == (approval,)
    assert trace.exceptions == (exception,)

    exported = backend.export_decision(decision.decision_id, tmp_path, "json")
    assert exported.path.is_file()
    assert sha256(exported.path.read_bytes()).hexdigest() == exported.sha256
