"""In-memory and SQLite storage for append-only provenance entries."""

from __future__ import annotations

import copy
import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ProvenanceEntry:
    sequence_id: int
    entry_id: str
    entity_id: str
    entity_type: str
    payload: dict[str, Any]
    derived_from: list[str] = field(default_factory=list)
    agent_id: str = "auditgraph"
    timestamp: str = ""
    previous_checksum: str | None = None
    checksum: str = ""
    invalidated: bool = False
    invalidation_reason: str | None = None


class ProvenanceStorage(ABC):
    @abstractmethod
    def append(self, entry: ProvenanceEntry) -> None: ...

    @abstractmethod
    def all(self) -> list[ProvenanceEntry]: ...

    @abstractmethod
    def unsafe_update_payload(self, entity_id: str, payload: dict[str, Any]) -> None: ...

    def close(self) -> None:
        return


class InMemoryStorage(ProvenanceStorage):
    def __init__(self) -> None:
        self._entries: list[ProvenanceEntry] = []

    def append(self, entry: ProvenanceEntry) -> None:
        self._entries.append(copy.deepcopy(entry))

    def all(self) -> list[ProvenanceEntry]:
        return copy.deepcopy(self._entries)

    def unsafe_update_payload(self, entity_id: str, payload: dict[str, Any]) -> None:
        for entry in reversed(self._entries):
            if entry.entity_id == entity_id:
                entry.payload = copy.deepcopy(payload)
                return
        raise KeyError(f"unknown provenance entity: {entity_id}")


class SQLiteStorage(ProvenanceStorage):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS provenance (
                sequence_id INTEGER PRIMARY KEY,
                entry_id TEXT NOT NULL UNIQUE,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                derived_json TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                previous_checksum TEXT,
                checksum TEXT NOT NULL,
                invalidated INTEGER NOT NULL,
                invalidation_reason TEXT
            )
            """
        )
        self.connection.commit()

    def append(self, entry: ProvenanceEntry) -> None:
        self.connection.execute(
            """
            INSERT INTO provenance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.sequence_id,
                entry.entry_id,
                entry.entity_id,
                entry.entity_type,
                json.dumps(entry.payload, ensure_ascii=False, sort_keys=True),
                json.dumps(entry.derived_from, ensure_ascii=False),
                entry.agent_id,
                entry.timestamp,
                entry.previous_checksum,
                entry.checksum,
                int(entry.invalidated),
                entry.invalidation_reason,
            ),
        )
        self.connection.commit()

    def all(self) -> list[ProvenanceEntry]:
        rows = self.connection.execute("SELECT * FROM provenance ORDER BY sequence_id").fetchall()
        return [
            ProvenanceEntry(
                sequence_id=row[0],
                entry_id=row[1],
                entity_id=row[2],
                entity_type=row[3],
                payload=json.loads(row[4]),
                derived_from=json.loads(row[5]),
                agent_id=row[6],
                timestamp=row[7],
                previous_checksum=row[8],
                checksum=row[9],
                invalidated=bool(row[10]),
                invalidation_reason=row[11],
            )
            for row in rows
        ]

    def unsafe_update_payload(self, entity_id: str, payload: dict[str, Any]) -> None:
        row = self.connection.execute(
            "SELECT sequence_id FROM provenance WHERE entity_id = ? ORDER BY sequence_id DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"unknown provenance entity: {entity_id}")
        self.connection.execute(
            "UPDATE provenance SET payload_json = ? WHERE sequence_id = ?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), row[0]),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
