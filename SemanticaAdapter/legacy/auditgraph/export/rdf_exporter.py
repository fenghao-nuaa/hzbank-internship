"""Dependency-free Turtle export with PROV-O-compatible lineage predicates."""

import hashlib
import json
from pathlib import Path
from typing import Any


def _iri(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"audit:{digest}"


def _literal(value: Any) -> str:
    serialized = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    escaped = serialized.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


class RDFExporter:
    def export(
        self,
        graph: dict[str, list[dict[str, Any]]],
        provenance_entries: list[Any],
        path: str | Path,
    ) -> Path:
        lines = [
            "@prefix audit: <https://auditgraph.local/resource/> .",
            "@prefix prov: <http://www.w3.org/ns/prov#> .",
            "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
            "",
        ]
        for node in graph["entities"]:
            subject = _iri(node["id"])
            lines.append(f"{subject} rdf:type prov:Entity ;")
            lines.append(f"  audit:externalId {_literal(node['id'])} ;")
            lines.append(f"  audit:nodeType {_literal(node['type'])} .")
        for edge in graph["relationships"]:
            lines.append(
                f"{_iri(edge['source'])} audit:relatedTo {_iri(edge['target'])} ; "
                f"audit:edgeType {_literal(edge['type'])} ."
            )
        for entry in provenance_entries:
            subject = _iri(entry.entity_id)
            for parent in entry.derived_from:
                lines.append(f"{subject} prov:wasDerivedFrom {_iri(parent)} .")
            lines.append(f"{subject} audit:checksum {_literal(entry.checksum)} .")
            if entry.invalidated:
                lines.append(f"{subject} audit:invalidated \"true\" .")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target
