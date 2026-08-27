import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from auditgraph.core.models import Approval, SourceDocument
from auditgraph.pipeline import AuditPipeline, PipelineStageError, SourceSpec
from auditgraph.query import QueryService
from auditgraph.reasoning import Condition, Rule


class _SourceHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/regulation":
            payload = (
                "<html><body>ENTITY|REG-1|Regulation|Credit Risk Regulation<br>"
                "RELATION|POL-RISK-001:1.0|implements|REG-1</body></html>"
            ).encode()
            content_type = "text/html"
        else:
            payload = json.dumps(
                {"application_id": "A-1", "risk_score": 82, "risk_level": "high"}
            ).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def source_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SourceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _approval() -> Approval:
    return Approval("pending", "risk_manager", "email", "reviewed source evidence")


def test_real_ingestors_feed_the_decision_pipeline(tmp_path, source_server) -> None:
    policy_path = tmp_path / "policy.txt"
    policy_path.write_text(
        "ENTITY|POL-RISK-001:1.0|Policy|Manual Review Policy\n"
        "TRIPLE|POL-RISK-001:1.0|source_ref|policy.txt#article-3",
        encoding="utf-8",
    )
    db_path = tmp_path / "applications.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE applications (application_id TEXT, amount INTEGER)")
    connection.execute("INSERT INTO applications VALUES ('A-1', 100000)")
    connection.commit()
    connection.close()

    pipeline = AuditPipeline()
    result = pipeline.run_sources(
        [
            SourceSpec("file", str(policy_path)),
            SourceSpec("web", f"{source_server}/regulation", allow_private_network=True),
            SourceSpec("database", str(db_path), query="SELECT * FROM applications"),
            SourceSpec("api", f"{source_server}/risk", allow_private_network=True),
        ],
        output_dir=tmp_path / "output",
        approval=_approval(),
    )
    assert result.stage_counts["documents"] == 4
    assert QueryService(pipeline.graph, pipeline.provenance).get_lineage(result.decision_id)
    payload = json.loads((tmp_path / "output" / "auditgraph.json").read_text(encoding="utf-8"))
    decision = next(node for node in payload["graph"]["entities"] if node["id"] == result.decision_id)
    assert decision["properties"]["outcome"] == "manual_review"


def test_missing_decision_fact_fails_closed(tmp_path) -> None:
    documents = [
        SourceDocument("file:policy", "file", "ENTITY|POL-RISK-001:1.0|Policy|Policy"),
        SourceDocument("database:application", "database", "ENTITY|A-1|LoanApplication|Application A-1"),
    ]
    pipeline = AuditPipeline()
    result = pipeline.run(documents, output_dir=tmp_path)
    decision = QueryService(pipeline.graph, pipeline.provenance).get_decision(result.decision_id)
    assert decision["properties"]["outcome"] == "manual_review"
    assert result.compliant is False
    assert result.stage_counts["approvals"] == 0


def test_conflicting_decision_fact_fails_closed(tmp_path) -> None:
    documents = [
        SourceDocument("file:policy", "file", "ENTITY|POL-RISK-001:1.0|Policy|Policy"),
        SourceDocument(
            "database:application",
            "database",
            "ENTITY|A-1|LoanApplication|Application A-1\n"
            "TRIPLE|A-1|risk_score|20\nTRIPLE|A-1|risk_score|82",
        ),
    ]
    pipeline = AuditPipeline()
    result = pipeline.run(documents, output_dir=tmp_path, approval=_approval())
    decision = QueryService(pipeline.graph, pipeline.provenance).get_decision(result.decision_id)
    assert decision["properties"]["outcome"] == "manual_review"
    assert result.compliant is False


def test_pipeline_runs_are_isolated(tmp_path) -> None:
    pipeline = AuditPipeline()
    first = [
        SourceDocument("file:p1", "file", "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|82")
    ]
    second = [
        SourceDocument("file:p2", "file", "ENTITY|B-1|LoanApplication|Application B-1\nTRIPLE|B-1|risk_score|20")
    ]
    pipeline.run(first, output_dir=tmp_path / "first", approval=_approval())
    second_result = pipeline.run(second, output_dir=tmp_path / "second", approval=_approval())
    decision = QueryService(pipeline.graph, pipeline.provenance).get_decision(second_result.decision_id)
    assert decision["properties"]["scenario"] == "Decision for B-1"
    assert decision["properties"]["outcome"] == "approved"
    assert pipeline.graph.get_node("A-1") is None


