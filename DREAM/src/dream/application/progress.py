"""Durable record of source events accepted by Background Review."""

import json
import os
from pathlib import Path
from threading import RLock


class ReviewProgressStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def read_all(self) -> tuple[str, ...]:
        with self._lock:
            if not self.path.exists():
                return ()
            states: dict[str, bool] = {}
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    payload = json.loads(line)
                    states[str(payload["event_id"])] = bool(
                        payload.get("processed", True)
                    )
            return tuple(
                event_id for event_id, processed in states.items() if processed
            )

    def contains(self, event_id: str) -> bool:
        return event_id in self.read_all()

    def append(self, event_id: str) -> None:
        with self._lock:
            if self.contains(event_id):
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(self._encoded(event_id, processed=True))
                handle.flush()
                os.fsync(handle.fileno())

    def invalidate(self, event_id: str) -> None:
        with self._lock:
            if not self.contains(event_id):
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(self._encoded(event_id, processed=False))
                handle.flush()
                os.fsync(handle.fileno())

    @staticmethod
    def _encoded(event_id: str, *, processed: bool) -> str:
        return (
            json.dumps(
                {"event_id": event_id, "processed": processed},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
