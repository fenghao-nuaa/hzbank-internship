"""Pure Claude Session Memory extraction translated from ``sessionMemory.ts``."""

from datetime import datetime, timezone
import json

from short_term_memory.compression.context_messages import to_provider_messages
from short_term_memory.compression.continuity_model import ContinuityCompactionModel
from short_term_memory.compression.session_memory_prompt import (
    EMPTY_SESSION_MEMORY,
    SESSION_MEMORY_HEADINGS,
    build_session_memory_update_prompt,
)
from short_term_memory.models import SessionCompressionMessage, SessionMemoryRevision


def _template_descriptions() -> dict[str, str]:
    lines = EMPTY_SESSION_MEMORY.splitlines()
    return {
        line[2:]: lines[index + 1]
        for index, line in enumerate(lines)
        if line.startswith("# ")
    }


_DESCRIPTIONS = _template_descriptions()


def validate_session_memory(content: str) -> str:
    """Require Claude's ten headings and immutable description lines exactly."""

    lines = content.splitlines()
    headings = [line[2:] for line in lines if line.startswith("# ")]
    if headings != list(SESSION_MEMORY_HEADINGS):
        raise ValueError("model output must preserve the exact Session Memory template")
    if lines[:1] != [f"# {SESSION_MEMORY_HEADINGS[0]}"]:
        raise ValueError("model output must preserve the exact Session Memory template")
    for heading in SESSION_MEMORY_HEADINGS:
        index = lines.index(f"# {heading}")
        if index + 1 >= len(lines) or lines[index + 1] != _DESCRIPTIONS[heading]:
            raise ValueError("model output must preserve the exact Session Memory template")
    return content


def _rough_token_count(content: str) -> int:
    return int((len(content) / 4) + 0.5)


async def extract_session_memory_revision(
    *,
    current_memory: str,
    messages: tuple[SessionCompressionMessage, ...],
    covered_through_sequence: int,
    previous_version: int,
    continuity_model: ContinuityCompactionModel,
    model_name: str,
    extraction_started_at: datetime,
    now: datetime | None = None,
) -> SessionMemoryRevision:
    """Run one isolated update and create a revision only after validation."""

    if covered_through_sequence < 1:
        raise ValueError("covered_through_sequence must be positive")
    if previous_version < 0:
        raise ValueError("previous_version must not be negative")
    if not model_name:
        raise ValueError("model_name must not be blank")
    for timestamp in (extraction_started_at, now):
        if timestamp is not None and (
            timestamp.tzinfo is None or timestamp.utcoffset() is None
        ):
            raise ValueError("extraction timestamps must be timezone-aware")
    completed_at = now or datetime.now(timezone.utc)
    prompt = build_session_memory_update_prompt(current_memory)
    output = await continuity_model.update_session_memory(
        current_memory=current_memory,
        messages=to_provider_messages(messages),
        prompt=prompt,
        model=model_name,
        query_source="session_memory",
    )
    validated = validate_session_memory(output)
    return SessionMemoryRevision(
        version=previous_version + 1,
        content=validated,
        covered_through_sequence=covered_through_sequence,
        token_count=_rough_token_count(
            json.dumps(to_provider_messages(messages), ensure_ascii=False)
        ),
        extraction_started_at=extraction_started_at.isoformat(),
        updated_at=completed_at.isoformat(),
    )
