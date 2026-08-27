"""Claude Read semantics over one service-owned Journal transcript.

Claude source: ``tools/FileReadTool/FileReadTool.ts``. The filesystem range
read is replaced by a sequence range over the virtual Journal transcript.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from short_term_memory.transcript.journal_transcript import TranscriptLine

DEFAULT_READ_LIMIT = 2_000


class TranscriptOffsetError(ValueError):
    """The requested sequence offset has no readable transcript line."""


class TranscriptResultTooLargeError(ValueError):
    """A transcript range exceeds the service's response budget."""


class TranscriptReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_path: Literal["journal://current-session"]
    offset: int = Field(default=1, ge=1)
    limit: int | None = Field(default=None, ge=1, le=DEFAULT_READ_LIMIT)


class TranscriptReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    sequence_from: int = Field(ge=1)
    sequence_through: int = Field(ge=1)
    num_lines: int = Field(ge=1)
    total_lines: int = Field(ge=1)


def read_transcript(
    transcript_lines: tuple[TranscriptLine, ...],
    request: TranscriptReadRequest,
    *,
    max_response_chars: int,
) -> TranscriptReadResult:
    """Read complete numbered Journal lines starting at a sequence offset."""

    if max_response_chars < 0:
        raise ValueError("max_response_chars must be non-negative")
    if not transcript_lines:
        raise TranscriptOffsetError("transcript is empty")

    selected = tuple(
        line for line in transcript_lines if line.sequence >= request.offset
    )[: request.limit or DEFAULT_READ_LIMIT]
    if not selected:
        raise TranscriptOffsetError(
            f"offset {request.offset} is out of range for transcript"
        )

    content = "\n".join(
        f"{line.sequence}\t{line.text}" for line in selected
    )
    if len(content) > max_response_chars:
        raise TranscriptResultTooLargeError(
            "result too large; use Grep or reduce the offset/limit range"
        )

    return TranscriptReadResult(
        content=content,
        sequence_from=selected[0].sequence,
        sequence_through=selected[-1].sequence,
        num_lines=len(selected),
        total_lines=len(transcript_lines),
    )
