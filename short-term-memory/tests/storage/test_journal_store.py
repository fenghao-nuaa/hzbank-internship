import json
import multiprocessing
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.factories import envelope, memory_event
from short_term_memory.models import JournalRole
from short_term_memory.storage.compaction_checkpoint import checkpoint_from_envelope
from short_term_memory.storage.journal_store import (
    JournalConflictError,
    JournalFileEvent,
    JournalMessageEvent,
    JournalStore,
)
from short_term_memory.storage.vfs_adapter import VFSAdapter


NOW = datetime(2026, 7, 23, 6, 30, tzinfo=timezone.utc)


def _append_same_event(root: str, start, results) -> None:
    store = JournalStore(VFSAdapter(Path(root)))
    start.wait()
    result = store.append_event(
        "multi-user", "multi-session", memory_event(event_id="same-process-event")
    )
    results.put(result.appended)


def _write_two_part_line(root: str, ready) -> None:
    store = JournalStore(VFSAdapter(Path(root)))
    event = memory_event(event_id="two-part")
    with store._session_lock("half-user", "half-session"):
        path = (
            store.vfs.paths("half-user").journals
            / "2026-08-06-half-session.jsonl"
        )
        line = json.dumps(
            store._encoded_record(
                JournalMessageEvent(
                    role=event.role,
                    content=event.content,
                    timestamp=event.created_at,
                    event_id=event.event_id,
                    sequence=event.sequence,
                    content_type=event.content_type,
                    metadata=dict(event.metadata),
                    sha256=event.sha256,
                )
            ),
            separators=(",", ":"),
        ) + "\n"
        with path.open("w", encoding="utf-8") as handle:
            split = len(line) // 2
            handle.write(line[:split])
            handle.flush()
            os.fsync(handle.fileno())
            ready.set()
            time.sleep(0.1)
            handle.write(line[split:])
            handle.flush()
            os.fsync(handle.fileno())


def test_message_row_uses_plan_fields_and_session_filename(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    path = store.append_message(
        "user-1",
        "sess-1",
        role="user",
        content="帮我总结这个 PDF",
        timestamp=NOW,
    )

    assert path.name == "2026-07-23-sess-1.jsonl"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "type": "message",
        "role": "user",
        "content": "帮我总结这个 PDF",
        "timestamp": "2026-07-23T06:30:00+00:00",
    }


