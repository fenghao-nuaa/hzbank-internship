"""W3C PROV-inspired lineage management with a verifiable append-only chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .integrity import compute_checksum, verify_checksum
from .storage import InMemoryStorage, ProvenanceEntry, ProvenanceStorage


@dataclass(slots=True)
class ChainVerification:
    valid: bool
    checked: int
    errors: list[dict[str, Any]] = field(default_factory=list)


class ProvenanceManager:
    def __init__(self, storage: ProvenanceStorage | None = None) -> None:
        self.storage = storage or InMemoryStorage()

    def track(
        self,
        entity_id: str,
        entity_type: str,
        payload: dict[str, Any],
        *,
        derived_from: list[str] | None = None,
        agent_id: str = "auditgraph",
        invalidated: bool = False,
        invalidation_reason: str | None = None,
    ) -> ProvenanceEntry:
        if not entity_id or not entity_type:
            raise ValueError("entity_id and entity_type are required")
        entries = self.storage.all()
        previous = entries[-1].checksum if entries else None
        entry = ProvenanceEntry(
            sequence_id=len(entries) + 1,
            entry_id=f"prov:{uuid4()}",
            entity_id=entity_id,
            entity_type=entity_type,
            payload=dict(payload),
            derived_from=list(derived_from or []),
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            previous_checksum=previous,
            invalidated=invalidated,
            invalidation_reason=invalidation_reason,
        )
        entry.checksum = compute_checksum(entry)
        self.storage.append(entry)
        return entry

    def trace(self, entity_id: str) -> list[ProvenanceEntry]:
        entries = self.storage.all()
        latest: dict[str, ProvenanceEntry] = {}
        by_entry_id: dict[str, ProvenanceEntry] = {}
        for entry in entries:
            latest[entry.entity_id] = entry
            by_entry_id[entry.entry_id] = entry
        if entity_id not in latest and entity_id not in by_entry_id:
            raise KeyError(f"unknown provenance entity: {entity_id}")
        result: list[ProvenanceEntry] = []
        visited: set[str] = set()

        def visit(reference: str) -> None:
            entry = by_entry_id.get(reference) or latest.get(reference)
            if entry is None or entry.entry_id in visited:
                return
            visited.add(entry.entry_id)
            result.append(entry)
            for parent in entry.derived_from:
                visit(parent)

        visit(entity_id)
        return result

    def invalidate(self, entity_id: str, *, agent_id: str, reason: str) -> ProvenanceEntry:
        current = self.trace(entity_id)[0]
        return self.track(
            entity_id,
            current.entity_type,
            dict(current.payload),
            derived_from=[current.entry_id],
            agent_id=agent_id,
            invalidated=True,
            invalidation_reason=reason,
        )

    def verify_chain(self) -> ChainVerification:
        entries = self.storage.all()
        errors: list[dict[str, Any]] = []
        previous: str | None = None
        for expected_sequence, entry in enumerate(entries, start=1):
            if entry.sequence_id != expected_sequence:
                errors.append(
                    {
                        "sequence_id": entry.sequence_id,
                        "reason": "sequence_gap",
                        "expected": expected_sequence,
                    }
                )
            if entry.previous_checksum != previous:
                errors.append(
                    {
                        "sequence_id": entry.sequence_id,
                        "reason": "previous_checksum_mismatch",
                    }
                )
            if not verify_checksum(entry):
                errors.append({"sequence_id": entry.sequence_id, "reason": "checksum_mismatch"})
            previous = entry.checksum
        return ChainVerification(valid=not errors, checked=len(entries), errors=errors)
