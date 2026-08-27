from dataclasses import replace
from hashlib import sha256

import pytest

from semantica_adapter.adapters.memory import (
    FakeGovernanceBackend,
    MemoryAgentProfileRepository,
    MemoryApprovalWorkflow,
)
from semantica_adapter.api import AgentGovernanceService
from semantica_adapter.domain.errors import ApprovalRequiredError, ValidationError
from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditRequest,
    AuditStatus,
    EvidenceRef,
    PolicyExceptionRecord,
)


def _profile(*, approval_policy: str = "manual_on_review") -> AgentProfile:
    return AgentProfile(
        agent_id="amount-checker",
        name="Amount Checker",
        purpose="Reconcile declared and ledger amounts",
        profile_version="1.0",
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
        ontology_id="banking",
        ontology_version="1.0",
        allowed_source_types=("ledger", "voucher"),
        approval_policy=approval_policy,
        required_fields=("declared_amount", "ledger_amount"),
        ontology={"types": {"declared_amount": "int", "ledger_amount": "int"}},
    )


def _request(inputs=None) -> AuditRequest:
    return AuditRequest(
        request_id="request-1",
        agent_id="amount-checker",
        task_type="amount_reconciliation",
        inputs=inputs if inputs is not None else {"declared_amount": 100, "ledger_amount": 100},
        evidence=(
            EvidenceRef(
                "ledger-1", "ledger", "ledger://entry/1", sha256(b"100").hexdigest()
            ),
        ),
    )


@pytest.fixture
def service() -> AgentGovernanceService:
    profiles = MemoryAgentProfileRepository()
    workflow = MemoryApprovalWorkflow({("risk-manager", "reviewer")})
    result = AgentGovernanceService(FakeGovernanceBackend(), profiles, workflow)
    result.register_agent(_profile())
    return result


def test_missing_required_field_forces_manual_review(service) -> None:
    audit = service.start_audit(_request({"declared_amount": 100}))
    evaluated = service.evaluate(audit.audit_id)
    assert evaluated.status is AuditStatus.MANUAL_REVIEW
    assert evaluated.rule_evaluation.missing_fields == ("ledger_amount",)


def test_ontology_error_forces_manual_review(service) -> None:
    audit = service.start_audit(_request({"declared_amount": "100", "ledger_amount": 100}))
    evaluated = service.evaluate(audit.audit_id)
    assert evaluated.status is AuditStatus.MANUAL_REVIEW
    assert evaluated.ontology_errors == ("declared_amount must be int",)


def test_conflict_forces_manual_review(service) -> None:
    audit = service.start_audit(
        _request(
            {
                "declared_amount": 100,
                "ledger_amount": 100,
                "_conflicts": ("ledger_amount has two authoritative values",),
            }
        )
    )
    evaluated = service.evaluate(audit.audit_id)
    assert evaluated.status is AuditStatus.MANUAL_REVIEW
    assert evaluated.rule_evaluation.conflicts


def test_rule_manual_review_conclusion_cannot_be_overridden(service) -> None:
    audit = service.start_audit(_request({"declared_amount": 101, "ledger_amount": 100}))
    evaluated = service.evaluate(audit.audit_id)
    assert evaluated.status is AuditStatus.MANUAL_REVIEW
    decision = service.record_decision(
        audit.audit_id,
        proposed_outcome="matched",
        reasoning_summary="Agent attempted to override the rule conclusion",
        confidence=1.0,
    )
    assert decision.outcome == "manual_review"
    assert decision.status is AuditStatus.PENDING_APPROVAL


def test_profile_cannot_remove_mandatory_adverse_conclusions() -> None:
    profiles = MemoryAgentProfileRepository()
    workflow = MemoryApprovalWorkflow({("risk-manager", "reviewer")})
    service = AgentGovernanceService(FakeGovernanceBackend(), profiles, workflow)
    unsafe_profile = replace(_profile(), rules={"manual_review_conclusions": ()})
    service.register_agent(unsafe_profile)
    audit = service.start_audit(_request({"declared_amount": 101, "ledger_amount": 100}))
    assert service.evaluate(audit.audit_id).status is AuditStatus.MANUAL_REVIEW


def test_zero_evidence_forces_manual_review(service) -> None:
    request = AuditRequest(
        request_id="request-no-evidence",
        agent_id="amount-checker",
        task_type="amount_reconciliation",
        inputs={"declared_amount": 100, "ledger_amount": 100},
        evidence=(),
    )
    audit = service.start_audit(request)
    evaluated = service.evaluate(audit.audit_id)
    assert evaluated.status is AuditStatus.MANUAL_REVIEW
    assert "no evidence supplied" in evaluated.rule_evaluation.conflicts


