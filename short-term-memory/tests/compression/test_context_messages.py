from short_term_memory.compression.context_messages import (
    annotate_active_message,
    to_provider_messages,
)
from short_term_memory.models import SessionCompressionMessage


def test_annotation_is_defensive_and_provider_projection_strips_only_internal_keys() -> None:
    original = SessionCompressionMessage(
        role="assistant",
        content="opaque",
        tool_calls=[{"id": "call-1"}],
    )

    annotated = annotate_active_message(
        original, from_sequence=2, through_sequence=4, group_id="generation:1"
    )
    projected = to_provider_messages((annotated,))

    assert original.model_extra == {"tool_calls": ({"id": "call-1"},)}
    assert annotated.model_extra["stm_sequence_from"] == 2
    assert projected == (
        {"role": "assistant", "content": "opaque", "tool_calls": [{"id": "call-1"}]},
    )


def test_annotation_rejects_invalid_sequence_range_and_blank_group() -> None:
    message = SessionCompressionMessage(role="user", content="x")

    for values in ((3, 2, "g"), (0, 2, "g"), (1, 2, "")):
        try:
            annotate_active_message(
                message,
                from_sequence=values[0],
                through_sequence=values[1],
                group_id=values[2],
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid annotation {values}")
