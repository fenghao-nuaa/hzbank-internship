from datetime import datetime, timezone
from pathlib import Path

from dream.application.service import DreamService
from dream.integrations.internship.sync import (
    InternshipSourceSync,
    SourceSyncState,
    SourceSyncStateStore,
    normalize_source_user_id,
    record_to_event,
)
from dream.integrations.internship.client import InternshipRecord, SourceFetchError
from dream.core.scope import ScopeIds
from tests.integrations.source_helpers import source_record, source_settings


class FakeSourceClient:
    def __init__(self, records: list[InternshipRecord]) -> None:
        self.records = tuple(records)

    def fetch(self, after: str, limit: int) -> tuple[InternshipRecord, ...]:
        return self.records[:limit]


class FailingSourceClient:
    def fetch(self, after: str, limit: int) -> tuple[InternshipRecord, ...]:
        raise SourceFetchError("Internship source request failed")


class UnexpectedSourceClient:
    def fetch(self, after: str, limit: int) -> tuple[InternshipRecord, ...]:
        raise AssertionError("source must not be called before the interval is due")


def test_source_user_id_mapping_is_stable_and_path_safe() -> None:
    assert normalize_source_user_id("alice") == "alice"
    mapped = normalize_source_user_id("tenant:user@example.com")
    assert mapped.startswith("external-")
    assert mapped == normalize_source_user_id("tenant:user@example.com")
    assert "/" not in mapped and ":" not in mapped


def test_record_maps_to_configured_scope() -> None:
    event = record_to_event(source_record(user_id="alice"), source_settings())
    assert event.scope == ScopeIds("acme", "assistant", "alice")
    assert event.event_id.startswith("internship-")
    assert event.task_id == "session-1:round-1"
    assert event.final_response == "Understood"
    assert event.transcript[0]["role"] == "user"


def test_sync_persists_cursor_after_each_durable_event(tmp_path: Path) -> None:
    service = DreamService(tmp_path)
    sync = InternshipSourceSync(
        service,
        source_settings(),
        client=FakeSourceClient(
            [
                source_record(cursor="101"),
                source_record(cursor="102", event_id="evt-102"),
            ]
        ),
    )
    result = sync.sync_once()
    assert result.status == "success"
    assert result.ingested == 2
    assert result.cursor == "102"
    state = SourceSyncStateStore(
        tmp_path / "source-state" / "internship.json"
    ).load()
    assert state.cursor == "102"
    assert len(service.ledger.read_all()) == 2


def test_sync_advances_over_duplicate_without_reingesting(tmp_path: Path) -> None:
    service = DreamService(tmp_path)
    first = InternshipSourceSync(
        service,
        source_settings(),
        client=FakeSourceClient([source_record(cursor="101")]),
    )
    assert first.sync_once().ingested == 1
    SourceSyncStateStore(
        tmp_path / "source-state" / "internship.json"
    ).save(SourceSyncState(cursor="", last_sync_at="", last_status="success"))
    second = InternshipSourceSync(
        service,
        source_settings(),
        client=FakeSourceClient([source_record(cursor="101")]),
    )
    result = second.sync_once()
    assert result.duplicates == 1
    assert result.cursor == "101"
    assert len(service.ledger.read_all()) == 1


def test_sync_failure_does_not_advance_cursor(tmp_path: Path) -> None:
    service = DreamService(tmp_path)
    sync = InternshipSourceSync(
        service, source_settings(), client=FailingSourceClient()
    )
    result = sync.sync_once()
    assert result.status == "error"
    assert result.cursor == ""
    assert result.errors == ("Internship source request failed",)


def test_sync_if_due_skips_source_before_configured_interval(tmp_path: Path) -> None:
    service = DreamService(tmp_path)
    state_store = SourceSyncStateStore(
        tmp_path / "source-state" / "internship.json"
    )
    state_store.save(
        SourceSyncState(
            cursor="101",
            last_sync_at="2026-07-15T10:00:00+00:00",
            last_status="success",
        )
    )
    sync = InternshipSourceSync(
        service,
        source_settings(interval_seconds=300),
        client=UnexpectedSourceClient(),
        state_store=state_store,
    )

    result = sync.sync_if_due(datetime(2026, 7, 15, 10, 4, 59, tzinfo=timezone.utc))

    assert result.status == "not_due"
    assert result.cursor == "101"
