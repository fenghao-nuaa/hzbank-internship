import json

import pytest

from auditgraph.core.models import Approval, SourceDocument
from auditgraph.pipeline import AuditPipeline, PipelineStageError
from auditgraph.query import QueryService


def _documents() -> list[SourceDocument]:
    return [
        SourceDocument(
            "file:policy.txt",
            "file",
            "\n".join(
                [
                    "ENTITY|POL-RISK-001:1.0|Policy|Manual Review Policy",
                    "TRIPLE|POL-RISK-001:1.0|source_ref|policy.txt#article-3",
                ]
            ),
        ),
        SourceDocument(
            "web:regulation",
            "web",
            "ENTITY|REG-1|Regulation|Credit Risk Regulation\nRELATION|POL-RISK-001:1.0|implements|REG-1",
        ),
        SourceDocument(
            "database:applications",
            "database",
            "\n".join(
                [
                    "ENTITY|A-1|LoanApplication|Application A-1",
                    "TRIPLE|A-1|risk_score|82",
                    "TRIPLE|A-1|risk_level|medium",
                ]
            ),
        ),
        SourceDocument(
            "api:risk-system",
            "api",
            "\n".join(
                [
                    "ENTITY|A-ALIAS|LoanApplication|Application A-1",
                    "EVENT|risk_assessed|A-1|2026-08-24",
                    "TRIPLE|A-1|risk_level|high",
                ]
            ),
        ),
    ]


def test_end_to_end_pipeline_produces_auditable_decision(tmp_path) -> None:
    pipeline = AuditPipeline()
    result = pipeline.run(
        _documents(),
        output_dir=tmp_path,
        approval=Approval("pending", "risk_manager", "email", "reviewed evidence"),
    )

    assert result.stage_counts["documents"] == 4
    assert result.stage_counts["entities"] >= 3
    assert result.stage_counts["conflicts"] == 1
    assert result.decision_id
    assert result.compliant is True
    assert result.audit_chain_valid is True
    assert {path.suffix for path in result.exports} == {".json", ".ttl", ".html"}
    assert all(path.exists() and path.stat().st_size > 0 for path in result.exports)

    service = QueryService(pipeline.graph, pipeline.provenance)
    decision = service.get_decision(result.decision_id)
    assert decision["properties"]["outcome"] == "manual_review"
    assert service.get_lineage(result.decision_id)
    assert service.compliance_report(result.decision_id)["compliant"] is True

    graph_export = next(path for path in result.exports if path.suffix == ".json")
    payload = json.loads(graph_export.read_text(encoding="utf-8"))
    assert payload["run"]["decision_id"] == result.decision_id
    assert payload["audit"]["chain_valid"] is True


def test_pipeline_rejects_empty_sources(tmp_path) -> None:
    with pytest.raises(PipelineStageError, match="ingest"):
        AuditPipeline().run([], output_dir=tmp_path)
