"""Append-only, disk-verifiable task event ledger."""

from dataclasses import asdict
import json
import os
from pathlib import Path
from threading import RLock

from dream.core.events import TaskCompletedEvent
from dream.core.scope import ScopeIds


class EventLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def append(self, event: TaskCompletedEvent) -> None:
        with self._lock:
            if any(existing.event_id == event.event_id for existing in self.read_all()):
                raise ValueError(f"duplicate event_id: {event.event_id}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(
                asdict(event), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def contains(self, event_id: str) -> bool:
        return any(event.event_id == event_id for event in self.read_all())

    def read_all(self) -> list[TaskCompletedEvent]:
        with self._lock:
            if not self.path.exists():
                return []
            events: list[TaskCompletedEvent] = []
            seen: set[str] = set()
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    event_id = str(raw["event_id"])
                    if event_id in seen:
                        raise ValueError(
                            f"duplicate event_id at line {line_number}: {event_id}"
                        )
                    seen.add(event_id)
                    scope_raw = raw["scope"]
                    events.append(
                        TaskCompletedEvent(
                            event_id=event_id,
                            task_id=str(raw["task_id"]),
                            scope=ScopeIds(
                                tenant_id=str(scope_raw["tenant_id"]),
                                agent_id=str(scope_raw["agent_id"]),
                                user_id=str(scope_raw["user_id"]),
                            ),
                            completed_at=str(raw["completed_at"]),
                            interrupted=bool(raw["interrupted"]),
                            tool_iterations=int(raw["tool_iterations"]),
                            transcript=tuple(raw["transcript"]),
                            final_response=str(raw["final_response"]),
                            source_refs=tuple(raw["source_refs"]),
                        )
                    )
            return events
