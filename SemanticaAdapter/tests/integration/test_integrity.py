from hashlib import sha256
from pathlib import Path

import pytest

from semantica_adapter.domain.models import AuditExport
from semantica_adapter.services.integrity import (
    publish_export_package,
    verify_export_package,
)


def _exports(tmp_path: Path) -> tuple[AuditExport, AuditExport]:
    source = tmp_path / "source"
    source.mkdir()
    json_path = source / "decision-1.json"
    rdf_path = source / "decision-1.ttl"
    json_path.write_text('{"decision_id":"decision-1"}', encoding="utf-8")
    rdf_path.write_text("@prefix ex: <urn:example:> .\nex:d ex:outcome ex:review .\n", encoding="utf-8")
    return (
        AuditExport("decision-1", "json", json_path, sha256(json_path.read_bytes()).hexdigest()),
        AuditExport("decision-1", "turtle", rdf_path, sha256(rdf_path.read_bytes()).hexdigest()),
    )


def _package(tmp_path: Path) -> Path:
    target = tmp_path / "package"
    publish_export_package(
        _exports(tmp_path),
        target,
        backend_name="semantica",
        backend_version="0.6.6",
    )
    return target


def test_valid_export_package_passes_offline_verification(tmp_path) -> None:
    target = _package(tmp_path)
    result = verify_export_package(target)
    assert result.valid is True
    assert result.decision_id == "decision-1"
    assert result.errors == ()


def test_external_chain_head_can_anchor_offline_verification(tmp_path) -> None:
    package = publish_export_package(
        _exports(tmp_path),
        tmp_path / "package",
        backend_name="semantica",
        backend_version="0.6.6",
    )
    assert verify_export_package(
        package.output_dir, trusted_chain_head=package.audit_chain_head
    ).valid
    result = verify_export_package(package.output_dir, trusted_chain_head="0" * 64)
    assert result.valid is False
    assert "trusted chain head mismatch" in result.errors


@pytest.mark.parametrize("filename", ["decision-1.json", "decision-1.ttl"])
def test_artifact_tampering_is_detected(tmp_path, filename) -> None:
    target = _package(tmp_path)
    (target / filename).write_text("tampered", encoding="utf-8")
    result = verify_export_package(target)
    assert result.valid is False
    assert any("digest mismatch" in error for error in result.errors)


def test_missing_required_artifact_is_detected(tmp_path) -> None:
    target = _package(tmp_path)
    (target / "decision-1.ttl").unlink()
    result = verify_export_package(target)
    assert result.valid is False
    assert any("missing artifact" in error for error in result.errors)


def test_graph_digest_mismatch_is_detected(tmp_path) -> None:
    import json

    target = _package(tmp_path)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["graph_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = verify_export_package(target)
    assert result.valid is False
    assert any("graph_sha256" in error for error in result.errors)


def test_unknown_schema_version_is_rejected(tmp_path) -> None:
    import json

    target = _package(tmp_path)
    for filename in ("manifest.json", "audit-chain.json"):
        path = target / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = "999"
        path.write_text(json.dumps(payload), encoding="utf-8")
    result = verify_export_package(target)
    assert result.valid is False
    assert any("schema_version" in error for error in result.errors)


def test_chain_artifact_path_cannot_escape_package(tmp_path) -> None:
    import json

    target = _package(tmp_path)
    chain_path = target / "audit-chain.json"
    manifest_path = target / "manifest.json"
    chain = json.loads(chain_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = chain["entries"][0]["artifacts"].pop("decision-1.json")
    chain["entries"][0]["artifacts"]["../outside.json"] = metadata
    manifest["artifacts"] = chain["entries"][0]["artifacts"]
    # Other hashes deliberately remain stale: the verifier must still report
    # the unsafe path explicitly before it ever trusts package-controlled paths.
    chain_path.write_text(json.dumps(chain), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = verify_export_package(target)
    assert result.valid is False
    assert any("unsafe artifact path" in error for error in result.errors)


def test_deleting_manifest_entry_and_corresponding_file_still_fails(tmp_path) -> None:
    import json

    target = _package(tmp_path)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["artifacts"]["decision-1.ttl"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (target / "decision-1.ttl").unlink()
    result = verify_export_package(target)
    assert result.valid is False
    assert any("artifact list" in error for error in result.errors)


def test_publish_failure_restores_previous_package(tmp_path, monkeypatch) -> None:
    import semantica_adapter.services.integrity as integrity

    target = tmp_path / "package"
    target.mkdir()
    marker = target / "existing.txt"
    marker.write_text("previous package", encoding="utf-8")
    real_replace = integrity.os.replace

    def fail_final_publish(source, destination):
        if Path(source).name.startswith(".package.staging-") and Path(destination) == target:
            raise OSError("simulated publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(integrity.os, "replace", fail_final_publish)
    with pytest.raises(OSError, match="simulated"):
        publish_export_package(
            _exports(tmp_path),
            target,
            backend_name="semantica",
            backend_version="0.6.6",
        )
    assert marker.read_text(encoding="utf-8") == "previous package"
    assert list(target.iterdir()) == [marker]
