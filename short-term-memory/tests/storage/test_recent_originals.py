from short_term_memory.models import JournalRole, MemoryContentType
from short_term_memory.storage.recent_originals import select_recent_turns
from tests.factories import memory_event


def test_recent_turns_keep_system_prefix_and_complete_last_user_turn() -> None:
    events = (
        memory_event(sequence=1, event_id="system").model_copy(
            update={"role": JournalRole.SYSTEM}
        ),
        memory_event(sequence=2, event_id="orphan").model_copy(
            update={"role": JournalRole.ASSISTANT}
        ),
        memory_event(sequence=3, event_id="user-1"),
        memory_event(sequence=4, event_id="code").model_copy(
            update={"content_type": MemoryContentType.CODE}
        ),
        memory_event(sequence=5, event_id="tool").model_copy(
            update={"role": JournalRole.TOOL}
        ),
        memory_event(sequence=6, event_id="assistant-1").model_copy(
            update={"role": JournalRole.ASSISTANT}
        ),
        memory_event(sequence=7, event_id="user-2"),
        memory_event(sequence=8, event_id="document").model_copy(
            update={"content_type": MemoryContentType.DOCUMENT}
        ),
        memory_event(sequence=9, event_id="assistant-2").model_copy(
            update={"role": JournalRole.ASSISTANT}
        ),
    )

    selected = select_recent_turns(events, 1)

    assert [event.event_id for event in selected] == [
        "system", "user-2", "document", "assistant-2"
    ]


def test_recent_turns_reject_conflicting_duplicate_sequences() -> None:
    first = memory_event(sequence=1, event_id="first", content="first")
    second = memory_event(sequence=1, event_id="second", content="second")

    try:
        select_recent_turns((first, second), 1)
    except ValueError as error:
        assert "sequence 1" in str(error)
    else:
        raise AssertionError("conflicting sequence must be rejected")


def test_each_user_content_type_can_start_a_turn_and_contiguous_attachments_stay_together() -> None:
    standalone = tuple(
        memory_event(sequence=index, event_id=kind).model_copy(
            update={"content_type": MemoryContentType(kind)}
        )
        for index, kind in enumerate(
            ("conversation", "code", "document", "skill"), start=1
        )
    )
    attachments = (
        memory_event(sequence=10, event_id="user"),
        memory_event(sequence=11, event_id="code").model_copy(
            update={"content_type": MemoryContentType.CODE}
        ),
        memory_event(sequence=12, event_id="document").model_copy(
            update={"content_type": MemoryContentType.DOCUMENT}
        ),
        memory_event(sequence=13, event_id="skill").model_copy(
            update={"content_type": MemoryContentType.SKILL}
        ),
        memory_event(sequence=14, event_id="assistant").model_copy(
            update={"role": JournalRole.ASSISTANT}
        ),
    )

    for event in standalone:
        assert select_recent_turns((event,), 1) == (event,)
    assert select_recent_turns(attachments, 1) == attachments
