from pathlib import Path

import pytest

from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditStatus,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
    RuleEvaluation,
)


def _profile() -> AgentProfile:
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
        approval_policy="manual_on_review",
        required_fields=("declared_amount", "ledger_amount"),
    )


def test_profile_requires_versioned_governance_bindings() -> None:
    profile = _profile()
    assert profile.agent_id == "amount-checker"
    assert profile.required_fields == ("declared_amount", "ledger_amount")


def test_profile_rejects_unknown_approval_policy() -> None:
    with pytest.raises(ValueError, match="approval_policy"):
        AgentProfile(
            agent_id="a",
            name="A",
            purpose="audit",
            profile_version="1",
            rule_set_id="r",
            rule_set_version="1",
            ontology_id="o",
            ontology_version="1",
            allowed_source_types=(),
            approval_policy="sometimes",
        )


def test_evidence_requires_sha256_hex_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        EvidenceRef("e-1", "ledger", "ledger://entry/1", "not-a-hash")


def test_audit_status_has_manual_review_and_pending_approval() -> None:
    assert AuditStatus.MANUAL_REVIEW.value == "manual_review"
    assert AuditStatus.PENDING_APPROVAL.value == "pending_approval"


def test_decision_validates_confidence_and_preserves_public_reasoning() -> None:
    evaluation = RuleEvaluation("amount-rules", "2026.08")
    with pytest.raises(ValueError, match="confidence"):
        DecisionRecord(
            decision_id="d-1",
            audit_id="audit-1",
            agent_id="amount-checker",
            profile_version="1.0",
            category="amount_reconciliation",
            scenario="Compare voucher and ledger",
            outcome="matched",
            reasoning_summary="Amounts are equal",
            confidence=1.1,
            evidence_ids=("e-1",),
            rule_evaluation=evaluation,
            backend_name="fake",
            backend_version="1",
        )


def test_approval_and_exception_use_semantica_compatible_methods() -> None:
    approval = ApprovalRecord(
        approval_id="a-1",
        decision_id="d-1",
        approver_id="u-1",
        approver_role="reviewer",
        action="approve",
        approval_method="email",
        approval_context="reviewed",
    )
    exception = PolicyExceptionRecord(
        exception_id="x-1",
        decision_id="d-1",
        policy_id="p-1",
        reason="documented exception",
        approver_id="u-1",
        approval_method="email",
        justification="manager accepted compensating control",
    )
    assert approval.approval_method == exception.approval_method == "email"


def test_export_requires_sha256_hex_digest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        AuditExport("d-1", "json", tmp_path / "audit.json", "bad")
