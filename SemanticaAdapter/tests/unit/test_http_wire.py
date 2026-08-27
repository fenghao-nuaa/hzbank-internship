from datetime import datetime, timezone
from hashlib import sha256

from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditRequest,
    AuditSession,
    AuditStatus,
    AuditTrace,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
    RuleEvaluation,
)
from semantica_adapter.http.wire import (
    agent_profile_from_wire,
    approval_from_wire,
    audit_request_from_wire,
    audit_session_from_wire,
    decision_from_wire,
    exception_from_wire,
    to_wire,
    trace_from_wire,
)


NOW = datetime(2026, 8, 26, 9, 30, tzinfo=timezone.utc)


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
        allowed_source_types=("ledger", "voucher"),
        approval_policy="manual_on_review",
        required_fields=("declared_amount", "ledger_amount"),
        rules={"threshold": 0},
        ontology={"types": {"declared_amount": "int"}},
        metadata={"owner": "finance"},
    )


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="ledger-1",
        source_type="ledger",
        source_uri="ledger://entry/1",
        content_hash=sha256(b"100").hexdigest(),
        observed_at=NOW,
        metadata={"system": "core-ledger"},
    )


def _evaluation() -> RuleEvaluation:
    return RuleEvaluation(
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        matched_rules=("AMOUNT_MATCH",),
        conclusions=("auto_pass",),
        explanation_steps=("declared_amount equals ledger_amount",),
    )


def test_profile_and_request_round_trip() -> None:
    profile = _profile()
    request = AuditRequest(
        request_id="request-1",
        agent_id=profile.agent_id,
        task_type="amount_reconciliation",
        inputs={"declared_amount": 100, "ledger_amount": 100},
        evidence=(_evidence(),),
        requested_at=NOW,
        correlation_id="corr-1",
    )

    assert agent_profile_from_wire(to_wire(profile)) == profile
    assert audit_request_from_wire(to_wire(request)) == request


def test_audit_session_round_trip_preserves_enum_and_nested_models() -> None:
    request = AuditRequest(
        "request-1",
        "amount-checker",
        "amount_reconciliation",
        {"declared_amount": 100, "ledger_amount": 100},
        (_evidence(),),
        NOW,
    )
    session = AuditSession(
        audit_id="audit:request-1",
        request=request,
        profile=_profile(),
        status=AuditStatus.EVALUATED,
        rule_evaluation=_evaluation(),
        decision_id="decision:audit:request-1",
    )

    encoded = to_wire(session)
    restored = audit_session_from_wire(encoded)

    assert restored == session
    assert type(encoded["status"]) is str
    assert restored.status is AuditStatus.EVALUATED
    assert restored.request.requested_at.tzinfo is not None


def test_decision_approval_exception_and_trace_round_trip() -> None:
    decision = DecisionRecord(
        decision_id="decision-1",
        audit_id="audit-1",
        agent_id="amount-checker",
        profile_version="1.0",
        category="amount_reconciliation",
        scenario="Compare amounts",
        outcome="manual_review",
        reasoning_summary="Amounts differ",
        confidence=1.0,
        evidence_ids=("ledger-1",),
        rule_evaluation=_evaluation(),
        backend_name="semantica",
        backend_version="0.6.6",
        created_at=NOW,
        status=AuditStatus.PENDING_APPROVAL,
    )
    approval = ApprovalRecord(
        "approval-1",
        decision.decision_id,
        "risk-manager",
        "reviewer",
        "approve",
        "email",
        "evidence reviewed",
        NOW,
        {"ticket": "T-1"},
    )
    exception = PolicyExceptionRecord(
        "exception-1",
        decision.decision_id,
        "POL-1",
        "approved discrepancy",
        "risk-manager",
        "email",
        "secondary control",
        NOW,
        {"approver_role": "reviewer"},
    )
    trace = AuditTrace(
        decision_id=decision.decision_id,
        agent_id=decision.agent_id,
        profile_version=decision.profile_version,
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        decision_status=AuditStatus.APPROVED,
        evidence=(_evidence(),),
        nodes=({"id": decision.decision_id, "type": "Decision"},),
        approvals=(approval,),
        exceptions=(exception,),
    )

    assert decision_from_wire(to_wire(decision)) == decision
    assert approval_from_wire(to_wire(approval)) == approval
    assert exception_from_wire(to_wire(exception)) == exception
    assert trace_from_wire(to_wire(trace)) == trace
