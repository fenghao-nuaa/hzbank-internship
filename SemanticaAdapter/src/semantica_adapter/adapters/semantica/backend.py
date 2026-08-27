"""Semantica-backed implementation of the stable governance contract."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from functools import wraps
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from semantica.context import ContextGraph, DecisionRecorder
from semantica.export import JSONExporter, RDFExporter
from semantica.ontology import OntologyValidator
from semantica.provenance import ProvenanceManager
from semantica.reasoning import Reasoner

from semantica_adapter.domain.errors import (
    BackendError,
    SemanticaAdapterError,
    UnsupportedCapabilityError,
)
from semantica_adapter.domain.models import (
    AgentProfile,
    ApprovalRecord,
    AuditExport,
    AuditStatus,
    AuditTrace,
    DecisionRecord,
    EvidenceRef,
    PolicyExceptionRecord,
    RuleEvaluation,
)

from .config import SemanticaConfig
from .mapping import to_semantica_approval, to_semantica_decision, to_semantica_exception


_Method = TypeVar("_Method", bound=Callable[..., Any])


def _translate_backend_errors(method: _Method) -> _Method:
    """Keep provider exceptions behind the stable adapter boundary."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except SemanticaAdapterError:
            raise
        except Exception as error:
            raise BackendError(f"Semantica {method.__name__} failed: {error}") from error

    return wrapped  # type: ignore[return-value]