def test_required_human_approval_stays_pending(service) -> None:
    audit = service.start_audit(_request())
    service.evaluate(audit.audit_id)
    decision = service.record_decision(
        audit.audit_id,
        proposed_outcome="matched",
        reasoning_summary="Declared and ledger amounts match",
        confidence=1.0,
    )
    assert decision.status is AuditStatus.EVALUATED

    mismatch = service.start_audit(
        AuditRequest(
            request_id="request-2",
            agent_id="amount-checker",
            task_type="amount_reconciliation",
            inputs={"declared_amount": 101, "ledger_amount": 100},
            evidence=_request().evidence,
        )
    )
    service.evaluate(mismatch.audit_id)
    pending = service.record_decision(
        mismatch.audit_id,
        proposed_outcome="manual_review",
        reasoning_summary="Amounts differ",
        confidence=1.0,
    )
    assert pending.status is AuditStatus.PENDING_APPROVAL


def test_manual_review_cannot_be_overridden_by_agent(service) -> None:
    audit = service.start_audit(_request({"declared_amount": 100}))
    service.evaluate(audit.audit_id)
    decision = service.record_decision(
        audit.audit_id,
        proposed_outcome="matched",
        reasoning_summary="Agent proposed pass despite missing evidence",
        confidence=0.9,
    )
    assert decision.outcome == "manual_review"
    assert decision.status is AuditStatus.PENDING_APPROVAL


def test_unauthorized_actor_cannot_approve(service) -> None:
    audit = service.start_audit(_request({"declared_amount": 101, "ledger_amount": 100}))
    service.evaluate(audit.audit_id)
    pending = service.record_decision(
        audit.audit_id, "manual_review", "Amounts differ", 1.0
    )
    approval = ApprovalRecord(
        "approval-1",
        pending.decision_id,
        "agent-process",
        "agent",
        "approve",
        "system",
        "attempted self approval",
    )
    with pytest.raises(ApprovalRequiredError):
        service.submit_approval(approval)


def test_authorized_approval_and_exception_are_persisted(service) -> None:
    audit = service.start_audit(_request({"declared_amount": 101, "ledger_amount": 100}))
    service.evaluate(audit.audit_id)
    pending = service.record_decision(audit.audit_id, "manual_review", "Amounts differ", 1.0)
    exception = PolicyExceptionRecord(
        "exception-1",
        pending.decision_id,
        "POL-1",
        "approved discrepancy",
        "risk-manager",
        "email",
        "secondary control",
        metadata={"approver_role": "reviewer"},
    )
    assert service.record_exception(exception) == exception
    approval = ApprovalRecord(
        "approval-1", pending.decision_id, "risk-manager", "reviewer", "approve", "email", "reviewed"
    )
    approved = service.submit_approval(approval)
    assert approved.status is AuditStatus.APPROVED
    trace = service.get_audit_trace(pending.decision_id)
    assert trace.decision_status is AuditStatus.APPROVED
    assert trace.approvals == (approval,)
    assert trace.exceptions == (exception,)


def test_exception_cannot_be_added_after_decision_is_final(service) -> None:
    audit = service.start_audit(_request({"declared_amount": 101, "ledger_amount": 100}))
    service.evaluate(audit.audit_id)
    pending = service.record_decision(audit.audit_id, "manual_review", "Amounts differ", 1.0)
    service.submit_approval(
        ApprovalRecord(
            "approval-final", pending.decision_id, "risk-manager", "reviewer", "approve", "email", "reviewed"
        )
    )
    exception = PolicyExceptionRecord(
        "exception-final",
        pending.decision_id,
        "POL-1",
        "too late",
        "risk-manager",
        "email",
        "not allowed after finalization",
        metadata={"approver_role": "reviewer"},
    )
    with pytest.raises(ValidationError, match="pending approval"):
        service.record_exception(exception)


def test_approval_retry_is_idempotent(service) -> None:
    audit = service.start_audit(_request({"declared_amount": 101, "ledger_amount": 100}))
    service.evaluate(audit.audit_id)
    pending = service.record_decision(audit.audit_id, "manual_review", "Amounts differ", 1.0)
    approval = ApprovalRecord(
        "approval-retry", pending.decision_id, "risk-manager", "reviewer", "approve", "email", "reviewed"
    )
    first = service.submit_approval(approval)
    second = service.submit_approval(approval)
    assert first == second
    assert service.get_audit_trace(pending.decision_id).approvals == (approval,)


def test_decision_cannot_be_recorded_before_evaluation(service) -> None:
    audit = service.start_audit(_request())
    with pytest.raises(ValidationError, match="evaluated"):
        service.record_decision(audit.audit_id, "matched", "not evaluated", 1.0)
