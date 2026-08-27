from datetime import datetime, timezone

from short_term_memory.storage.compaction_checkpoint import checkpoint_from_envelope
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter
from short_term_memory.transcript.journal_transcript import (
    JOURNAL_TRANSCRIPT_URI,
    JournalTranscript,
)
from tests.factories import envelope, memory_event


def test_virtual_transcript_sorts_sequences_and_escapes_newlines_across_days(
    tmp_path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event(
        "u",
        "s",
        memory_event(
            sequence=2,
            event_id="second",
            content="second\nline",
            created_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        ),
    )
    store.append_event(
        "u",
        "s",
        memory_event(
            sequence=1,
            event_id="first",
            content="first",
            created_at=datetime(2026, 8, 13, 23, 59, tzinfo=timezone.utc),
        ),
    )

    transcript = JournalTranscript(store)

    assert JOURNAL_TRANSCRIPT_URI == "journal://current-session"
    assert transcript.render("u", "s").splitlines() == [
        '1\t{"sequence":1,"role":"user","content":"first"}',
        '2\t{"sequence":2,"role":"user","content":"second\\nline"}',
    ]
    assert [line.sequence for line in transcript.lines("u", "s")] == [1, 2]


def test_virtual_transcript_excludes_unsequenced_messages_and_file_events(
    tmp_path,
) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_message(
        "u", "s", role="user", content="legacy unsequenced"
    )
    store.append_file(
        "u", "s", original_url="https://example.test/a.pdf", local_path=None
    )
    store.append_event("u", "s", memory_event(sequence=7, event_id="seven"))

    rendered = JournalTranscript(store).render("u", "s")

    assert rendered.startswith('7\t{"sequence":7')
    assert "legacy unsequenced" not in rendered
    assert "example.test" not in rendered


def test_virtual_transcript_empty_session_is_empty(tmp_path) -> None:
    transcript = JournalTranscript(JournalStore(VFSAdapter(tmp_path)))

    assert transcript.lines("u", "missing") == ()
    assert transcript.render("u", "missing") == ""


def test_virtual_transcript_excludes_compaction_checkpoints(tmp_path) -> None:
    store = JournalStore(VFSAdapter(tmp_path))
    store.append_event("u", "s", memory_event(sequence=1, event_id="one"))
    store.append_compaction_checkpoint(
        "u", "s", checkpoint_from_envelope("u", "s", envelope(through=1))
    )

    transcript = JournalTranscript(store)

    assert [line.sequence for line in transcript.lines("u", "s")] == [1]
    assert "compaction_checkpoint" not in transcript.render("u", "s")
