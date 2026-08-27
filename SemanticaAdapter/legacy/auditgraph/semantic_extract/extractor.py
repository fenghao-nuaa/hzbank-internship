"""Rule-based extraction compatible with AuditGraph's auditable test format."""

import hashlib
import json
from typing import Any

from auditgraph.core.models import Chunk, Entity, Event, ExtractionResult, Relation, Triplet


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _coerce(value: str) -> Any:
    value = value.strip()
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


class SemanticExtractor:
    """Extract explicit, deterministic semantic records from line-oriented text.

    Supported lines are `ENTITY|id|type|name`, `RELATION|subject|predicate|object`,
    `EVENT|type|participant[,participant]|date`, and
    `TRIPLE|subject|predicate|object`.
    """

    def extract(self, chunks: list[Chunk]) -> ExtractionResult:
        result = ExtractionResult()
        entities_by_id: dict[str, Entity] = {}
        for chunk in chunks:
            if chunk.content.lstrip().startswith(("{", "[")):
                try:
                    self._extract_json(chunk, json.loads(chunk.content), result, entities_by_id)
                    continue
                except json.JSONDecodeError:
                    pass
            for raw_line in chunk.content.splitlines():
                parts = [part.strip() for part in raw_line.split("|")]
                if not parts:
                    continue
                record_type = parts[0].upper()
                if record_type == "ENTITY" and len(parts) == 4:
                    entity_id, entity_type, name = parts[1:]
                    self._add_entity(
                        Entity(
                            entity_id,
                            name,
                            entity_type,
                            source_ids={chunk.source_id},
                            properties={"chunk_ids": [chunk.chunk_id]},
                        ),
                        result,
                        entities_by_id,
                    )
                elif record_type == "RELATION" and len(parts) == 4:
                    subject, predicate, object_id = parts[1:]
                    result.relations.append(
                        Relation(
                            relation_id=_stable_id("relation", subject, predicate, object_id, chunk.source_id),
                            subject_id=subject,
                            predicate=predicate,
                            object_id=object_id,
                            source_id=chunk.source_id,
                            chunk_id=chunk.chunk_id,
                        )
                    )
                elif record_type == "EVENT" and len(parts) >= 3:
                    event_type = parts[1]
                    participants = [item.strip() for item in parts[2].split(",") if item.strip()]
                    occurred_at = parts[3] if len(parts) > 3 and parts[3] else None
                    result.events.append(
                        Event(
                            event_id=_stable_id("event", event_type, *participants, occurred_at or ""),
                            event_type=event_type,
                            participants=participants,
                            source_id=chunk.source_id,
                            occurred_at=occurred_at,
                            chunk_id=chunk.chunk_id,
                        )
                    )
                elif record_type == "TRIPLE" and len(parts) == 4:
                    result.triplets.append(
                        Triplet(
                            parts[1],
                            parts[2],
                            _coerce(parts[3]),
                            chunk.source_id,
                            chunk_id=chunk.chunk_id,
                        )
                    )
        return result

    def _extract_json(
        self,
        chunk: Chunk,
        payload: Any,
        result: ExtractionResult,
        entities_by_id: dict[str, Entity],
    ) -> None:
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if not isinstance(record, dict):
                continue
            entity_id = str(record.get("application_id") or record.get("entity_id") or "").strip()
            if not entity_id:
                continue
            entity_type = str(record.get("entity_type") or "LoanApplication")
            name = str(record.get("name") or f"Application {entity_id}")
            self._add_entity(
                Entity(
                    entity_id,
                    name,
                    entity_type,
                    source_ids={chunk.source_id},
                    properties={"chunk_ids": [chunk.chunk_id]},
                ),
                result,
                entities_by_id,
            )
            ignored = {"application_id", "entity_id", "entity_type", "name"}
            for key, value in sorted(record.items()):
                if key in ignored:
                    continue
                result.triplets.append(
                    Triplet(entity_id, key, value, chunk.source_id, chunk_id=chunk.chunk_id)
                )

    @staticmethod
    def _add_entity(
        entity: Entity,
        result: ExtractionResult,
        entities_by_id: dict[str, Entity],
    ) -> None:
        existing = entities_by_id.get(entity.entity_id)
        if existing is None:
            entities_by_id[entity.entity_id] = entity
            result.entities.append(entity)
            return
        existing.source_ids.update(entity.source_ids)
        existing.aliases.update(entity.aliases)
        existing_chunks = existing.properties.setdefault("chunk_ids", [])
        for chunk_id in entity.properties.get("chunk_ids", []):
            if chunk_id not in existing_chunks:
                existing_chunks.append(chunk_id)
