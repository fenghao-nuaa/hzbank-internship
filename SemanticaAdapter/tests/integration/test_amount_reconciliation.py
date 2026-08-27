import ast
from pathlib import Path

from semantica_adapter import (
    AuditStatus,
    create_local_semantica_service,
    verify_export_package,
)

from examples.amount_reconciliation import run_amount_reconciliation


def test_amount_reconciliation_agent_uses_only_adapter_api(tmp_path) -> None:
    source = Path("examples/amount_reconciliation.py").read_text(encoding="utf-8")
    imported_modules = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "semantica" not in imported_modules
    assert not any(module.startswith("semantica.") for module in imported_modules)

    service = create_local_semantica_service(
        authorized_actors={("risk-manager", "reviewer")},
        provenance_storage_path=tmp_path / "provenance.db",
    )
    result = run_amount_reconciliation(service, tmp_path / "audit-package")

    assert result.decision.status is AuditStatus.PENDING_APPROVAL
    assert result.trace.decision_status is AuditStatus.APPROVED
    assert result.trace.profile_version == "1.0"
    assert result.trace.rule_set_version == "2026.08"
    assert {item.evidence_id for item in result.trace.evidence} == {
        "ledger-1",
        "voucher-1",
    }
    assert len(result.trace.approvals) == 1
    assert result.json_export.path.exists()
    assert result.rdf_export.path.exists()
    assert ":" not in result.json_export.path.name
    assert ":" not in result.rdf_export.path.name
    assert result.package.manifest_path.exists()
    assert verify_export_package(result.package.output_dir).valid is True