def test_multimodal_content_keeps_only_input_text(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_message(
        "user-1",
        "sess-1",
        role="user",
        content=[
            {"type": "input_text", "text": "第一段"},
            {"type": "input_image", "image_url": "image.png"},
            {"type": "input_text", "text": "第二段"},
        ],
        timestamp=NOW,
    )

    event = store.read_session("user-1", "sess-1")[0]
    assert isinstance(event, JournalMessageEvent)
    assert event.content == "第一段\n第二段"


def test_file_row_falls_back_to_original_url(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_file(
        "user-1",
        "sess-1",
        original_url="https://example.com/product-plan.pdf",
        local_path=None,
        timestamp=NOW,
    )

    event = store.read_session("user-1", "sess-1")[0]
    assert isinstance(event, JournalFileEvent)
    assert event.local_path == "https://example.com/product-plan.pdf"


def test_session_read_sorts_daily_files_and_does_not_persist_scope_ids(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_message(
        "user-1",
        "sess-1",
        role="user",
        content="第二天",
        timestamp=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    store.append_message(
        "user-1",
        "sess-1",
        role="assistant",
        content="第一天",
        timestamp=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    events = store.read_session("user-1", "sess-1")
    assert [event.content for event in events if isinstance(event, JournalMessageEvent)] == [
        "第一天",
        "第二天",
    ]
    for path in VFSAdapter(tmp_path).paths("user-1").journals.glob("*.jsonl"):
        assert "user_id" not in path.read_text(encoding="utf-8")
        assert "session_id" not in path.read_text(encoding="utf-8")


def test_append_event_is_byte_preserving_and_idempotent(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    event = memory_event(sequence=7, event_id="same", content="a\n中文\n")

    first = store.append_event("u", "s", event)
    second = store.append_event("u", "s", event)

    assert first.appended is True
    assert second.appended is False
    assert store.find_event("u", "s", "same") == event
    assert store.read_original_range("u", "s", 7, 7)[0].content == "a\n中文\n"
    assert json.loads(first.path.read_text(encoding="utf-8")) == {
        "type": "message",
        "role": "user",
        "content": "a\n中文\n",
        "timestamp": event.created_at,
        "event_id": "same",
        "sequence": 7,
        "content_type": "conversation",
        "metadata": {},
        "sha256": event.sha256,
    }


def test_same_event_id_with_different_digest_is_conflict(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event("u", "s", memory_event(event_id="same", content="one"))

    with pytest.raises(JournalConflictError):
        store.append_event("u", "s", memory_event(event_id="same", content="two"))


def test_read_original_range_selects_only_requested_sequences(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    for sequence in (1, 2, 3):
        store.append_event(
            "u",
            "s",
            memory_event(sequence=sequence, event_id=f"event-{sequence}"),
        )

    assert store.read_original_range("u", "s", 2, 2) == (
        memory_event(sequence=2, event_id="event-2"),
    )


def test_read_recent_originals_uses_turns_and_never_starts_with_assistant(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    events = (
        memory_event(sequence=1, event_id="one", content="one"),
        memory_event(sequence=2, event_id="two", content="two").model_copy(
            update={"role": JournalRole.ASSISTANT}
        ),
        memory_event(sequence=3, event_id="three", content="three"),
    )
    for event in events:
        store.append_event("u", "s", event)

    assert store.read_recent_originals("u", "s", 1) == events[2:]


def test_recent_originals_are_sorted_by_sequence_after_out_of_order_appends(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    later = memory_event(sequence=2, event_id="later", content="later")
    first = memory_event(sequence=1, event_id="first", content="first")
    store.append_event("u", "s", later)
    store.append_event("u", "s", first)

    assert store.read_recent_originals("u", "s", 2) == (first, later)


def test_incomplete_final_json_line_is_ignored_as_crash_residue(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    event = memory_event(event_id="durable")
    result = store.append_event("u", "s", event)
    with result.path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"message"')

    assert store.find_event("u", "s", "durable") == event
    assert store.read_original_range("u", "s", 1, 1) == (event,)


def test_session_read_rejects_whitespace_middle_corruption(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    first = store.append_event("u", "s", memory_event(sequence=1, event_id="one"))
    store.append_event("u", "s", memory_event(sequence=2, event_id="two"))
    durable_line, valid_line = first.path.read_text(encoding="utf-8").splitlines()
    first.path.write_text(
        f"{durable_line}\n \t\n{valid_line}\n",
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError):
        store.read_session("u", "s")


def test_sequence_event_preserves_original_offset_timestamp(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    event = memory_event(
        event_id="offset",
        created_at=datetime(2026, 8, 6, 8, tzinfo=timezone(timedelta(hours=8))),
    )

    result = store.append_event("u", "s", event)

    assert json.loads(result.path.read_text(encoding="utf-8"))["timestamp"] == event.created_at
    assert store.find_event("u", "s", "offset") == event
    assert store.read_original_range("u", "s", 1, 1) == (event,)


def test_session_locks_are_released_after_operations(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))

    for index in range(100):
        store.append_event(
            "u",
            f"session-{index}",
            memory_event(event_id=f"event-{index}"),
        )

    assert store.session_lock_count == 0


def test_append_checkpoint_is_idempotent_and_latest_crosses_days(
    tmp_path: Path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    old = checkpoint_from_envelope(
        "u",
        "s",
        envelope(version=2).model_copy(
            update={"updated_at": "2026-08-16T23:00:00+00:00"}
        ),
    )
    new = checkpoint_from_envelope(
        "u",
        "s",
        envelope(version=3).model_copy(
            update={"updated_at": "2026-08-17T01:00:00+00:00"}
        ),
    )

    assert store.append_compaction_checkpoint("u", "s", old).appended
    assert not store.append_compaction_checkpoint("u", "s", old).appended
    assert store.append_compaction_checkpoint("u", "s", new).appended

    assert store.read_latest_compaction_checkpoint("u", "s") == new


def test_latest_original_sequence_ignores_checkpoint(tmp_path: Path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event("u", "s", memory_event(sequence=41, event_id="e41"))
    checkpoint = checkpoint_from_envelope(
        "u", "s", envelope(version=7, through=41)
    )
    store.append_compaction_checkpoint("u", "s", checkpoint)

    assert store.latest_original_sequence("u", "s") == 41


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl journals require POSIX")
def test_independent_processes_append_same_event_idempotently(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_append_same_event, args=(str(tmp_path), start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=1) for _ in processes) == [False, True]
    store = JournalStore(VFSAdapter(tmp_path))
    assert len(store.read_session("multi-user", "multi-session")) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl journals require POSIX")
def test_reader_never_observes_half_line_from_another_process(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    writer = context.Process(target=_write_two_part_line, args=(str(tmp_path), ready))
    writer.start()
    assert ready.wait(timeout=5)

    records = JournalStore(VFSAdapter(tmp_path)).read_session(
        "half-user", "half-session"
    )

    writer.join(timeout=5)
    assert writer.exitcode == 0
    assert len(records) == 1
    assert isinstance(records[0], JournalMessageEvent)
    assert records[0].event_id == "two-part"
