"""Durable cursor-based ingestion for the Internship conversation source."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol

from dream.config import InternshipSourceSettings
from dream.core.events import TaskCompletedEvent
from dream.core.scope import ScopeIds
from dream.application.service import DreamService
from dream.integrations.internship.client import InternshipRecord, SourceFetchError


_SAFE_SOURCE_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SourceClient(Protocol):
    def fetch(self, after: str, limit: int) -> tuple[InternshipRecord, ...]: ...


@dataclass(frozen=True)
class SourceSyncState:
    cursor: str = ""
    last_sync_at: str = ""
    last_status: str = "never"
    last_fetched: int = 0
    last_ingested: int = 0
    last_duplicates: int = 0
    last_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceSyncResult:
    status: str
    fetched: int
    ingested: int
    duplicates: int
    cursor: str
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SourceSyncStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SourceSyncState:
        if not self.path.exists():
            return SourceSyncState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["last_errors"] = tuple(raw.get("last_errors", ()))
        return SourceSyncState(**raw)

    def save(self, state: SourceSyncState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(asdict(state), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def normalize_source_user_id(value: str) -> str:
    if _SAFE_SOURCE_USER_ID.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"external-{digest}"


def record_to_event(
    record: InternshipRecord,
    settings: InternshipSourceSettings,
) -> TaskCompletedEvent:
    source_event_hash = hashlib.sha256(
        record.event_id.encode("utf-8")
    ).hexdigest()[:32]
    return TaskCompletedEvent(
        event_id=f"internship-{source_event_hash}",
        task_id=f"{record.session_id}:{record.round_id}",
        scope=ScopeIds(
            settings.tenant_id,
            settings.agent_id,
            normalize_source_user_id(record.user_id),
        ),
        completed_at=record.completed_at,
        interrupted=False,
        tool_iterations=0,
        transcript=tuple(message.model_dump() for message in record.messages),
        final_response=record.final_response,
        source_refs=(
            {
                "source": "internship",
                "source_event_id": record.event_id,
                "source_user_id": record.user_id,
                "cursor": record.cursor,
            },
        ),
    )


class InternshipSourceSync:
    def __init__(
        self,
        service: DreamService,
        settings: InternshipSourceSettings,
        *,
        client: SourceClient,
        state_store: SourceSyncStateStore | None = None,
    ) -> None:
        self.service = service
        self.settings = settings
        self.client = client
        self.state_store = state_store or SourceSyncStateStore(
            service.home / "source-state" / "internship.json"
        )

    def _save_result(self, result: SourceSyncResult, synced_at: str) -> None:
        self.state_store.save(
            SourceSyncState(
                cursor=result.cursor,
                last_sync_at=synced_at,
                last_status=result.status,
                last_fetched=result.fetched,
                last_ingested=result.ingested,
                last_duplicates=result.duplicates,
                last_errors=result.errors,
            )
        )

    def sync_if_due(self, now: datetime) -> SourceSyncResult:
        state = self.state_store.load()
        if state.last_sync_at:
            last_sync_at = datetime.fromisoformat(state.last_sync_at)
            next_sync_at = last_sync_at + timedelta(
                seconds=self.settings.interval_seconds
            )
            if now < next_sync_at:
                return SourceSyncResult(
                    status="not_due",
                    fetched=0,
                    ingested=0,
                    duplicates=0,
                    cursor=state.cursor,
                )
        return self.sync_once()

    def sync_once(self) -> SourceSyncResult:
        state = self.state_store.load()
        now = datetime.now(timezone.utc).isoformat()
        try:
            records = self.client.fetch(
                after=state.cursor,
                limit=self.settings.batch_size,
            )
        except SourceFetchError as exc:
            result = SourceSyncResult(
                status="error",
                fetched=0,
                ingested=0,
                duplicates=0,
                cursor=state.cursor,
                errors=(str(exc),),
            )
            self._save_result(result, now)
            return result

        ingested = 0
        duplicates = 0
        cursor = state.cursor
        for record in records:
            try:
                event = record_to_event(record, self.settings)
                if self.service.ledger.contains(event.event_id):
                    duplicates += 1
                else:
                    self.service.ingest_conversation(event)
                    ingested += 1
            except ValueError as exc:
                result = SourceSyncResult(
                    status="error",
                    fetched=len(records),
                    ingested=ingested,
                    duplicates=duplicates,
                    cursor=cursor,
                    errors=(str(exc),),
                )
                self._save_result(result, now)
                return result
            cursor = record.cursor
            self._save_result(
                SourceSyncResult(
                    status="success",
                    fetched=len(records),
                    ingested=ingested,
                    duplicates=duplicates,
                    cursor=cursor,
                ),
                now,
            )

        result = SourceSyncResult(
            status="success",
            fetched=len(records),
            ingested=ingested,
            duplicates=duplicates,
            cursor=cursor,
        )
        self._save_result(result, now)
        return result
