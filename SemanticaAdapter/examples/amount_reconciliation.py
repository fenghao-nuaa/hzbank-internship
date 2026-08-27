"""Bank amount-reconciliation Agent using only SemanticaAdapter's public API."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from semantica_adapter import (
    AgentGovernanceService,
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditPackage,
    AuditRequest,
    AuditTrace,
    DecisionRecord,
    EvidenceRef,
    create_local_semantica_service,
)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    decision: DecisionRecord
    trace: AuditTrace
    json_export: AuditExport
    rdf_export: AuditExport
    package: AuditPackage


def run_amount_reconciliation(
    service: AgentGovernanceService, output_dir: Path
) -> ReconciliationResult:
    """Run one controlled mismatch, human approval, and audit export."""

    profile = AgentProfile(
        agent_id="amount-checker",
        name="Amount Reconciliation Agent",
        purpose="Compare voucher declarations against the authoritative ledger",
        profile_version="1.0",
        rule_set_id="amount-reconciliation-rules",
        rule_set_version="2026.08",
        ontology_id="banking-amount-ontology",
        ontology_version="1.0",
        allowed_source_types=("ledger", "voucher"),
        approval_policy="manual_on_review",
        required_fields=("declared_amount", "ledger_amount"),
        rules={
            "reasoner_rules": (
                "IF amount_mismatch THEN manual_review",
                "IF amount_match THEN auto_pass",
            )
        },
        ontology={
            "classes": {"AmountReconciliation": {}},
            "properties": {"declared_amount": {}, "ledger_amount": {}},
            "types": {"declared_amount": "int", "ledger_amount": "int"},
        },
    )
    service.register_agent(profile)

    request = AuditRequest(
        request_id="amount-case-20260824-001",
        agent_id=profile.agent_id,
        task_type="amount_reconciliation",
        inputs={"declared_amount": 10100, "ledger_amount": 10000},
        evidence=(
            EvidenceRef(
                "ledger-1",
                "ledger",
                "ledger://entries/20260824-001",
                sha256(b"ledger_amount=10000").hexdigest(),
            ),
            EvidenceRef(
                "voucher-1",
                "voucher",
                "voucher://applications/20260824-001",
                sha256(b"declared_amount=10100").hexdigest(),
            ),
        ),
    )
    audit = service.start_audit(request)
    service.evaluate(audit.audit_id)
    decision = service.record_decision(
        audit.audit_id,
        proposed_outcome="manual_review",
        reasoning_summary=(
            "The declared amount is 10,100 while the authoritative ledger amount is "
            "10,000; rule-based reconciliation requires manual review."
        ),
        confidence=1.0,
    )
    service.submit_approval(
        ApprovalRecord(
            approval_id="approval-amount-case-001",
            decision_id=decision.decision_id,
            approver_id="risk-manager",
            approver_role="reviewer",
            action="approve",
            approval_method="email",
            approval_context="Compared the voucher and ledger evidence.",
        )
    )
    trace = service.get_audit_trace(decision.decision_id)
    package = service.export_audit_package(decision.decision_id, output_dir)
    json_export = next(item for item in package.exports if item.format == "json")
    rdf_export = next(item for item in package.exports if item.format == "turtle")
    return ReconciliationResult(decision, trace, json_export, rdf_export, package)


def main() -> None:
    output_dir = Path("amount-reconciliation-output")
    service = create_local_semantica_service(
        authorized_actors={("risk-manager", "reviewer")},
        provenance_storage_path=Path("amount-reconciliation-state") / "provenance.db",
    )
    result = run_amount_reconciliation(service, output_dir)
    print(
        f"decision={result.decision.decision_id} "
        f"initial_status={result.decision.status.value} "
        f"final_status={result.trace.decision_status.value}"
    )
    print(f"json={result.json_export.path}")
    print(f"rdf={result.rdf_export.path}")


if __name__ == "__main__":
    main()
