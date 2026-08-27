"""Activity-message metadata required by the Journal sequence adaptation.

Claude source: message UUID/message-id metadata consumed by compact.ts and
sessionMemoryCompact.ts. Project adaptation: Journal sequences replace UUIDs.
"""

from typing import Any

from short_term_memory.models import SessionCompressionMessage


def annotate_active_message(
    message: SessionCompressionMessage,
    *,
    from_sequence: int,
    through_sequence: int,
    group_id: str,
) -> SessionCompressionMessage:
    if from_sequence < 1:
        raise ValueError("from_sequence must be positive")
    if through_sequence < from_sequence:
        raise ValueError("through_sequence must be >= from_sequence")
    if not group_id:
        raise ValueError("group_id must not be blank")
    return SessionCompressionMessage.model_validate(
        {
            **message.model_dump(mode="json"),
            "stm_sequence_from": from_sequence,
            "stm_sequence_through": through_sequence,
            "stm_group_id": group_id,
        }
    )


def to_provider_messages(
    messages: tuple[SessionCompressionMessage, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            key: value
            for key, value in message.model_dump(mode="json").items()
            if not key.startswith("stm_")
        }
        for message in messages
    )
