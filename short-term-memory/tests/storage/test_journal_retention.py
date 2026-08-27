from datetime import UTC, datetime
import json
from pathlib import Path

from tests.factories import memory_event
from short_term_memory.storage.journal_retention import JournalRetentionJob
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter


def test_retention_removes_only_files_older_than_thirty_days(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event(
        "u",
        "old",
        memory_event(
            event_id="old",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )
    store.append_event(
        "u",
        "fresh",
        memory_event(
            event_id="fresh",
            created_at=datetime(2026, 7, 20, tzinfo=UTC),
        ),
    )

    result = JournalRetentionJob(store.vfs, retention_days=30).run(
        datetime(2026, 8, 6, tzinfo=UTC)
    )

    assert [path.name for path in result.removed] == ["2026-07-01-old.jsonl"]
    assert store.read_session("u", "fresh")


def test_retention_uses_latest_valid_event_and_skips_crash_residue(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    first = store.append_event(
        "u",
        "s",
        memory_event(
            sequence=1,
            event_id="old",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )
    fresh = memory_event(
        sequence=2,
        event_id="fresh",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    with first.path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "message",
                    "role": "user",
                    "content": fresh.content,
                    "timestamp": fresh.created_at,
                    "event_id": fresh.event_id,
                    "sequence": fresh.sequence,
                    "content_type": fresh.content_type.value,
                    "metadata": dict(fresh.metadata),
                    "sha256": fresh.sha256,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        handle.write('{"type":"message"')

    result = JournalRetentionJob(store.vfs, retention_days=30).run(
        datetime(2026, 8, 6, tzinfo=UTC)
    )

    assert result.removed == ()
    assert first.path.exists()


def test_retention_skips_malformed_filenames_without_broad_deletion(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    journals = store.vfs.paths("u").journals
    malformed = journals / "not-a-date-s.jsonl"
    malformed.write_text("{}\n", encoding="utf-8")
    empty_session = journals / "2026-07-01-.jsonl"
    empty_session.write_text(
        '{"timestamp":"2026-07-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )

    result = JournalRetentionJob(store.vfs, retention_days=30).run(
        datetime(2026, 8, 6, tzinfo=UTC)
    )

    assert result.removed == ()
    assert malformed.exists()
    assert empty_session.exists()
    assert empty_session not in [failure.path for failure in result.failures]


def test_retention_isolates_invalid_utf8_file_and_keeps_cleaning(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    old = store.append_event(
        "u",
        "old",
        memory_event(event_id="old", created_at=datetime(2026, 7, 1, tzinfo=UTC)),
    )
    corrupt = store.vfs.paths("u").journals / "2026-07-01-corrupt.jsonl"
    corrupt.write_bytes(b"\xff")

    result = JournalRetentionJob(store.vfs, retention_days=30).run(
        datetime(2026, 8, 6, tzinfo=UTC)
    )

    assert result.removed == (old.path,)
    assert corrupt.exists()
    assert [failure.path for failure in result.failures] == [corrupt]
