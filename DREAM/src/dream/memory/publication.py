"""Per-user publication versions for reviewed dream artifacts."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path

from dream.memory.artifacts import AtomicArtifactStore
from dream.core.scope import ScopePaths


class PublicationStatus(StrEnum):
    PENDING = "pending"
    DREAMING = "dreaming"
    READY_FOR_REVIEW = "ready_for_review"
    READY_FOR_ACTIVATION = "ready_for_activation"
    READY_FOR_WRITEBACK = "ready_for_writeback"
    ACTIVE = "active"
    FAILED = "failed"


@dataclass(frozen=True)
class PublicationVersion:
    version: int
    status: PublicationStatus
    source_event_ids: tuple[str, ...]
    processed_through_event_id: str
    before_snapshot_id: str
    after_snapshot_id: str = ""
    character_definition_sha256: str = ""
    user_persona_sha256: str = ""
    character_definition_written: bool = False
    user_persona_written: bool = False
    created_at: str = ""
    activated_at: str = ""
    failure_reason: str = ""
    fallback_version: int | None = None


class PublicationTransitionError(ValueError):
    """A requested publication state change is not legal."""


_ALLOWED = {
    PublicationStatus.PENDING: {
        PublicationStatus.DREAMING,
        PublicationStatus.FAILED,
    },
    PublicationStatus.DREAMING: {
        PublicationStatus.READY_FOR_REVIEW,
        PublicationStatus.READY_FOR_ACTIVATION,
        PublicationStatus.FAILED,
    },
    PublicationStatus.READY_FOR_REVIEW: {
        PublicationStatus.READY_FOR_WRITEBACK,
        PublicationStatus.FAILED,
    },
    PublicationStatus.READY_FOR_WRITEBACK: {
        PublicationStatus.ACTIVE,
        PublicationStatus.FAILED,
    },
    PublicationStatus.READY_FOR_ACTIVATION: {
        PublicationStatus.ACTIVE,
        PublicationStatus.FAILED,
    },
    PublicationStatus.ACTIVE: set(),
    PublicationStatus.FAILED: set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PublicationStore:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.root = Path("publication") / "users" / paths.user_root.name

    def _version_path(self, version: int) -> Path:
        return self.root / "versions" / f"{version:06d}.json"

    def _write_version(self, value: PublicationVersion) -> PublicationVersion:
        payload = asdict(value)
        payload["status"] = value.status.value
        self.artifacts.write_text(
            self._version_path(value.version),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self.artifacts.write_text(
            self.root / "latest.json",
            json.dumps({"version": value.version}, sort_keys=True) + "\n",
        )
        return value

    def _read_pointer(self, name: str) -> PublicationVersion | None:
        raw = self.artifacts.read_text(self.root / name)
        if not raw:
            return None
        return self.get(int(json.loads(raw)["version"]))

    def get(self, version: int) -> PublicationVersion:
        raw = self.artifacts.read_text(self._version_path(version))
        if not raw:
            raise FileNotFoundError(f"publication version not found: {version}")
        payload = json.loads(raw)
        payload["status"] = PublicationStatus(payload["status"])
        payload["source_event_ids"] = tuple(payload["source_event_ids"])
        return PublicationVersion(**payload)

    def latest(self) -> PublicationVersion | None:
        return self._read_pointer("latest.json")

    def active(self) -> PublicationVersion | None:
        return self._read_pointer("active.json")

    def restore_active(self, version: int) -> PublicationVersion:
        selected = self.get(version)
        if selected.status is not PublicationStatus.ACTIVE:
            raise PublicationTransitionError("rollback target was never active")
        self.artifacts.write_text(
            self.root / "active.json",
            json.dumps({"version": selected.version}, sort_keys=True) + "\n",
        )
        return selected

    def pending_event_ids(self) -> tuple[str, ...]:
        raw = self.artifacts.read_text(self.root / "pending.json")
        if not raw:
            return ()
        return tuple(str(value) for value in json.loads(raw)["event_ids"])

    def _write_pending(self, values: tuple[str, ...]) -> None:
        self.artifacts.write_text(
            self.root / "pending.json",
            json.dumps({"event_ids": list(values)}, sort_keys=True) + "\n",
        )

    def note_completed_event(self, event_id: str) -> None:
        pending = self.pending_event_ids()
        if event_id not in pending:
            self._write_pending(pending + (event_id,))

    def begin(
        self,
        source_event_ids: tuple[str, ...],
        processed_through_event_id: str,
        before_snapshot_id: str,
    ) -> PublicationVersion:
        if not source_event_ids or source_event_ids[-1] != processed_through_event_id:
            raise ValueError("processed event must be the last source event")
        latest = self.latest()
        active = self.active()
        value = PublicationVersion(
            version=1 if latest is None else latest.version + 1,
            status=PublicationStatus.PENDING,
            source_event_ids=source_event_ids,
            processed_through_event_id=processed_through_event_id,
            before_snapshot_id=before_snapshot_id,
            created_at=_now(),
            fallback_version=active.version if active is not None else None,
        )
        return self._write_version(value)

    def _transition(
        self,
        version: int,
        status: PublicationStatus,
        **changes: object,
    ) -> PublicationVersion:
        current = self.get(version)
        if status not in _ALLOWED[current.status]:
            raise PublicationTransitionError(
                f"cannot transition {current.status.value} to {status.value}"
            )
        return self._write_version(replace(current, status=status, **changes))

    def mark_dreaming(self, version: int) -> PublicationVersion:
        return self._transition(version, PublicationStatus.DREAMING)

    def mark_ready_for_review(
        self,
        version: int,
        after_snapshot_id: str,
        character_definition_sha256: str,
        user_persona_sha256: str,
    ) -> PublicationVersion:
        return self._transition(
            version,
            PublicationStatus.READY_FOR_REVIEW,
            after_snapshot_id=after_snapshot_id,
            character_definition_sha256=character_definition_sha256,
            user_persona_sha256=user_persona_sha256,
        )

    def mark_ready_for_activation(
        self,
        version: int,
        after_snapshot_id: str,
        character_definition_sha256: str,
        user_persona_sha256: str,
    ) -> PublicationVersion:
        return self._transition(
            version,
            PublicationStatus.READY_FOR_ACTIVATION,
            after_snapshot_id=after_snapshot_id,
            character_definition_sha256=character_definition_sha256,
            user_persona_sha256=user_persona_sha256,
            character_definition_written=True,
            user_persona_written=True,
        )

    def approve(self, version: int) -> PublicationVersion:
        return self._transition(version, PublicationStatus.READY_FOR_WRITEBACK)

    def confirm_writeback(
        self,
        version: int,
        *,
        character_written: bool,
        user_written: bool,
    ) -> PublicationVersion:
        current = self.get(version)
        if current.status is not PublicationStatus.READY_FOR_WRITEBACK:
            raise PublicationTransitionError("writeback confirmation is not ready")
        return self._write_version(
            replace(
                current,
                character_definition_written=character_written,
                user_persona_written=user_written,
            )
        )

    def activate(self, version: int) -> PublicationVersion:
        self.require_activation_ready(version)
        active = self._transition(
            version,
            PublicationStatus.ACTIVE,
            activated_at=_now(),
        )
        self.artifacts.write_text(
            self.root / "active.json",
            json.dumps({"version": active.version}, sort_keys=True) + "\n",
        )
        consumed = set(active.source_event_ids)
        self._write_pending(
            tuple(value for value in self.pending_event_ids() if value not in consumed)
        )
        return active

    def require_activation_ready(self, version: int) -> PublicationVersion:
        current = self.get(version)
        if current.status not in {
            PublicationStatus.READY_FOR_WRITEBACK,
            PublicationStatus.READY_FOR_ACTIVATION,
        }:
            raise PublicationTransitionError("publication is not ready for activation")
        if not (current.character_definition_written and current.user_persona_written):
            raise PublicationTransitionError(
                "both writebacks must be confirmed before activation"
            )
        return current

    def fail(self, version: int, reason: str) -> PublicationVersion:
        if not reason.strip():
            raise ValueError("failure reason must not be blank")
        return self._transition(
            version,
            PublicationStatus.FAILED,
            failure_reason=reason.strip(),
        )