def _safe_export_stem(decision_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", decision_id).strip("._-")
    if sanitized == decision_id:
        return sanitized
    digest = sha256(decision_id.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized or 'decision'}-{digest}"


class SemanticaBackend:
    """Translate company governance records into Semantica 0.6.6 operations.

    The small amount of local graph wiring around approvals and exceptions is a
    compatibility shim: Semantica 0.6.6's corresponding recorder paths assume
    a database ``execute_query`` API and cannot preserve caller-owned IDs when
    used with its in-memory ``ContextGraph``.
    """

    name = "semantica"

    def __init__(self, config: SemanticaConfig | None = None) -> None:
        self.config = config or SemanticaConfig()
        self.version = self.config.verify_version()
        self.graph = ContextGraph(advanced_analytics=self.config.advanced_analytics)
        if self.config.provenance_storage_path is not None:
            self.config.provenance_storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path = (
            str(self.config.provenance_storage_path)
            if self.config.provenance_storage_path is not None
            else None
        )
        self.provenance = ProvenanceManager(storage_path=storage_path)
        # Semantica 0.6.6's recorder omits the required ``source`` argument
        # while delegating to ProvenanceManager, so provenance is recorded
        # explicitly below through the public manager API.
        self.recorder = DecisionRecorder(self.graph)
        self.ontology_validator = OntologyValidator()
        self._profiles: dict[str, AgentProfile] = {}
        self._profile_nodes: dict[str, str] = {}
        self._evidence: dict[str, dict[str, EvidenceRef]] = {}
        self._evidence_nodes: dict[tuple[str, str], str] = {}
        self._decisions: dict[str, DecisionRecord] = {}
        self._approvals: dict[str, list[ApprovalRecord]] = {}
        self._approvals_by_id: dict[str, ApprovalRecord] = {}
        self._approval_nodes: dict[tuple[str, str], str] = {}
        self._exceptions: dict[str, list[PolicyExceptionRecord]] = {}
        self._exceptions_by_id: dict[str, PolicyExceptionRecord] = {}
        self._exception_nodes: dict[tuple[str, str], str] = {}

    def capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                "context",
                "reasoning",
                "provenance",
                "ontology",
                "approval",
                "exception",
                "json_export",
                "rdf_export",
            }
        )

    def health_check(self) -> Mapping[str, Any]:
        return {
            "healthy": True,
            "backend": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities()),
        }

    def _ensure_audit_node(self, audit_id: str) -> None:
        if not self.graph.get_node_attributes(audit_id):
            self.graph.add_node(audit_id, "AuditSession", audit_id=audit_id)

    @_translate_backend_errors
    def record_profile_snapshot(self, audit_id: str, profile: AgentProfile) -> str:
        self._ensure_audit_node(audit_id)
        node_id = f"profile:{profile.agent_id}:{profile.profile_version}:{audit_id}"
        self.graph.add_node(
            node_id,
            "AgentProfile",
            agent_id=profile.agent_id,
            name=profile.name,
            purpose=profile.purpose,
            profile_version=profile.profile_version,
            rule_set_id=profile.rule_set_id,
            rule_set_version=profile.rule_set_version,
            ontology_id=profile.ontology_id,
            ontology_version=profile.ontology_version,
            approval_policy=profile.approval_policy,
        )
        self.graph.add_edge(audit_id, node_id, "USES_PROFILE")
        self.provenance.track_entity(
            node_id,
            source=f"agent-profile://{profile.agent_id}/{profile.profile_version}",
            metadata={
                "rule_set_version": profile.rule_set_version,
                "ontology_version": profile.ontology_version,
            },
        )
        self._profiles[audit_id] = profile
        self._profile_nodes[audit_id] = node_id
        return node_id

    @_translate_backend_errors
    def record_evidence(self, audit_id: str, evidence: EvidenceRef) -> str:
        self._ensure_audit_node(audit_id)
        graph_evidence_id = f"evidence:{audit_id}:{evidence.evidence_id}"
        self.graph.add_node(
            graph_evidence_id,
            "Evidence",
            external_evidence_id=evidence.evidence_id,
            source_type=evidence.source_type,
            source_uri=evidence.source_uri,
            content_hash=evidence.content_hash,
            observed_at=evidence.observed_at.isoformat(),
            **dict(evidence.metadata),
        )
        self.graph.add_edge(audit_id, graph_evidence_id, "USES_EVIDENCE")
        self.provenance.track_entity(
            graph_evidence_id,
            source=evidence.source_uri,
            metadata={
                "content_hash": evidence.content_hash,
                "source_type": evidence.source_type,
                **dict(evidence.metadata),
            },
        )
        self._evidence.setdefault(audit_id, {})[evidence.evidence_id] = evidence
        self._evidence_nodes[(audit_id, evidence.evidence_id)] = graph_evidence_id
        return evidence.evidence_id

    @_translate_backend_errors
    def validate_ontology(
        self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]
    ) -> tuple[str, ...]:
        # Exercise Semantica's ontology validator for structural validation.
        structural = self.ontology_validator.validate(dict(profile.ontology))
        errors = list(structural.errors)
        expected_types = profile.ontology.get("types", {})
        for field_name, type_name in expected_types.items():
            value = inputs.get(field_name)
            if value is not None and type(value).__name__ != type_name:
                errors.append(f"{field_name} must be {type_name}")
        return tuple(errors)

    @_translate_backend_errors
    def evaluate_rules(
        self, audit_id: str, profile: AgentProfile, inputs: Mapping[str, Any]
    ) -> RuleEvaluation:
        missing = tuple(field for field in profile.required_fields if field not in inputs)
        if missing:
            return RuleEvaluation(
                profile.rule_set_id,
                profile.rule_set_version,
                missing_fields=missing,
            )

        facts = [f"present_{field}" for field in inputs]
        if {"declared_amount", "ledger_amount"}.issubset(inputs):
            amount_fact = (
                "amount_match"
                if inputs["declared_amount"] == inputs["ledger_amount"]
                else "amount_mismatch"
            )
            facts.append(amount_fact)

        reasoner_rules = tuple(profile.rules.get("reasoner_rules", ()))
        reasoner = Reasoner()
        for fact in facts:
            reasoner.add_fact(fact)
        for rule in reasoner_rules:
            reasoner.add_rule(rule)
        results = reasoner.forward_chain()
        return RuleEvaluation(
            profile.rule_set_id,
            profile.rule_set_version,
            matched_rules=tuple(
                result.rule_used.rule_id for result in results if result.rule_used is not None
            ),
            conclusions=tuple(result.conclusion for result in results),
            explanation_steps=tuple(
                f"{', '.join(result.premises)} -> {result.conclusion}"
                for result in results
            ),
            conflicts=tuple(str(item) for item in inputs.get("_conflicts", ())),
        )

    @_translate_backend_errors
    def record_decision(self, decision: DecisionRecord) -> str:
        evidence_by_id = self._evidence.get(decision.audit_id, {})
        sources = [
            evidence_by_id[evidence_id].source_uri
            for evidence_id in decision.evidence_ids
            if evidence_id in evidence_by_id
        ]
        graph_evidence_ids = [
            self._evidence_nodes[(decision.audit_id, evidence_id)]
            for evidence_id in decision.evidence_ids
            if (decision.audit_id, evidence_id) in self._evidence_nodes
        ]
        decision_id = self.recorder.record_decision(
            to_semantica_decision(decision),
            # DecisionRecorder.link_entities() re-adds existing ContextGraph
            # nodes as generic Entity nodes. Preserve typed Evidence nodes and
            # wire the same public ABOUT relationship explicitly below.
            entities=[],
            source_documents=sources,
        )
        for graph_evidence_id in graph_evidence_ids:
            self.graph.add_edge(decision_id, graph_evidence_id, "ABOUT")
        self.graph.add_edge(decision.audit_id, decision_id, "PRODUCED_DECISION")
        self.provenance.track_entity(
            decision_id,
            source=sources[0] if sources else f"audit://{decision.audit_id}",
            metadata={
                "audit_id": decision.audit_id,
                "agent_id": decision.agent_id,
                "profile_version": decision.profile_version,
                "source_documents": sources,
            },
            confidence=decision.confidence,
        )
        self._decisions[decision_id] = decision
        return decision_id

    @_translate_backend_errors
    def update_decision_status(
        self, decision_id: str, status: AuditStatus
    ) -> None:
        self.graph.add_node_attribute(decision_id, {"status": status.value})
        self._decisions[decision_id] = replace(
            self._decisions[decision_id], status=status
        )

    @_translate_backend_errors
    def record_approval(self, approval: ApprovalRecord) -> str:
        existing = self._approvals_by_id.get(approval.approval_id)
        if existing is not None:
            if existing != approval:
                raise BackendError(f"approval ID collision: {approval.approval_id}")
            return approval.approval_id
        mapped = to_semantica_approval(approval)
        graph_approval_id = f"approval:{approval.decision_id}:{approval.approval_id}"
        self.graph.add_node(
            graph_approval_id,
            "ApprovalChain",
            external_approval_id=mapped.approval_id,
            decision_id=mapped.decision_id,
            approver=mapped.approver,
            approval_method=mapped.approval_method,
            approval_context=mapped.approval_context,
            timestamp=mapped.timestamp.isoformat(),
            **dict(mapped.metadata),
        )
        self.graph.add_edge(mapped.decision_id, graph_approval_id, "APPROVED_BY")
        self._approvals.setdefault(approval.decision_id, []).append(approval)
        self._approvals_by_id[approval.approval_id] = approval
        self._approval_nodes[(approval.decision_id, approval.approval_id)] = graph_approval_id
        return approval.approval_id

    @_translate_backend_errors
    def record_exception(self, exception: PolicyExceptionRecord) -> str:
        existing = self._exceptions_by_id.get(exception.exception_id)
        if existing is not None:
            if existing != exception:
                raise BackendError(f"exception ID collision: {exception.exception_id}")
            return exception.exception_id
        mapped = to_semantica_exception(exception)
        graph_exception_id = f"exception:{exception.decision_id}:{exception.exception_id}"
        self.graph.add_node(
            graph_exception_id,
            "Exception",
            external_exception_id=mapped.exception_id,
            decision_id=mapped.decision_id,
            policy_id=mapped.policy_id,
            reason=mapped.reason,
            approver=mapped.approver,
            approval_timestamp=mapped.approval_timestamp.isoformat(),
            justification=mapped.justification,
            **dict(mapped.metadata),
        )
        self.graph.add_node(mapped.policy_id, "Policy", policy_id=mapped.policy_id)
        self.graph.add_edge(mapped.decision_id, graph_exception_id, "GRANTED_EXCEPTION")
        self.graph.add_edge(graph_exception_id, mapped.policy_id, "OVERRIDDEN_POLICY")
        self._exceptions.setdefault(exception.decision_id, []).append(exception)
        self._exceptions_by_id[exception.exception_id] = exception
        self._exception_nodes[(exception.decision_id, exception.exception_id)] = graph_exception_id
        return exception.exception_id

    def _scoped_graph(self, decision_id: str) -> dict[str, Any]:
        decision = self._decisions[decision_id]
        graph = self.graph.to_kg_dict()
        allowed_ids = {
            decision_id,
            decision.audit_id,
        }
        allowed_ids.update(
            self._evidence_nodes[(decision.audit_id, evidence_id)]
            for evidence_id in decision.evidence_ids
            if (decision.audit_id, evidence_id) in self._evidence_nodes
        )
        profile_node = self._profile_nodes.get(decision.audit_id)
        if profile_node is not None:
            allowed_ids.add(profile_node)
        for approval in self._approvals.get(decision_id, ()):
            allowed_ids.add(
                self._approval_nodes[(decision_id, approval.approval_id)]
            )
        for exception in self._exceptions.get(decision_id, ()):
            allowed_ids.add(
                self._exception_nodes[(decision_id, exception.exception_id)]
            )
            allowed_ids.add(exception.policy_id)
        entities = tuple(
            entity for entity in graph["entities"] if entity["id"] in allowed_ids
        )
        relationships = tuple(
            relationship
            for relationship in graph["relationships"]
            if relationship["source_id"] in allowed_ids
            and relationship["target_id"] in allowed_ids
        )
        return {
            "entities": entities,
            "relationships": relationships,
            "statistics": {
                "entity_count": len(entities),
                "relationship_count": len(relationships),
            },
        }

    def _scoped_provenance_rdf(self, decision_id: str) -> str:
        decision = self._decisions[decision_id]
        profile = self._profiles[decision.audit_id]
        profile_node = self._profile_nodes[decision.audit_id]
        scoped = ProvenanceManager()
        scoped.track_entity(
            profile_node,
            source=f"agent-profile://{profile.agent_id}/{profile.profile_version}",
            metadata={
                "rule_set_version": profile.rule_set_version,
                "ontology_version": profile.ontology_version,
            },
        )
        evidence_by_id = self._evidence.get(decision.audit_id, {})
        sources: list[str] = []
        for evidence_id in decision.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            sources.append(evidence.source_uri)
            scoped.track_entity(
                self._evidence_nodes[(decision.audit_id, evidence.evidence_id)],
                source=evidence.source_uri,
                metadata={
                    "content_hash": evidence.content_hash,
                    "source_type": evidence.source_type,
                },
            )
        scoped.track_entity(
            decision_id,
            source=sources[0] if sources else f"audit://{decision.audit_id}",
            metadata={
                "audit_id": decision.audit_id,
                "agent_id": decision.agent_id,
                "profile_version": decision.profile_version,
                "source_documents": sources,
            },
            confidence=decision.confidence,
        )
        return scoped.export_prov(format="turtle")

    @_translate_backend_errors
    def trace_decision(self, decision_id: str) -> AuditTrace:
        decision = self._decisions[decision_id]
        graph = self._scoped_graph(decision_id)
        evidence_by_id = self._evidence.get(decision.audit_id, {})
        evidence = tuple(
            evidence_by_id[evidence_id]
            for evidence_id in decision.evidence_ids
            if evidence_id in evidence_by_id
        )
        return AuditTrace(
            decision_id=decision_id,
            agent_id=decision.agent_id,
            profile_version=decision.profile_version,
            rule_set_id=decision.rule_evaluation.rule_set_id,
            rule_set_version=decision.rule_evaluation.rule_set_version,
            decision_status=decision.status,
            evidence=evidence,
            nodes=tuple(graph["entities"]),
            approvals=tuple(self._approvals.get(decision_id, ())),
            exceptions=tuple(self._exceptions.get(decision_id, ())),
        )

    @_translate_backend_errors
    def export_decision(self, decision_id: str, output_dir: Path, format: str) -> AuditExport:
        output_dir.mkdir(parents=True, exist_ok=True)
        export_stem = _safe_export_stem(decision_id)
        if format == "json":
            target = output_dir / f"{export_stem}.json"
            serializable_trace = json.loads(
                json.dumps(asdict(self.trace_decision(decision_id)), default=str)
            )
            JSONExporter().export(serializable_trace, target)
        elif format in {"turtle", "ttl"}:
            target = output_dir / f"{export_stem}.ttl"
            RDFExporter().export_knowledge_graph(
                self._scoped_graph(decision_id), target, format="turtle"
            )
            graph_rdf = target.read_text(encoding="utf-8")
            provenance_rdf = self._scoped_provenance_rdf(decision_id)
            target.write_text(
                graph_rdf + "\n\n# W3C PROV-O provenance\n" + provenance_rdf,
                encoding="utf-8",
            )
        else:
            raise UnsupportedCapabilityError(f"Semantica backend does not support {format}")
        return AuditExport(
            decision_id=decision_id,
            format=format,
            path=target,
            sha256=sha256(target.read_bytes()).hexdigest(),
        )
