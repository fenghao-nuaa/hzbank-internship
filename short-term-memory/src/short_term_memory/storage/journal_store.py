"""Append-only, recoverable per-session original-event journals."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from typing import Iterator, Literal

from pydantic import BaseModel, Field

from short_term_memory.models import MemoryContentType, MemoryEvent
from short_term_memory.storage.compaction_checkpoint import CompactionCheckpoint
from short_term_memory.storage.recent_originals import select_recent_turns
from short_term_memory.storage.vfs_adapter import VFSAdapter, safe_component

try:
    import fcntl
except ImportError:  # pragma: no cover - supported production is Linux/POSIX.
    fcntl = None


JournalRole = Literal["user", "assistant", "system", "tool", "unknown"]


class JournalEvent(BaseModel):
    type: Literal["message", "file"]
    timestamp: str


class JournalMessageEvent(JournalEvent):
    type: Literal["message"] = "message"
    role: JournalRole
    content: str
    event_id: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    content_type: MemoryContentType = MemoryContentType.CONVERSATION
    metadata: dict[str, str] = Field(default_factory=dict)
    sha256: str | None = None


class JournalFileEvent(JournalEvent):
    type: Literal["file"] = "file"
    original_url: str
    local_path: str


JournalRecord = JournalMessageEvent | JournalFileEvent | CompactionCheckpoint


class JournalConflictError(ValueError):
    """An event ID already exists with a different original-content digest."""


@dataclass(frozen=True)
class JournalAppendResult:
    appended: bool
    path: Path


@dataclass
class _SessionLockEntry:
    lock: RLock
    users: int = 0


def _utc_timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp must be an ISO-8601 datetime") from error
    return _utc_timestamp(timestamp)


def _text_content(value: str | list[dict[str, object]]) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(
        str(part["text"])
        for part in value
        if part.get("type") == "input_text" and isinstance(part.get("text"), str)
    )


class JournalStore:
    def __init__(self, vfs: VFSAdapter) -> None:
        if fcntl is None:
            raise RuntimeError(
                "JournalStore requires POSIX fcntl.flock; Windows is unsupported"
            )
        self.vfs = vfs
        self._locks_guard = RLock()
        self._session_locks: dict[tuple[str, str], _SessionLockEntry] = {}

    @property
    def session_lock_count(self) -> int:
        with self._locks_guard:
            return len(self._session_locks)

    def append_message(
        self,
        user_id: str,
        session_id: str,
        *,
        role: JournalRole,
        content: str | list[dict[str, object]],
        timestamp: datetime | None = None,
    ) -> Path:
        at = _utc_timestamp(timestamp)
        event = JournalMessageEvent(
            role=role,
            content=_text_content(content),
            timestamp=at.isoformat(),
        )
        return self._append(user_id, session_id, at, event)

    def append_file(
        self,
        user_id: str,
        session_id: str,
        *,
        original_url: str,
        local_path: str | None,
        timestamp: datetime | None = None,
    ) -> Path:
        at = _utc_timestamp(timestamp)
        event = JournalFileEvent(
            original_url=original_url,
            local_path=local_path or original_url,
            timestamp=at.isoformat(),
        )
        return self._append(user_id, session_id, at, event)

    def append_event(
        self, user_id: str, session_id: str, event: MemoryEvent
    ) -> JournalAppendResult:
        at = _parse_timestamp(event.created_at)
        record = JournalMessageEvent(
            role=event.role,
            content=event.content,
            timestamp=event.created_at,
            event_id=event.event_id,
            sequence=event.sequence,
            content_type=event.content_type,
            metadata=dict(event.metadata),
            sha256=event.sha256,
        )
        with self._session_lock(user_id, session_id):
            existing = self._find_event_entry_unlocked(user_id, session_id, event.event_id)
            if existing is not None:
                existing_event, existing_path = existing
                if existing_event.sha256 != event.sha256:
                    raise JournalConflictError(
                        f"event_id {event.event_id!r} already has a different digest"
                    )
                return JournalAppendResult(appended=False, path=existing_path)
            path = self._append_unlocked(user_id, session_id, at, record)
            return JournalAppendResult(appended=True, path=path)

    def find_event(
        self, user_id: str, session_id: str, event_id: str
    ) -> MemoryEvent | None:
        with self._session_lock(user_id, session_id):
            entry = self._find_event_entry_unlocked(user_id, session_id, event_id)
            return entry[0] if entry is not None else None

    def append_compaction_checkpoint(
        self,
        user_id: str,
        session_id: str,
        checkpoint: CompactionCheckpoint,
    ) -> JournalAppendResult:
        """Idempotently append one immutable L3/L4 recovery checkpoint."""

        if checkpoint.user_id != user_id or checkpoint.session_id != session_id:
            raise ValueError("checkpoint scope does not match Journal session")
        at = _parse_timestamp(checkpoint.created_at)
        with self._session_lock(user_id, session_id):
            for record, path in self._read_session_entries_unlocked(
                user_id, session_id
            ):
                if (
                    isinstance(record, CompactionCheckpoint)
                    and record.checkpoint_id == checkpoint.checkpoint_id
                ):
                    return JournalAppendResult(appended=False, path=path)
            path = self._append_unlocked(user_id, session_id, at, checkpoint)
            return JournalAppendResult(appended=True, path=path)

    def read_latest_compaction_checkpoint(
        self, user_id: str, session_id: str
    ) -> CompactionCheckpoint | None:
        """Return the strongest immutable checkpoint across all session days."""

        with self._session_lock(user_id, session_id):
            checkpoints = tuple(
                record
                for record, _ in self._read_session_entries_unlocked(
                    user_id, session_id
                )
                if isinstance(record, CompactionCheckpoint)
            )
        return max(
            checkpoints,
            key=lambda item: (item.envelope_version, item.created_at),
            default=None,
        )

    def latest_original_sequence(self, user_id: str, session_id: str) -> int:
        """Return the maximum durable original sequence, ignoring checkpoints."""

        with self._session_lock(user_id, session_id):
            return max(
                (
                    record.sequence
                    for record, _ in self._read_session_entries_unlocked(
                        user_id, session_id
                    )
                    if isinstance(record, JournalMessageEvent)
                    and record.sequence is not None
                ),
                default=0,
            )

    def read_original_range(
        self,
        user_id: str,
        session_id: str,
        from_sequence: int,
        through_sequence: int,
    ) -> tuple[MemoryEvent, ...]:
        with self._session_lock(user_id, session_id):
            events = (
                self._memory_event(record)
                for record, _ in self._read_session_entries_unlocked(user_id, session_id)
                if isinstance(record, JournalMessageEvent) and record.sequence is not None
            )
            return tuple(
                event
                for event in events
                if from_sequence <= event.sequence <= through_sequence
            )

    def read_recent_originals(
        self, user_id: str, session_id: str, history_turns: int
    ) -> tuple[MemoryEvent, ...]:
        """Read recent complete user turns from reverse-buffered journal files."""

        if history_turns < 1:
            raise ValueError("history_turns must be positive")
        with self._session_lock(user_id, session_id):
            originals: list[MemoryEvent] = []
            session = safe_component(session_id, "session_id")
            directory = self.vfs.paths(user_id).journals
            for path in sorted(directory.glob(f"*-{session}.jsonl"), reverse=True):
                first_line = True
                for line in self._reverse_lines(path):
                    if not line.strip():
                        raise json.JSONDecodeError("blank journal line", line, 0)
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        if first_line and not line.endswith("\n"):
                            first_line = False
                            continue
                        raise
                    first_line = False
                    if raw.get("type") == "message":
                        record = JournalMessageEvent.model_validate(raw)
                        if record.sequence is not None:
                            originals.append(self._memory_event(record))
                    elif raw.get("type") not in {"file", "compaction_checkpoint"}:
                        raise ValueError(f"unknown journal event type in {path.name}")
            return select_recent_turns(originals, history_turns)

    @staticmethod
    def _reverse_lines(path: Path, block_size: int = 8_192):
        """Yield UTF-8 journal lines backwards using a fixed-size read buffer."""

        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            offset = handle.tell()
            carry = b""
            while offset:
                size = min(block_size, offset)
                offset -= size
                handle.seek(offset)
                parts = (handle.read(size) + carry).splitlines(keepends=True)
                if offset:
                    carry = parts.pop(0) if parts else b""
                else:
                    carry = b""
                for line in reversed(parts):
                    yield line.decode("utf-8")
            if carry:
                yield carry.decode("utf-8")

    def read_session(self, user_id: str, session_id: str) -> tuple[JournalRecord, ...]:
        with self._session_lock(user_id, session_id):
            return tuple(
                record
                for record, _ in self._read_session_entries_unlocked(user_id, session_id)
            )

    def list_for_day(self, user_id: str, day: str) -> tuple[Path, ...]:
        safe_component(day, "journal day")
        return tuple(sorted(self.vfs.paths(user_id).journals.glob(f"{day}-*.jsonl")))

    def _append(
        self,
        user_id: str,
        session_id: str,
        timestamp: datetime,
        event: JournalRecord,
    ) -> Path:
        with self._session_lock(user_id, session_id):
            return self._append_unlocked(user_id, session_id, timestamp, event)

    def _append_unlocked(
        self,
        user_id: str,
        session_id: str,
        timestamp: datetime,
        event: JournalRecord,
    ) -> Path:
        session = safe_component(session_id, "session_id")
        path = (
            self.vfs.paths(user_id).journals
            / f"{timestamp.date().isoformat()}-{session}.jsonl"
        )
        encoded = json.dumps(
            self._encoded_record(event),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _find_event_entry_unlocked(
        self, user_id: str, session_id: str, event_id: str
    ) -> tuple[MemoryEvent, Path] | None:
        for record, path in self._read_session_entries_unlocked(user_id, session_id):
            if (
                isinstance(record, JournalMessageEvent)
                and record.event_id == event_id
            ):
                return self._memory_event(record), path
        return None

    @staticmethod
    def _encoded_record(event: JournalRecord) -> dict[str, object]:
        encoded = event.model_dump(mode="json", exclude_none=True)
        if isinstance(event, JournalMessageEvent) and event.event_id is None:
            for field in ("content_type", "metadata"):
                encoded.pop(field, None)
        return encoded

    def _read_session_entries_unlocked(
        self, user_id: str, session_id: str
    ) -> tuple[tuple[JournalRecord, Path], ...]:
        session = safe_component(session_id, "session_id")
        directory = self.vfs.paths(user_id).journals
        records: list[tuple[JournalRecord, Path]] = []
        for path in sorted(directory.glob(f"*-{session}.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
            for index, line in enumerate(lines):
                if not line.strip():
                    raise json.JSONDecodeError("blank journal line", line, 0)
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    if index == len(lines) - 1 and not line.endswith("\n"):
                        continue
                    raise
                if raw.get("type") == "message":
                    records.append((JournalMessageEvent.model_validate(raw), path))
                elif raw.get("type") == "file":
                    records.append((JournalFileEvent.model_validate(raw), path))
                elif raw.get("type") == "compaction_checkpoint":
                    records.append((CompactionCheckpoint.model_validate(raw), path))
                else:
                    raise ValueError(f"unknown journal event type in {path.name}")
        return tuple(records)

    @contextmanager
    def _session_lock(
        self, user_id: str, session_id: str
    ) -> Iterator[None]:
        key = (
            safe_component(user_id, "user_id"),
            safe_component(session_id, "session_id"),
        )
        with self._locks_guard:
            entry = self._session_locks.get(key)
            if entry is None:
                entry = _SessionLockEntry(RLock())
                self._session_locks[key] = entry
            entry.users += 1
        try:
            with entry.lock:
                with self._process_session_lock(*key):
                    yield
        finally:
            with self._locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._session_locks.get(key) is entry:
                    self._session_locks.pop(key, None)

    @contextmanager
    def _process_session_lock(
        self, user_id: str, session_id: str
    ) -> Iterator[None]:
        digest = hashlib.sha256(
            f"{user_id}\0{session_id}".encode("utf-8")
        ).hexdigest()
        lock_directory = self.vfs.paths(user_id).journals / ".locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        lock_path = lock_directory / f"{digest}.lock"
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _memory_event(record: JournalMessageEvent) -> MemoryEvent:
        if (
            record.event_id is None
            or record.sequence is None
            or record.sha256 is None
        ):
            raise ValueError("journal message is not a sequence-bearing original event")
        return MemoryEvent(
            sequence=record.sequence,
            event_id=record.event_id,
            role=record.role,
            content_type=record.content_type,
            content=record.content,
            metadata=record.metadata,
            sha256=record.sha256,
            created_at=record.timestamp,
        )
