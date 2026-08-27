import pytest
from pydantic import ValidationError

from short_term_memory.transcript.grep_tool import (
    TranscriptGrepRequest,
    TranscriptPatternError,
    grep_transcript,
)
from short_term_memory.transcript.journal_transcript import (
    JOURNAL_TRANSCRIPT_URI,
    TranscriptLine,
)


def lines(*items: object) -> tuple[TranscriptLine, ...]:
    return tuple(
        TranscriptLine(sequence=int(items[index]), text=str(items[index + 1]))
        for index in range(0, len(items), 2)
    )


def request(pattern: str, **overrides: object) -> TranscriptGrepRequest:
    return TranscriptGrepRequest(
        path=JOURNAL_TRANSCRIPT_URI,
        pattern=pattern,
        output_mode="content",
        **overrides,
    )


def test_grep_rejects_invalid_regex() -> None:
    with pytest.raises(TranscriptPatternError):
        grep_transcript(lines(1, "text"), request("["), max_response_chars=1_000)


def test_grep_is_case_insensitive_by_default() -> None:
    result = grep_transcript(
        lines(1, "Redis TTL"), request("redis ttl"), max_response_chars=1_000
    )

    assert [match.sequence for match in result.matches] == [1]


def test_grep_can_be_case_sensitive() -> None:
    result = grep_transcript(
        lines(1, "Redis TTL"),
        request("redis ttl", case_insensitive=False),
        max_response_chars=1_000,
    )

    assert result.matches == ()


def test_content_mode_expands_before_and_after_context() -> None:
    result = grep_transcript(
        lines(1, "before", 2, "target", 3, "after", 4, "outside"),
        request("target", context_before=1, context_after=1),
        max_response_chars=1_000,
    )

    assert [match.sequence for match in result.matches] == [1, 2, 3]
    assert [match.is_match for match in result.matches] == [False, True, False]
    assert result.content == "1\tbefore\n2\ttarget\n3\tafter"


def test_context_overrides_before_and_after() -> None:
    result = grep_transcript(
        lines(1, "far before", 2, "before", 3, "target", 4, "after"),
        request("target", context_before=2, context_after=0, context=1),
        max_response_chars=1_000,
    )

    assert [match.sequence for match in result.matches] == [2, 3, 4]


def test_count_mode_returns_matching_line_count_for_the_virtual_file() -> None:
    result = grep_transcript(
        lines(1, "TTL TTL", 2, "none", 3, "ttl"),
        TranscriptGrepRequest(
            path=JOURNAL_TRANSCRIPT_URI,
            pattern="TTL",
            output_mode="count",
        ),
        max_response_chars=1_000,
    )

    # Claude uses `rg -c`, whose count is matching lines rather than occurrences.
    assert result.num_matches == 2
    assert result.num_files == 1
    assert result.content == f"{JOURNAL_TRANSCRIPT_URI}:2"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [("TTL", (JOURNAL_TRANSCRIPT_URI,)), ("missing", ())],
)
def test_files_with_matches_returns_only_the_logical_uri(
    pattern: str, expected: tuple[str, ...]
) -> None:
    result = grep_transcript(
        lines(1, "TTL"),
        TranscriptGrepRequest(path=JOURNAL_TRANSCRIPT_URI, pattern=pattern),
        max_response_chars=1_000,
    )

    assert result.filenames == expected
    assert result.num_files == len(expected)


def test_grep_applies_offset_then_head_limit() -> None:
    result = grep_transcript(
        lines(1, "TTL one", 2, "TTL two", 3, "TTL three"),
        request("TTL", offset=1, head_limit=1),
        max_response_chars=10_000,
    )

    assert [match.sequence for match in result.matches] == [2]
    assert result.applied_offset == 1
    assert result.was_truncated is True


def test_head_limit_zero_is_unlimited() -> None:
    result = grep_transcript(
        lines(*(item for sequence in range(1, 302) for item in (sequence, "TTL"))),
        request("TTL", head_limit=0),
        max_response_chars=100_000,
    )

    assert len(result.matches) == 301
    assert result.was_truncated is False


def test_overlapping_context_lines_are_deduplicated() -> None:
    result = grep_transcript(
        lines(1, "before", 2, "target", 3, "between", 4, "target", 5, "after"),
        request("target", context=2),
        max_response_chars=1_000,
    )

    assert [match.sequence for match in result.matches] == [1, 2, 3, 4, 5]


def test_multiline_pattern_returns_every_line_crossed_by_the_match() -> None:
    result = grep_transcript(
        lines(1, "alpha", 2, "middle", 3, "omega", 4, "outside"),
        request("alpha.*omega", multiline=True),
        max_response_chars=1_000,
    )

    assert [match.sequence for match in result.matches] == [1, 2, 3]
    assert all(match.is_match for match in result.matches)


def test_response_budget_truncates_only_at_complete_transcript_lines() -> None:
    transcript = lines(1, '{"content":"first"}', 2, '{"content":"second"}')
    first_rendered = '1\t{"content":"first"}'

    result = grep_transcript(
        transcript,
        request("content"),
        max_response_chars=len(first_rendered),
    )

    assert result.content == first_rendered
    assert [match.sequence for match in result.matches] == [1]
    assert result.was_truncated is True
    assert result.content.endswith("}")


def test_grep_request_rejects_non_journal_paths() -> None:
    with pytest.raises(ValidationError):
        TranscriptGrepRequest(path="/etc/passwd", pattern="root")
