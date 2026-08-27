from datetime import datetime, timezone

import pytest

from short_term_memory.compression.session_memory import (
    extract_session_memory_revision,
    validate_session_memory,
)
from short_term_memory.compression.session_memory_prompt import EMPTY_SESSION_MEMORY
from short_term_memory.models import SessionCompressionMessage


def populated_memory() -> str:
    lines: list[str] = []
    for line in EMPTY_SESSION_MEMORY.splitlines():
        lines.append(line)
        if line.startswith("_") and line.endswith("_"):
            lines.append("Preserved concrete session detail.")
    return "\n".join(lines) + "\n"


class RecordingModel:
    def __init__(self, output: str) -> None:
        self.output = output
        self.update_calls: list[dict[str, object]] = []

    async def update_session_memory(self, **kwargs: object) -> str:
        self.update_calls.append(kwargs)
        return self.output


def test_validate_session_memory_requires_exact_ten_section_template() -> None:
    assert validate_session_memory(populated_memory()) == populated_memory()
    with pytest.raises(ValueError, match="exact Session Memory template"):
        validate_session_memory(populated_memory().replace("# Worklog", "# Timeline"))
    with pytest.raises(ValueError, match="exact Session Memory template"):
        validate_session_memory(
            populated_memory().replace(
                "_What is actively being worked on right now?",
                "_What was being worked on?",
            )
        )


@pytest.mark.asyncio
async def test_extract_uses_current_memory_full_context_and_isolated_query_source() -> None:
    model = RecordingModel(populated_memory())
    messages = (
        SessionCompressionMessage(role="user", content="first question"),
        SessionCompressionMessage(role="assistant", content="first answer"),
        SessionCompressionMessage(role="user", content="latest question"),
        SessionCompressionMessage(role="assistant", content="latest answer"),
    )
    now = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
    revision = await extract_session_memory_revision(
        current_memory=EMPTY_SESSION_MEMORY,
        messages=messages,
        covered_through_sequence=12,
        previous_version=3,
        continuity_model=model,
        model_name="claude-sonnet",
        extraction_started_at=now,
        now=now,
    )

    assert revision.version == 4
    assert revision.content == populated_memory()
    assert revision.covered_through_sequence == 12
    assert revision.extraction_started_at == now.isoformat()
    assert model.update_calls[0]["current_memory"] == EMPTY_SESSION_MEMORY
    assert model.update_calls[0]["messages"] == tuple(
        message.model_dump(mode="json") for message in messages
    )
    assert model.update_calls[0]["model"] == "claude-sonnet"
    assert model.update_calls[0]["query_source"] == "session_memory"
    assert EMPTY_SESSION_MEMORY in str(model.update_calls[0]["prompt"])


@pytest.mark.asyncio
async def test_invalid_model_output_never_creates_a_revision() -> None:
    model = RecordingModel("# Session Title\nmissing the other sections")
    with pytest.raises(ValueError, match="exact Session Memory template"):
        await extract_session_memory_revision(
            current_memory=EMPTY_SESSION_MEMORY,
            messages=(SessionCompressionMessage(role="user", content="question"),),
            covered_through_sequence=1,
            previous_version=0,
            continuity_model=model,
            model_name="claude-sonnet",
            extraction_started_at=datetime.now(timezone.utc),
        )
