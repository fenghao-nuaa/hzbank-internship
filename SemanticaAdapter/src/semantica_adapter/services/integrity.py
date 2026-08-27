"""Detached manifests and offline verification for exported audit packages."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from semantica_adapter.domain.errors import AuditIntegrityError
from semantica_adapter.domain.models import AuditExport


SCHEMA_VERSION = "1.0"
MANIFEST_NAME = "manifest.json"
CHAIN_NAME = "audit-chain.json"


@dataclass(frozen=True, slots=True)
class AuditPackage:
    output_dir: Path
    manifest_path: Path
    chain_path: Path
    decision_id: str
    audit_chain_head: str
    exports: tuple[AuditExport, ...]


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    valid: bool
    decision_id: str | None
    errors: tuple[str, ...] = ()


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _graph_digest(artifacts: Mapping[str, Mapping[str, str]]) -> str:
    return sha256(_canonical(dict(artifacts))).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def publish_export_package(
    exports: Iterable[AuditExport],
    output_dir: Path,
    *,
    backend_name: str,
    backend_version: str,
) -> AuditPackage:
    """Publish JSON/RDF exports and their detached integrity records atomically."""

    export_items = tuple(exports)
    if not export_items:
        raise AuditIntegrityError("at least one export is required")
    decision_ids = {item.decision_id for item in export_items}
    if len(decision_ids) != 1:
        raise AuditIntegrityError("all exports must belong to the same decision")
    formats = {item.format for item in export_items}
    if "json" not in formats or not formats.intersection({"turtle", "ttl", "rdf"}):
        raise AuditIntegrityError("audit package requires JSON and RDF artifacts")

    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent)
    )
    backup = output_dir.parent / f".{output_dir.name}.backup-{uuid4().hex}"
    previous_moved = False
    published = False
    try:
        artifacts: dict[str, dict[str, str]] = {}
        for item in export_items:
            source = Path(item.path)
            if not source.is_file():
                raise AuditIntegrityError(f"missing source export: {source}")
            actual_digest = _digest(source)
            if actual_digest != item.sha256:
                raise AuditIntegrityError(f"source export digest mismatch: {source.name}")
            if source.name in artifacts:
                raise AuditIntegrityError(f"duplicate artifact name: {source.name}")
            shutil.copy2(source, staging / source.name)
            artifacts[source.name] = {"format": item.format, "sha256": actual_digest}

        decision_id = next(iter(decision_ids))
        graph_sha256 = _graph_digest(artifacts)
        chain_entry: dict[str, Any] = {
            "event_type": "EXPORT_PACKAGE",
            "sequence": 1,
            "decision_id": decision_id,
            "backend_name": backend_name,
            "backend_version": backend_version,
            "graph_sha256": graph_sha256,
            "artifacts": artifacts,
            "previous_hash": "0" * 64,
        }
        chain_head = sha256(_canonical(chain_entry)).hexdigest()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": decision_id,
            "backend_name": backend_name,
            "backend_version": backend_version,
            "graph_sha256": graph_sha256,
            "artifacts": artifacts,
            "audit_chain_head": chain_head,
        }
        _write_json(staging / CHAIN_NAME, {"schema_version": SCHEMA_VERSION, "entries": [chain_entry]})
        _write_json(staging / MANIFEST_NAME, manifest)

        if output_dir.exists():
            os.replace(output_dir, backup)
            previous_moved = True
        os.replace(staging, output_dir)
        published = True
        if previous_moved:
            shutil.rmtree(backup)
        return AuditPackage(
            output_dir=output_dir,
            manifest_path=output_dir / MANIFEST_NAME,
            chain_path=output_dir / CHAIN_NAME,
            decision_id=decision_id,
            audit_chain_head=chain_head,
            exports=tuple(
                AuditExport(
                    item.decision_id,
                    item.format,
                    output_dir / Path(item.path).name,
                    item.sha256,
                    item.generated_at,
                )
                for item in export_items
            ),
        )
    except Exception:
        if published and output_dir.exists():
            shutil.rmtree(output_dir)
        if previous_moved and backup.exists():
            os.replace(backup, output_dir)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists() and not previous_moved:
            shutil.rmtree(backup)


def verify_export_package(
    output_dir: Path, *, trusted_chain_head: str | None = None
) -> IntegrityResult:
    """Verify an audit package without contacting Semantica or another service."""

    output_dir = Path(output_dir)
    errors: list[str] = []
    manifest_path = output_dir / MANIFEST_NAME
    chain_path = output_dir / CHAIN_NAME
    if not manifest_path.is_file():
        errors.append(f"missing {MANIFEST_NAME}")
    if not chain_path.is_file():
        errors.append(f"missing {CHAIN_NAME}")
    if errors:
        return IntegrityResult(False, None, tuple(errors))

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        chain = json.loads(chain_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return IntegrityResult(False, None, (f"invalid integrity metadata: {error}",))

    decision_id = manifest.get("decision_id")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported manifest schema_version")
    if chain.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported audit chain schema_version")
    entries = chain.get("entries", [])
    if len(entries) != 1 or not isinstance(entries[0], dict):
        return IntegrityResult(False, decision_id, ("invalid audit chain entry count",))
    entry = entries[0]
    expected_head = sha256(_canonical(entry)).hexdigest()
    if trusted_chain_head is not None and expected_head != trusted_chain_head:
        errors.append("trusted chain head mismatch")
    if manifest.get("audit_chain_head") != expected_head:
        errors.append("audit_chain_head mismatch")

    for key in (
        "decision_id",
        "backend_name",
        "backend_version",
        "graph_sha256",
    ):
        if manifest.get(key) != entry.get(key):
            errors.append(f"{key} does not match chain-bound export entry")

    manifest_artifacts = manifest.get("artifacts")
    chain_artifacts = entry.get("artifacts")
    if not isinstance(manifest_artifacts, dict) or not isinstance(chain_artifacts, dict):
        errors.append("invalid artifact list")
        return IntegrityResult(False, decision_id, tuple(errors))
    if manifest_artifacts != chain_artifacts:
        errors.append("manifest artifact list does not match chain-bound artifact list")

    bound_formats = {
        item.get("format") for item in chain_artifacts.values() if isinstance(item, dict)
    }
    if "json" not in bound_formats or not bound_formats.intersection({"turtle", "ttl", "rdf"}):
        errors.append("chain-bound artifact list must include JSON and RDF")

    computed_graph_digest = _graph_digest(chain_artifacts)
    if entry.get("graph_sha256") != computed_graph_digest:
        errors.append("chain-bound graph_sha256 mismatch")
    if manifest.get("graph_sha256") != computed_graph_digest:
        errors.append("manifest graph_sha256 mismatch")

    for filename, metadata in chain_artifacts.items():
        relative_path = Path(filename)
        if (
            relative_path.is_absolute()
            or relative_path.name != filename
            or ".." in relative_path.parts
        ):
            errors.append(f"unsafe artifact path: {filename}")
            continue
        artifact = output_dir / filename
        if artifact.is_symlink():
            errors.append(f"unsafe artifact symlink: {filename}")
            continue
        if not artifact.is_file():
            errors.append(f"missing artifact: {filename}")
            continue
        expected_digest = metadata.get("sha256") if isinstance(metadata, dict) else None
        if _digest(artifact) != expected_digest:
            errors.append(f"artifact digest mismatch: {filename}")

    return IntegrityResult(not errors, decision_id, tuple(errors))
