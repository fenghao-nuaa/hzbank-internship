"""Deterministic turn-boundary selection for original memory events."""

from short_term_memory.models import JournalRole, MemoryEvent


def select_recent_turns(
    events: tuple[MemoryEvent, ...], history_turns: int
) -> tuple[MemoryEvent, ...]:
    """Sort/deduplicate originals and retain complete recent user turns.

    System originals before the first user message form a durable prefix.
    Assistant and tool originals belong to the preceding user turn. Consecutive
    user originals (conversation, code, document, or skill) remain in one turn
    until an assistant or tool response closes it.
    """

    if history_turns < 1:
        raise ValueError("history_turns must be positive")
    by_sequence: dict[int, MemoryEvent] = {}
    event_sequences: dict[str, int] = {}
    for event in events:
        prior = by_sequence.get(event.sequence)
        if prior is not None and prior != event:
            raise ValueError(f"conflicting originals for sequence {event.sequence}")
        prior_sequence = event_sequences.get(event.event_id)
        if prior_sequence is not None and prior_sequence != event.sequence:
            raise ValueError(f"event_id {event.event_id!r} has multiple sequences")
        by_sequence[event.sequence] = event
        event_sequences[event.event_id] = event.sequence
    ordered = tuple(by_sequence[sequence] for sequence in sorted(by_sequence))
    prefix: list[MemoryEvent] = []
    turns: list[list[MemoryEvent]] = []
    current: list[MemoryEvent] | None = None
    response_seen = False
    for event in ordered:
        if event.role is JournalRole.USER and (current is None or response_seen):
            current = [event]
            turns.append(current)
            response_seen = False
        elif event.role is JournalRole.USER:
            current.append(event)
        elif current is not None:
            current.append(event)
            if event.role in {JournalRole.ASSISTANT, JournalRole.TOOL}:
                response_seen = True
        elif event.role is JournalRole.SYSTEM:
            prefix.append(event)
    selected = turns[-history_turns:]
    return (*prefix, *(event for turn in selected for event in turn))
