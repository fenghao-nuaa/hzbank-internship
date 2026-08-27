from semantica.context import ApprovalChain, Decision, PolicyException

from semantica_adapter.adapters.semantica.mapping import (
    normalize_trace,
    to_semantica_approval,
    to_semantica_decision,
    to_semantica_exception,
)
from semantica_adapter.domain.models import (
    ApprovalRecord,
    AuditStatus,
    DecisionRecord,
    PolicyExceptionRecord,
    RuleEvaluation,
)


def _decision() -> DecisionRecord:
    return DecisionRecord(
        decision_id="decision-1",
        audit_id="audit-1",
        agent_id="amount-checker",
        profile_version="1.0",
        category="amount_reconciliation",
        scenario="Compare amounts",
        outcome="mismatch",
        reasoning_summary="Amounts differ",
        confidence=1.0,
        evidence_ids=("ledger-1", "voucher-1"),
        rule_evaluation=RuleEvaluation("amount-rules", "2026.08", matched_rules=("amount-equality",)),
        backend_name="semantica",
        backend_version="0.6.6",
        status=AuditStatus.PENDING_APPROVAL,
    )


def test_decision_mapping_preserves_public_context() -> None:
    mapped = to_semantica_decision(_decision())
    assert isinstance(mapped, Decision)
    assert mapped.decision_id == "decision-1"
    assert mapped.reasoning == "Amounts differ"
    assert mapped.metadata["profile_version"] == "1.0"
    assert mapped.metadata["evidence_ids"] == ["ledger-1", "voucher-1"]


def test_approval_and_exception_mapping_use_semantica_models() -> None:
    approval = ApprovalRecord(
        "approval-1",
        "decision-1",
        "risk-manager",
        "reviewer",
        "approve",
        "email",
        "reviewed evidence",
    )
    exception = PolicyExceptionRecord(
        "exception-1",
        "decision-1",
        "POL-1",
        "temporary override",
        "risk-manager",
        "email",
        "compensating control",
    )
    mapped_approval = to_semantica_approval(approval)
    mapped_exception = to_semantica_exception(exception)
    assert isinstance(mapped_approval, ApprovalChain)
    assert mapped_approval.metadata["approver_role"] == "reviewer"
    assert isinstance(mapped_exception, PolicyException)
    assert mapped_exception.metadata["approval_method"] == "email"


def test_trace_mapping_returns_provider_neutral_record() -> None:
    trace = normalize_trace(
        [{"id": "decision-1", "type": "Decision"}],
        decision_id="decision-1",
        agent_id="amount-checker",
        profile_version="1.0",
        rule_set_id="amount-rules",
        rule_set_version="2026.08",
    )
    assert trace.nodes == ({"id": "decision-1", "type": "Decision"},)
