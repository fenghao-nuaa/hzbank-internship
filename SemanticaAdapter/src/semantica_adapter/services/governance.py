"""Fail-closed lifecycle orchestration for governed Agent decisions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from semantica_adapter.domain.errors import ApprovalRequiredError, ValidationError
from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditRequest,
    AuditSession,
    AuditStatus,
    AuditTrace,
    DecisionRecord,
    PolicyExceptionRecord,
)
from semantica_adapter.ports import (
    AgentProfileRepository,
    ApprovalWorkflowPort,
    GovernanceBackend,
)
from semantica_adapter.services.integrity import AuditPackage, publish_export_package


class AgentGovernanceService:
    """The stable interface used by company Agents.

    The service owns workflow state and authorization. The selected backend
    owns graph, inference, provenance and export persistence.
    """

    def __init__(
        self,
        backend: GovernanceBackend,
        profiles: AgentProfileRepository,
        approvals: ApprovalWorkflowPort,
    ) -> None:
        self.backend = backend
        self.profiles = profiles
        self.approvals = approvals
        self._audits: dict[str, AuditSession] = {}
        self._decisions: dict[str, DecisionRecord] = {}
        self._submitted_approvals: dict[str, ApprovalRecord] = {}
        self._recorded_exceptions: dict[str, PolicyExceptionRecord] = {}

    def register_agent(self, profile: AgentProfile) -> AgentProfile:
        self.profiles.save(profile)
        return profile

    def start_audit(self, request: AuditRequest) -> AuditSession:
        profile = self.profiles.get(request.agent_id)
        audit_id = f"audit:{request.request_id}"
        if audit_id in self._audits:
            raise ValidationError(f"audit already exists: {audit_id}")
        session = AuditSession(audit_id=audit_id, request=request, profile=profile)
        self.backend.record_profile_snapshot(audit_id, profile)
        for evidence in request.evidence:
            self.backend.record_evidence(audit_id, evidence)
        self._audits[audit_id] = session
        return session

    def evaluate(self, audit_id: str) -> AuditSession:
        session = self._audits[audit_id]
        ontology_errors = list(
            self.backend.validate_ontology(
                audit_id, session.profile, session.request.inputs
            )
        )
        evaluation = self.backend.evaluate_rules(
            audit_id, session.profile, session.request.inputs
        )
        disallowed_sources = tuple(
            evidence.source_type
            for evidence in session.request.evidence
            if evidence.source_type not in session.profile.allowed_source_types
        )
        if disallowed_sources:
            evaluation = replace(
                evaluation,
                conflicts=evaluation.conflicts
                + tuple(f"source type not allowed: {item}" for item in disallowed_sources),
            )
        evidence_conflicts: tuple[str, ...] = ()
        if not session.request.evidence:
            evidence_conflicts = ("no evidence supplied",)
        required_evidence_types = tuple(
            session.profile.metadata.get("required_evidence_types", ())
        )
        observed_source_types = {item.source_type for item in session.request.evidence}
        missing_evidence_types = tuple(
            item for item in required_evidence_types if item not in observed_source_types
        )
        if evidence_conflicts or missing_evidence_types:
            evaluation = replace(
                evaluation,
                conflicts=evaluation.conflicts
                + evidence_conflicts
                + tuple(
                    f"missing required evidence type: {item}"
                    for item in missing_evidence_types
                ),
            )
        mandatory_adverse_conclusions = {
            "manual_review",
            "mismatch",
            "reject",
            "rejected",
        }
        manual_review_conclusions = mandatory_adverse_conclusions | set(
            session.profile.rules.get("manual_review_conclusions", ())
        )
        rules_require_review = any(
            conclusion in manual_review_conclusions
            for conclusion in evaluation.conclusions
        )
        status = (
            AuditStatus.MANUAL_REVIEW
            if (
                ontology_errors
                or evaluation.missing_fields
                or evaluation.conflicts
                or rules_require_review
            )
            else AuditStatus.EVALUATED
        )
        updated = replace(
            session,
            status=status,
            ontology_errors=tuple(ontology_errors),
            rule_evaluation=evaluation,
        )
        self._audits[audit_id] = updated
        return updated

    def record_decision(
        self,
        audit_id: str,
        proposed_outcome: str,
        reasoning_summary: str,
        confidence: float,
    ) -> DecisionRecord:
        session = self._audits[audit_id]
        if session.rule_evaluation is None:
            raise ValidationError("audit must be evaluated before recording a decision")

        unsafe = session.status is AuditStatus.MANUAL_REVIEW
        outcome = "manual_review" if unsafe else proposed_outcome
        needs_approval = session.profile.approval_policy == "always" or (
            session.profile.approval_policy == "manual_on_review"
            and outcome == "manual_review"
        )
        status = AuditStatus.PENDING_APPROVAL if needs_approval else AuditStatus.EVALUATED
        decision_id = f"decision:{audit_id}"
        decision = DecisionRecord(
            decision_id=decision_id,
            audit_id=audit_id,
            agent_id=session.profile.agent_id,
            profile_version=session.profile.profile_version,
            category=session.request.task_type,
            scenario=f"Governed {session.request.task_type} decision",
            outcome=outcome,
            reasoning_summary=reasoning_summary,
            confidence=confidence,
            evidence_ids=tuple(item.evidence_id for item in session.request.evidence),
            rule_evaluation=session.rule_evaluation,
            backend_name=self.backend.name,
            backend_version=self.backend.version,
            status=status,
        )
        self.backend.record_decision(decision)
        self._decisions[decision_id] = decision
        self._audits[audit_id] = replace(
            session, status=status, decision_id=decision_id
        )
        return decision

    def submit_approval(self, approval: ApprovalRecord) -> DecisionRecord:
        if not self.approvals.authorize(approval):
            raise ApprovalRequiredError(
                f"actor {approval.approver_id} is not authorized to approve"
            )
        previous = self._submitted_approvals.get(approval.approval_id)
        if previous is not None:
            if previous != approval:
                raise ValidationError(
                    f"approval ID already used with different content: {approval.approval_id}"
                )
            return self._decisions[approval.decision_id]
        decision = self._decisions[approval.decision_id]
        if decision.status is not AuditStatus.PENDING_APPROVAL:
            raise ApprovalRequiredError("decision is not pending approval")
        self.backend.record_approval(approval)
        final_status = (
            AuditStatus.APPROVED
            if approval.action == "approve"
            else AuditStatus.REJECTED
        )
        updated = replace(decision, status=final_status)
        self.backend.update_decision_status(approval.decision_id, final_status)
        self._decisions[approval.decision_id] = updated
        self._submitted_approvals[approval.approval_id] = approval
        audit = self._audits[decision.audit_id]
        self._audits[decision.audit_id] = replace(audit, status=final_status)
        return updated

    def record_exception(
        self, exception: PolicyExceptionRecord
    ) -> PolicyExceptionRecord:
        if not self.approvals.authorize(exception):
            raise ApprovalRequiredError(
                f"actor {exception.approver_id} is not authorized to grant an exception"
            )
        previous = self._recorded_exceptions.get(exception.exception_id)
        if previous is not None:
            if previous != exception:
                raise ValidationError(
                    f"exception ID already used with different content: {exception.exception_id}"
                )
            return previous
        if exception.decision_id not in self._decisions:
            raise ValidationError(f"unknown decision: {exception.decision_id}")
        if self._decisions[exception.decision_id].status is not AuditStatus.PENDING_APPROVAL:
            raise ValidationError("policy exceptions require a decision pending approval")
        self.backend.record_exception(exception)
        self._recorded_exceptions[exception.exception_id] = exception
        return exception

    def get_audit_trace(self, decision_id: str) -> AuditTrace:
        return self.backend.trace_decision(decision_id)

    def export_audit(
        self, decision_id: str, output_dir: Path, format: str
    ) -> AuditExport:
        return self.backend.export_decision(decision_id, output_dir, format)

    def export_audit_package(
        self, decision_id: str, output_dir: Path
    ) -> AuditPackage:
        """Render and atomically publish a JSON/RDF package with detached hashes."""

        output_dir = Path(output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix=f".{output_dir.name}.exports-", dir=output_dir.parent
        ) as temporary:
            temporary_dir = Path(temporary)
            exports = (
                self.backend.export_decision(decision_id, temporary_dir, "json"),
                self.backend.export_decision(decision_id, temporary_dir, "turtle"),
            )
            return publish_export_package(
                exports,
                output_dir,
                backend_name=self.backend.name,
                backend_version=self.backend.version,
            )
