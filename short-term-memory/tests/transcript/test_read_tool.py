import pytest
from pydantic import ValidationError

from short_term_memory.transcript.journal_transcript import (
    JOURNAL_TRANSCRIPT_URI,
    TranscriptLine,
)
from short_term_memory.transcript.read_tool import (
    DEFAULT_READ_LIMIT,
    TranscriptOffsetError,
    TranscriptReadRequest,
    TranscriptResultTooLargeError,
    read_transcript,
)


def lines(*items: object) -> tuple[TranscriptLine, ...]:
    return tuple(
        TranscriptLine(sequence=int(items[index]), text=str(items[index + 1]))
        for index in range(0, len(items), 2)
    )


def test_read_uses_sequence_as_one_based_offset() -> None:
    result = read_transcript(
        lines(7, "seven", 8, "eight", 9, "nine"),
        TranscriptReadRequest(
            file_path=JOURNAL_TRANSCRIPT_URI, offset=8, limit=2
        ),
        max_response_chars=10_000,
    )

    assert result.content.splitlines() == ["8\teight", "9\tnine"]
    assert result.sequence_from == 8
    assert result.sequence_through == 9
    assert result.num_lines == 2
    assert result.total_lines == 3


def test_read_omitted_limit_uses_claude_default_of_two_thousand() -> None:
    transcript = tuple(
        TranscriptLine(sequence=sequence, text="line")
        for sequence in range(1, DEFAULT_READ_LIMIT + 2)
    )

    result = read_transcript(
        transcript,
        TranscriptReadRequest(file_path=JOURNAL_TRANSCRIPT_URI),
        max_response_chars=100_000,
    )

    assert DEFAULT_READ_LIMIT == 2_000
    assert result.num_lines == 2_000
    assert result.sequence_through == 2_000


def test_read_request_rejects_limit_above_two_thousand() -> None:
    with pytest.raises(ValidationError):
        TranscriptReadRequest(
            file_path=JOURNAL_TRANSCRIPT_URI, limit=DEFAULT_READ_LIMIT + 1
        )


def test_read_returns_last_partial_page() -> None:
    result = read_transcript(
        lines(10, "ten", 11, "eleven", 12, "twelve"),
        TranscriptReadRequest(
            file_path=JOURNAL_TRANSCRIPT_URI, offset=11, limit=100
        ),
        max_response_chars=1_000,
    )

    assert [line.split("\t", 1)[0] for line in result.content.splitlines()] == [
        "11",
        "12",
    ]
    assert result.sequence_through == 12


def test_read_rejects_offset_after_final_sequence() -> None:
    with pytest.raises(TranscriptOffsetError, match="offset 10 is out of range"):
        read_transcript(
            lines(1, "one", 9, "nine"),
            TranscriptReadRequest(
                file_path=JOURNAL_TRANSCRIPT_URI, offset=10
            ),
            max_response_chars=1_000,
        )


def test_read_reports_missing_transcript_without_a_physical_path() -> None:
    with pytest.raises(TranscriptOffsetError, match="transcript is empty") as exc:
        read_transcript(
            (),
            TranscriptReadRequest(file_path=JOURNAL_TRANSCRIPT_URI),
            max_response_chars=1_000,
        )

    assert "/" not in str(exc.value)


def test_read_returns_numbered_text() -> None:
    result = read_transcript(
        lines(42, '{"role":"user"}'),
        TranscriptReadRequest(file_path=JOURNAL_TRANSCRIPT_URI, offset=42),
        max_response_chars=1_000,
    )

    assert result.content == '42\t{"role":"user"}'


def test_read_size_error_tells_agent_to_grep_or_narrow_the_range() -> None:
    with pytest.raises(TranscriptResultTooLargeError) as exc:
        read_transcript(
            lines(1, "a long transcript line", 2, "another line"),
            TranscriptReadRequest(file_path=JOURNAL_TRANSCRIPT_URI),
            max_response_chars=10,
        )

    message = str(exc.value)
    assert "Grep" in message
    assert "offset/limit" in message


def test_read_request_rejects_non_journal_paths_and_zero_offset() -> None:
    with pytest.raises(ValidationError):
        TranscriptReadRequest(file_path="/etc/passwd")
    with pytest.raises(ValidationError):
        TranscriptReadRequest(file_path=JOURNAL_TRANSCRIPT_URI, offset=0)