def test_entity_merge_rewrites_fact_references(tmp_path) -> None:
    pipeline = AuditPipeline()
    result = pipeline.run(
        [
            SourceDocument("file:canonical", "file", "ENTITY|A-1|LoanApplication|Application A-1"),
            SourceDocument(
                "api:alias",
                "api",
                "ENTITY|A-ALIAS|LoanApplication|Application A-1\nTRIPLE|A-ALIAS|risk_score|82",
            ),
        ],
        output_dir=tmp_path,
        approval=_approval(),
    )
    decision = QueryService(pipeline.graph, pipeline.provenance).get_decision(result.decision_id)
    assert decision["properties"]["scenario"] == "Decision for A-1"
    assert decision["properties"]["outcome"] == "manual_review"
    assert pipeline.graph.get_node("A-ALIAS") is None


def test_decision_lineage_contains_exact_fact_and_artifact_hashes(tmp_path) -> None:
    pipeline = AuditPipeline()
    result = pipeline.run(
        [
            SourceDocument(
                "file:application",
                "file",
                "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|82",
            )
        ],
        output_dir=tmp_path,
        approval=_approval(),
    )
    lineage = pipeline.provenance.trace(result.decision_id)
    fact_entries = [entry for entry in lineage if entry.entity_type == "fact"]
    assert fact_entries
    assert fact_entries[0].payload["predicate"] == "risk_score"
    assert fact_entries[0].payload["object"] == 82
    assert any(entry.entity_type == "chunk" for entry in lineage)
    assert any(entry.entity_type == "source" for entry in lineage)
    assert pipeline.verify_artifacts().valid is True

    ttl_path = next(path for path in result.exports if path.suffix == ".ttl")
    ttl_path.write_text(ttl_path.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    assert pipeline.verify_artifacts().valid is False


def test_json_export_and_in_memory_graph_are_covered_by_artifact_verification(tmp_path) -> None:
    pipeline = AuditPipeline()
    result = pipeline.run(
        [
            SourceDocument(
                "file:application",
                "file",
                "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|20",
            )
        ],
        output_dir=tmp_path,
        approval=_approval(),
    )
    json_path = next(path for path in result.exports if path.suffix == ".json")
    manifest_path = tmp_path / "auditgraph.manifest.json"
    assert manifest_path.is_file()
    assert AuditPipeline.verify_export_package(tmp_path).valid is True
    json_path.write_text(json_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    verification = pipeline.verify_artifacts()
    assert verification.valid is False
    assert any(error["reason"] == "artifact_checksum_mismatch" for error in verification.errors)
    assert AuditPipeline.verify_export_package(tmp_path).valid is False

    pipeline = AuditPipeline()
    pipeline.run(
        [
            SourceDocument(
                "file:application",
                "file",
                "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|20",
            )
        ],
        output_dir=tmp_path / "graph",
        approval=_approval(),
    )
    pipeline.graph.nodes["A-1"].properties["risk_score"] = 999
    verification = pipeline.verify_artifacts()
    assert verification.valid is False
    assert any(error["reason"] == "graph_checksum_mismatch" for error in verification.errors)


def test_failed_export_does_not_leave_partial_artifacts(tmp_path, monkeypatch) -> None:
    output = tmp_path / "output"

    def fail_export(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr("auditgraph.pipeline.audit_pipeline.HTMLVisualizer.export", fail_export)
    with pytest.raises(PipelineStageError):
        AuditPipeline().run(
            [
                SourceDocument(
                    "file:application",
                    "file",
                    "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|20",
                )
            ],
            output_dir=output,
            approval=_approval(),
        )
    assert not output.exists() or not list(output.iterdir())


def test_publish_failure_rolls_back_all_artifacts(tmp_path, monkeypatch) -> None:
    output = tmp_path / "output"
    output.mkdir()
    original_replace = AuditPipeline._replace_path
    failed = False

    def fail_during_publish(source, destination):
        nonlocal failed
        if not failed and destination.parent == output and destination.name == "auditgraph.ttl":
            failed = True
            raise OSError("publish failed")
        return original_replace(source, destination)

    monkeypatch.setattr(AuditPipeline, "_replace_path", staticmethod(fail_during_publish))
    with pytest.raises(PipelineStageError):
        AuditPipeline().run(
            [
                SourceDocument(
                    "file:application",
                    "file",
                    "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|20",
                )
            ],
            output_dir=output,
            approval=_approval(),
        )
    assert not list(output.iterdir())


def test_offline_verifier_uses_chain_bound_required_artifact_list(tmp_path) -> None:
    AuditPipeline().run(
        [
            SourceDocument(
                "file:application",
                "file",
                "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|20",
            )
        ],
        output_dir=tmp_path,
        approval=_approval(),
    )
    manifest_path = tmp_path / "auditgraph.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "auditgraph.ttl").unlink()
    (tmp_path / "auditgraph.html").unlink()

    verification = AuditPipeline.verify_export_package(tmp_path)
    assert verification.valid is False
    reasons = {error["reason"] for error in verification.errors}
    assert "manifest_artifact_binding_mismatch" in reasons
    assert "artifact_missing" in reasons


def test_failed_run_does_not_leave_partial_graph_state(tmp_path) -> None:
    pipeline = AuditPipeline()
    with pytest.raises(PipelineStageError) as caught:
        pipeline.run(
            [SourceDocument("file:policy", "file", "ENTITY|POL-1|Policy|Policy")],
            output_dir=tmp_path,
        )
    assert caught.value.run_id != "unknown"
    assert pipeline.graph.to_kg_dict() == {"entities": [], "relationships": []}


def test_explicit_empty_rules_disable_default_policy(tmp_path) -> None:
    pipeline = AuditPipeline()
    result = pipeline.run(
        [
            SourceDocument(
                "file:application",
                "file",
                "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|82",
            )
        ],
        output_dir=tmp_path,
        rules=[],
        approval=_approval(),
    )
    decision = QueryService(pipeline.graph, pipeline.provenance).get_decision(result.decision_id)
    assert decision["properties"]["outcome"] == "approved"
    assert decision["properties"]["metadata"]["rule_matches"] == []


def test_custom_rule_missing_required_fact_fails_closed(tmp_path) -> None:
    pipeline = AuditPipeline()
    result = pipeline.run(
        [
            SourceDocument(
                "file:application",
                "file",
                "ENTITY|A-1|LoanApplication|Application A-1\nTRIPLE|A-1|risk_score|20",
            )
        ],
        output_dir=tmp_path,
        rules=[Rule("POL-COUNTRY", "1.0", [Condition("country", "in", ["IR", "KP"])], "manual_review")],
        approval=_approval(),
    )
    decision = QueryService(pipeline.graph, pipeline.provenance).get_decision(result.decision_id)
    assert decision["properties"]["outcome"] == "manual_review"
    assert decision["properties"]["metadata"]["missing_decision_fields"] == ["country"]
    assert result.compliant is False


def test_alias_conflict_on_custom_rule_field_fails_closed(tmp_path) -> None:
    pipeline = AuditPipeline()
    result = pipeline.run(
        [
            SourceDocument("file:canonical", "file", "ENTITY|A-1|LoanApplication|Application A-1"),
            SourceDocument(
                "api:alias",
                "api",
                "ENTITY|ALT|LoanApplication|Application A-1\n"
                "TRIPLE|ALT|risk_score|20\nTRIPLE|ALT|country|IR\nTRIPLE|ALT|country|US",
            ),
        ],
        output_dir=tmp_path,
        rules=[Rule("POL-COUNTRY", "1.0", [Condition("country", "in", ["IR", "KP"])], "manual_review")],
        approval=_approval(),
    )
    decision = QueryService(pipeline.graph, pipeline.provenance).get_decision(result.decision_id)
    assert decision["properties"]["outcome"] == "manual_review"
    assert decision["properties"]["metadata"]["critical_conflict_ids"]
    assert result.compliant is False
