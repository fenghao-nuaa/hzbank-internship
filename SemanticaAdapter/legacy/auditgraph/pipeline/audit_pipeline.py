"""Compose every AuditGraph stage into one auditable execution."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from auditgraph.conflicts import ConflictDetector
from auditgraph.context import DecisionRecorder
from auditgraph.core.models import (
    Approval,
    Decision,
    ExtractionResult,
    PipelineResult,
    PolicyException,
    SourceDocument,
)
from auditgraph.deduplication import EntityResolver
from auditgraph.export import JSONExporter, RDFExporter
from auditgraph.ingest import APIIngestor, DBIngestor, FileIngestor, WebIngestor
from auditgraph.kg import KnowledgeGraph
from auditgraph.normalize import TextNormalizer
from auditgraph.ontology import Ontology, OntologyClass, OntologyValidator, PropertyConstraint
from auditgraph.parse import DocumentParser
from auditgraph.provenance import ChainVerification, InMemoryStorage, ProvenanceManager
from auditgraph.provenance.integrity import verify_checksum
from auditgraph.query import QueryService
from auditgraph.reasoning import Condition, Rule, RuleEngine
from auditgraph.semantic_extract import SemanticExtractor
from auditgraph.split import TextSplitter
from auditgraph.visualization import HTMLVisualizer


class PipelineStageError(RuntimeError):
    def __init__(self, run_id: str, stage: str, object_id: str, error: Exception | str) -> None:
        self.run_id = run_id
        self.stage = stage
        self.object_id = object_id
        self.original_error = error
        super().__init__(f"{stage} failed for {object_id} in run {run_id}: {type(error).__name__}: {error}")


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_type: str
    location: str
    query: str | None = None
    allow_private_network: bool = False

    def __post_init__(self) -> None:
        if self.source_type not in {"file", "web", "database", "api"}:
            raise ValueError("source_type must be file, web, database, or api")
        if not self.location:
            raise ValueError("source location is required")


class AuditPipeline:
    def __init__(self) -> None:
        self._reset_state()

    def _reset_state(self) -> None:
        self.graph = KnowledgeGraph()
        self.provenance = ProvenanceManager(InMemoryStorage())
        self.recorder = DecisionRecorder(self.graph, self.provenance)

    def run_sources(
        self,
        sources: list[SourceSpec],
        *,
        output_dir: str | Path,
        rules: list[Rule] | None = None,
        approval: Approval | None = None,
        policy_exception: PolicyException | None = None,
    ) -> PipelineResult:
        run_id = f"run:{uuid4()}"
        self._reset_state()
        documents: list[SourceDocument] = []
        try:
            for source in sources:
                if source.source_type == "file":
                    documents.append(FileIngestor().ingest(source.location))
                elif source.source_type == "web":
                    documents.append(
                        WebIngestor(allow_private_network=source.allow_private_network).ingest(source.location)
                    )
                elif source.source_type == "database":
                    if not source.query:
                        raise ValueError("database source requires a query")
                    documents.append(DBIngestor().ingest_sqlite(source.location, source.query))
                else:
                    documents.append(
                        APIIngestor(allow_private_network=source.allow_private_network).ingest(source.location)
                    )
        except Exception as error:
            self._reset_state()
            object_id = source.location if "source" in locals() else "sources"
            raise PipelineStageError(run_id, "ingest", object_id, error) from error
        return self.run(
            documents,
            output_dir=output_dir,
            rules=rules,
            approval=approval,
            policy_exception=policy_exception,
        )

    def run(
        self,
        documents: list[SourceDocument],
        *,
        output_dir: str | Path,
        rules: list[Rule] | None = None,
        approval: Approval | None = None,
        policy_exception: PolicyException | None = None,
    ) -> PipelineResult:
        run_id = f"run:{uuid4()}"
        self._reset_state()
        try:
            return self._run(
                run_id,
                documents,
                output_dir=output_dir,
                rules=rules,
                approval=approval,
                policy_exception=policy_exception,
            )
        except PipelineStageError:
            self._reset_state()
            raise
        except Exception as error:
            self._reset_state()
            raise PipelineStageError(run_id, "pipeline", "run", error) from error

    def _run(
        self,
        run_id: str,
        documents: list[SourceDocument],
        *,
        output_dir: str | Path,
        rules: list[Rule] | None,
        approval: Approval | None,
        policy_exception: PolicyException | None,
    ) -> PipelineResult:
        if not documents:
            raise PipelineStageError(run_id, "ingest", "sources", "at least one source is required")
        output = Path(output_dir)

        try:
            parsed = [DocumentParser().parse(document) for document in documents]
            normalized = [TextNormalizer().normalize_document(document) for document in parsed]
            chunks = [chunk for document in normalized for chunk in TextSplitter(max_chars=2_000).split(document)]
        except Exception as error:
            raise PipelineStageError(run_id, "parse_normalize_split", "documents", error) from error

        for document in documents:
            self.provenance.track(document.source_id, "source", document.to_dict())
            self.graph.add_node(document.source_id, "source", source_type=document.source_type)
        for chunk in chunks:
            self.provenance.track(chunk.chunk_id, "chunk", chunk.to_dict(), derived_from=[chunk.source_id])
            self.graph.add_node(chunk.chunk_id, "chunk", content=chunk.content, source_id=chunk.source_id)
            self.graph.add_edge(chunk.chunk_id, chunk.source_id, "DERIVED_FROM")

        try:
            extracted = SemanticExtractor().extract(chunks)
            resolved_entities, entity_mapping = EntityResolver().resolve_with_mapping(extracted.entities)
            resolved = ExtractionResult(
                entities=resolved_entities,
                relations=[
                    replace(
                        relation,
                        subject_id=entity_mapping.get(relation.subject_id, relation.subject_id),
                        object_id=entity_mapping.get(relation.object_id, relation.object_id),
                    )
                    for relation in extracted.relations
                ],
                events=[
                    replace(
                        event,
                        participants=[entity_mapping.get(item, item) for item in event.participants],
                    )
                    for event in extracted.events
                ],
                triplets=[
                    replace(
                        triplet,
                        subject=entity_mapping.get(triplet.subject, triplet.subject),
                        object=(
                            entity_mapping.get(triplet.object, triplet.object)
                            if isinstance(triplet.object, str)
                            else triplet.object
                        ),
                    )
                    for triplet in extracted.triplets
                ],
            )
            # Conflict subjects must use the same canonical IDs as the graph.
            # Otherwise a conflicting alias can evade decision-critical checks.
            conflicts = ConflictDetector().detect(resolved.triplets)
            semantic_graph = KnowledgeGraph.from_extraction(resolved)
            self._merge_graph(semantic_graph)
        except Exception as error:
            raise PipelineStageError(run_id, "extract_conflict_deduplicate_graph", "chunks", error) from error

        for entity in resolved.entities:
            if not any(entry.entity_id == entity.entity_id for entry in self.provenance.storage.all()):
                self.provenance.track(
                    entity.entity_id,
                    "entity",
                    entity.to_dict(),
                    derived_from=sorted(entity.source_ids),
                )
        fact_index = self._record_semantic_provenance(resolved)
        for conflict in conflicts:
            self.graph.add_node(conflict.conflict_id, "conflict", **conflict.to_dict())
            if conflict.subject in self.graph.nodes:
                self.graph.add_edge(conflict.conflict_id, conflict.subject, "CONFLICTS_WITH")
            self.provenance.track(
                conflict.conflict_id,
                "conflict",
                conflict.to_dict(),
                derived_from=sorted(conflict.source_ids),
            )

        default_rules = [
            Rule(
                "POL-RISK-001",
                "1.0",
                [Condition("risk_score", ">=", 70)],
                "manual_review",
                source_ref="policy.txt#article-3",
            )
        ]
        active_rules = default_rules if rules is None else rules
        for rule in active_rules:
            policy_node_id = f"{rule.rule_id}:{rule.version}"
            self.graph.add_node(
                policy_node_id,
                "Policy",
                policy_id=rule.rule_id,
                version=rule.version,
                source_ref=rule.source_ref,
                conclusion=rule.conclusion,
            )
            if not any(entry.entity_id == policy_node_id for entry in self.provenance.storage.all()):
                policy_sources = self._policy_sources(rule.source_ref, documents)
                self.provenance.track(
                    policy_node_id,
                    "policy",
                    {
                        "policy_id": rule.rule_id,
                        "version": rule.version,
                        "source_ref": rule.source_ref,
                        "conditions": [
                            {"field": item.field, "operator": item.operator, "value": item.value}
                            for item in rule.conditions
                        ],
                        "conclusion": rule.conclusion,
                    },
                    derived_from=policy_sources,
                )

        application = self._select_application(run_id)
        ontology = Ontology(
            "banking",
            "1.0",
            {
                "LoanApplication": OntologyClass(
                    "LoanApplication",
                    [PropertyConstraint("risk_score", required=True, value_type="number")],
                )
            },
            source_ref="policy.txt",
        )
        ontology_result = OntologyValidator().validate(self.graph, ontology)
        reasoning = RuleEngine(active_rules).evaluate(application.properties)
        decision_fields = {condition.field for rule in active_rules for condition in rule.conditions}
        missing_decision_fields = sorted(
            field for field in decision_fields if field not in application.properties
        )
        critical_conflicts = [
            conflict
            for conflict in conflicts
            if conflict.subject == application.node_id and conflict.predicate in decision_fields
        ]
        if not ontology_result.valid:
            outcome = "manual_review"
            reasoning_text = "Ontology validation failed; automated approval is prohibited."
        elif missing_decision_fields:
            outcome = "manual_review"
            reasoning_text = "Required decision facts are missing; automated approval is prohibited."
        elif critical_conflicts:
            outcome = "manual_review"
            reasoning_text = "Decision-critical facts conflict; automated approval is prohibited."
        else:
            outcome = reasoning.conclusions[0] if reasoning.conclusions else "approved"
            reasoning_text = "; ".join(step for match in reasoning.matches for step in match.steps)
            if not reasoning_text:
                reasoning_text = "No escalation rule matched; ontology constraints passed."

        decision = Decision(
            category="regulated_decision",
            scenario=f"Decision for {application.node_id}",
            reasoning=reasoning_text,
            outcome=outcome,
            confidence=1.0,
            metadata={
                "run_id": run_id,
                "ontology_id": ontology.ontology_id,
                "ontology_version": ontology.version,
                "ontology_valid": ontology_result.valid,
                "conflict_ids": [conflict.conflict_id for conflict in conflicts],
                "critical_conflict_ids": [conflict.conflict_id for conflict in critical_conflicts],
                "missing_decision_fields": missing_decision_fields,
                "rule_matches": [match.rule_id for match in reasoning.matches],
            },
        )
        decision_evidence = [
            fact_id
            for field in sorted(decision_fields)
            for fact_id in fact_index.get((application.node_id, field), [])
        ]
        if not decision_evidence:
            decision_evidence = [application.node_id]
        self.recorder.record_decision(
            decision,
            evidence_ids=decision_evidence,
            rule_refs=[(match.rule_id, match.version) for match in reasoning.matches],
        )
        if approval is not None:
            actual_approval = replace(approval, decision_id=decision.decision_id)
            self.recorder.record_approval(actual_approval)
        if policy_exception:
            self.recorder.record_policy_exception(replace(policy_exception, decision_id=decision.decision_id))

        export_paths = [
            output / "auditgraph.json",
            output / "auditgraph.ttl",
            output / "auditgraph.html",
            output / "auditgraph.manifest.json",
        ]
        query = QueryService(self.graph, self.provenance)
        compliance = query.compliance_report(decision.decision_id)
        stage_counts = {
            "documents": len(documents),
            "chunks": len(chunks),
            "entities": len(resolved.entities),
            "relations": len(resolved.relations),
            "events": len(resolved.events),
            "triplets": len(resolved.triplets),
            "conflicts": len(conflicts),
            "decisions": 1,
            "approvals": int(approval is not None),
            "exceptions": int(policy_exception is not None),
        }
        result = PipelineResult(
            run_id=run_id,
            decision_id=decision.decision_id,
            stage_counts=stage_counts,
            compliant=(
                ontology_result.valid
                and not missing_decision_fields
                and not critical_conflicts
                and compliance["compliant"]
            ),
            audit_chain_valid=False,
            exports=export_paths,
            errors=ontology_result.errors,
        )
        graph_payload = self.graph.to_kg_dict()
        output.parent.mkdir(parents=True, exist_ok=True)
        # Render everything in a sibling staging directory. An exporter failure
        # therefore cannot expose a half-written regulatory evidence package.
        with tempfile.TemporaryDirectory(prefix=".auditgraph-", dir=output.parent) as staging_dir:
            staging = Path(staging_dir)
            staged_paths = [staging / path.name for path in export_paths]
            RDFExporter().export(graph_payload, self.provenance.storage.all(), staged_paths[1])
            HTMLVisualizer().export(graph_payload, staged_paths[2])

            verification = self.provenance.verify_chain()
            result.audit_chain_valid = verification.valid
            JSONExporter().export(
                {
                    "run": result.to_dict(),
                    "graph": graph_payload,
                    "audit": {
                        "chain_valid": verification.valid,
                        "entries": [self._provenance_dict(entry) for entry in self.provenance.storage.all()],
                    },
                    "compliance": compliance,
                },
                staged_paths[0],
            )

            artifact_payload = {
                "graph_sha256": self._graph_checksum(graph_payload),
                "artifacts": [
                    {"path": str(final), "sha256": self._file_checksum(staged)}
                    for staged, final in zip(staged_paths[:3], export_paths[:3], strict=True)
                ],
                "manifest_path": str(export_paths[3]),
            }
            export_entry = self.provenance.track(
                f"export:{run_id}",
                "export",
                artifact_payload,
                derived_from=[decision.decision_id],
            )
            verification = self.provenance.verify_chain()
            result.audit_chain_valid = verification.valid
            JSONExporter().export(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "graph_sha256": artifact_payload["graph_sha256"],
                    "artifacts": [
                        {"path": Path(item["path"]).name, "sha256": item["sha256"]}
                        for item in artifact_payload["artifacts"]
                    ],
                    "audit_chain_head": export_entry.checksum,
                    "export_entry": self._provenance_dict(export_entry),
                },
                staged_paths[3],
            )

            output.mkdir(parents=True, exist_ok=True)
            self._publish_with_rollback(staged_paths, export_paths, staging)
        return result

    def verify_artifacts(self) -> ChainVerification:
        errors: list[dict[str, Any]] = []
        checked = 0
        checked_packages: set[Path] = set()
        for entry in self.provenance.storage.all():
            if entry.entity_type != "export":
                continue
            checked += 1
            expected_graph_hash = entry.payload.get("graph_sha256")
            if expected_graph_hash != self._graph_checksum(self.graph.to_kg_dict()):
                errors.append({"path": "in-memory-graph", "reason": "graph_checksum_mismatch"})
            for artifact in entry.payload.get("artifacts", []):
                checked += 1
                path = Path(artifact["path"])
                if not path.is_file():
                    errors.append({"path": str(path), "reason": "artifact_missing"})
                elif self._file_checksum(path) != artifact["sha256"]:
                    errors.append({"path": str(path), "reason": "artifact_checksum_mismatch"})
            manifest_path = Path(entry.payload.get("manifest_path", ""))
            if manifest_path and manifest_path not in checked_packages:
                package = self.verify_export_package(manifest_path.parent)
                checked += package.checked
                errors.extend(package.errors)
                checked_packages.add(manifest_path)
        return ChainVerification(valid=not errors and checked > 0, checked=checked, errors=errors)

    @classmethod
    def verify_export_package(cls, output_dir: str | Path) -> ChainVerification:
        """Verify a persisted evidence package without a live pipeline instance."""
        output = Path(output_dir)
        manifest_path = output / "auditgraph.manifest.json"
        errors: list[dict[str, Any]] = []
        checked = 0
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            return ChainVerification(
                valid=False,
                checked=0,
                errors=[{"path": str(manifest_path), "reason": "manifest_unreadable", "detail": str(error)}],
            )

        export_entry = manifest.get("export_entry", {})
        export_payload = export_entry.get("payload", {}) if isinstance(export_entry, dict) else {}
        bound_artifacts = [
            {"path": Path(item.get("path", "")).name, "sha256": item.get("sha256")}
            for item in export_payload.get("artifacts", [])
            if isinstance(item, dict)
        ]
        manifest_artifacts = manifest.get("artifacts", [])
        if manifest_artifacts != bound_artifacts:
            errors.append({"path": str(manifest_path), "reason": "manifest_artifact_binding_mismatch"})
        required_names = {"auditgraph.json", "auditgraph.ttl", "auditgraph.html"}
        bound_names = {item["path"] for item in bound_artifacts}
        if bound_names != required_names:
            errors.append(
                {
                    "path": str(manifest_path),
                    "reason": "required_artifact_list_mismatch",
                    "expected": sorted(required_names),
                    "actual": sorted(bound_names),
                }
            )
        for artifact in bound_artifacts:
            checked += 1
            path = output / Path(artifact.get("path", "")).name
            if not path.is_file():
                errors.append({"path": str(path), "reason": "artifact_missing"})
            elif cls._file_checksum(path) != artifact.get("sha256"):
                errors.append({"path": str(path), "reason": "artifact_checksum_mismatch"})

        audit_json_path = output / "auditgraph.json"
        try:
            audit_json = json.loads(audit_json_path.read_text(encoding="utf-8"))
            checked += 1
            bound_graph_hash = export_payload.get("graph_sha256")
            if manifest.get("graph_sha256") != bound_graph_hash:
                errors.append({"path": str(manifest_path), "reason": "manifest_graph_binding_mismatch"})
            if cls._graph_checksum(audit_json.get("graph", {})) != bound_graph_hash:
                errors.append({"path": str(audit_json_path), "reason": "graph_checksum_mismatch"})
            chain_entries = list(audit_json.get("audit", {}).get("entries", []))
            chain_entries.append(export_entry)
            previous: str | None = None
            for expected_sequence, entry in enumerate(chain_entries, start=1):
                checked += 1
                if entry.get("sequence_id") != expected_sequence:
                    errors.append({"sequence_id": entry.get("sequence_id"), "reason": "sequence_gap"})
                if entry.get("previous_checksum") != previous:
                    errors.append(
                        {"sequence_id": entry.get("sequence_id"), "reason": "previous_checksum_mismatch"}
                    )
                if not verify_checksum(entry):
                    errors.append({"sequence_id": entry.get("sequence_id"), "reason": "checksum_mismatch"})
                previous = entry.get("checksum")
            if previous != manifest.get("audit_chain_head"):
                errors.append({"path": str(manifest_path), "reason": "audit_chain_head_mismatch"})
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as error:
            errors.append({"path": str(audit_json_path), "reason": "audit_json_unreadable", "detail": str(error)})

        return ChainVerification(valid=not errors and checked > 0, checked=checked, errors=errors)

    def _publish_with_rollback(
        self,
        staged_paths: list[Path],
        final_paths: list[Path],
        staging: Path,
    ) -> None:
        backups: dict[Path, Path] = {}
        published: list[Path] = []
        try:
            for index, final in enumerate(final_paths):
                if final.exists():
                    backup = staging / f"backup-{index}"
                    self._replace_path(final, backup)
                    backups[final] = backup
            for staged, final in zip(staged_paths, final_paths, strict=True):
                self._replace_path(staged, final)
                published.append(final)
        except Exception:
            for final in reversed(published):
                if final.is_file() or final.is_symlink():
                    final.unlink()
            for final, backup in backups.items():
                if backup.exists():
                    self._replace_path(backup, final)
            raise

    @staticmethod
    def _replace_path(source: Path, destination: Path) -> None:
        source.replace(destination)

    def _merge_graph(self, source: KnowledgeGraph) -> None:
        for node in source.nodes.values():
            self.graph.add_node(node.node_id, node.node_type, **node.properties)
        for edge in source.edges:
            self.graph.add_edge(edge.source, edge.target, edge.edge_type, **edge.properties)

    def _record_semantic_provenance(
        self, extraction: ExtractionResult
    ) -> dict[tuple[str, str], list[str]]:
        fact_index: dict[tuple[str, str], list[str]] = {}
        for relation in extraction.relations:
            parents = [relation.chunk_id or relation.source_id]
            self.provenance.track(relation.relation_id, "relation", relation.to_dict(), derived_from=parents)
        for event in extraction.events:
            self.graph.add_node(
                event.event_id,
                "event",
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                source_id=event.source_id,
            )
            for participant in event.participants:
                if participant not in self.graph.nodes:
                    self.graph.add_node(participant, "Entity")
                self.graph.add_edge(event.event_id, participant, "HAS_PARTICIPANT")
            self.provenance.track(
                event.event_id,
                "event",
                event.to_dict(),
                derived_from=[event.chunk_id or event.source_id],
            )
        for triplet in extraction.triplets:
            identity = json.dumps(
                [triplet.subject, triplet.predicate, triplet.object, triplet.source_id, triplet.chunk_id],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            fact_id = f"fact:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
            self.graph.add_node(
                fact_id,
                "fact",
                subject=triplet.subject,
                predicate=triplet.predicate,
                object=triplet.object,
                source_id=triplet.source_id,
                chunk_id=triplet.chunk_id,
            )
            if triplet.subject in self.graph.nodes:
                self.graph.add_edge(triplet.subject, fact_id, "HAS_FACT")
            self.provenance.track(
                fact_id,
                "fact",
                {
                    "subject": triplet.subject,
                    "predicate": triplet.predicate,
                    "object": triplet.object,
                    "source_id": triplet.source_id,
                },
                derived_from=[triplet.chunk_id or triplet.source_id],
            )
            fact_index.setdefault((triplet.subject, triplet.predicate), []).append(fact_id)
        return fact_index

    @staticmethod
    def _policy_sources(source_ref: str | None, documents: list[SourceDocument]) -> list[str]:
        if not source_ref:
            return []
        source_name = source_ref.split("#", 1)[0]
        return [document.source_id for document in documents if source_name in document.source_id]

    @staticmethod
    def _file_checksum(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _graph_checksum(graph_payload: dict[str, Any]) -> str:
        canonical = json.dumps(graph_payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _select_application(self, run_id: str) -> Any:
        applications = [node for node in self.graph.nodes.values() if node.node_type == "LoanApplication"]
        if not applications:
            raise PipelineStageError(run_id, "reasoning", "LoanApplication", "no application entity found")
        return applications[0]

    @staticmethod
    def _provenance_dict(entry: Any) -> dict[str, Any]:
        return {
            "sequence_id": entry.sequence_id,
            "entry_id": entry.entry_id,
            "entity_id": entry.entity_id,
            "entity_type": entry.entity_type,
            "payload": entry.payload,
            "derived_from": entry.derived_from,
            "agent_id": entry.agent_id,
            "timestamp": entry.timestamp,
            "previous_checksum": entry.previous_checksum,
            "checksum": entry.checksum,
            "invalidated": entry.invalidated,
            "invalidation_reason": entry.invalidation_reason,
        }
