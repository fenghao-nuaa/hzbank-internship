"""Claude Grep semantics over one service-owned Journal transcript.

Claude source: ``tools/GrepTool/GrepTool.ts``. The physical ripgrep call is
replaced by Python ``re`` over the already session-scoped virtual transcript.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field

from short_term_memory.transcript.journal_transcript import (
    JOURNAL_TRANSCRIPT_URI,
    TranscriptLine,
)

GrepOutputMode = Literal["content", "files_with_matches", "count"]


class TranscriptPatternError(ValueError):
    """The supplied Grep regular expression cannot be compiled."""


class TranscriptGrepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Literal["journal://current-session"]
    pattern: str = Field(min_length=1)
    output_mode: GrepOutputMode = "files_with_matches"
    context_before: int = Field(default=0, ge=0)
    context_after: int = Field(default=0, ge=0)
    context: int | None = Field(default=None, ge=0)
    head_limit: int = Field(default=250, ge=0)
    offset: int = Field(default=0, ge=0)
    case_insensitive: bool = True
    multiline: bool = False


class TranscriptGrepMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    text: str
    is_match: bool = True


class TranscriptGrepResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: GrepOutputMode
    matches: tuple[TranscriptGrepMatch, ...] = ()
    filenames: tuple[str, ...] = ()
    content: str = ""
    num_files: int = Field(default=0, ge=0)
    num_lines: int = Field(default=0, ge=0)
    num_matches: int = Field(default=0, ge=0)
    was_truncated: bool = False
    applied_offset: int | None = Field(default=None, ge=0)


def _render_line(line: TranscriptLine) -> str:
    return f"{line.sequence}\t{line.text}"


def _multiline_match_indices(
    pattern: re.Pattern[str],
    rendered: str,
    transcript_lines: tuple[TranscriptLine, ...],
) -> list[int]:
    if not transcript_lines:
        return []

    spans: list[tuple[int, int]] = []
    cursor = 0
    for line in transcript_lines:
        line_end = cursor + len(_render_line(line))
        spans.append((cursor, line_end))
        cursor = line_end + 1

    matched: set[int] = set()
    for regex_match in pattern.finditer(rendered):
        match_start, match_end = regex_match.span()
        if match_start == match_end:
            match_end += 1
        for index, (line_start, line_end) in enumerate(spans):
            if match_start <= line_end and match_end > line_start:
                matched.add(index)
    return sorted(matched)


def _merge_context_indices(
    matched_indices: Collection[int],
    before: int,
    after: int,
    line_count: int,
) -> list[int]:
    expanded: set[int] = set()
    for index in matched_indices:
        expanded.update(
            range(max(0, index - before), min(line_count, index + after + 1))
        )
    return sorted(expanded)


def _page_indices(
    indices: list[int], offset: int, head_limit: int
) -> tuple[list[int], bool]:
    if head_limit == 0:
        return indices[offset:], False
    return (
        indices[offset : offset + head_limit],
        len(indices) - offset > head_limit,
    )


def _bounded_content_result(
    transcript_lines: tuple[TranscriptLine, ...],
    page: list[int],
    *,
    matched_indices: Collection[int],
    applied_offset: int,
    already_truncated: bool,
    max_response_chars: int,
) -> TranscriptGrepResult:
    if max_response_chars < 0:
        raise ValueError("max_response_chars must be non-negative")

    selected: list[TranscriptGrepMatch] = []
    rendered: list[str] = []
    rendered_chars = 0
    budget_truncated = False
    matched = set(matched_indices)
    for index in page:
        line = transcript_lines[index]
        line_text = _render_line(line)
        added_chars = len(line_text) + (1 if rendered else 0)
        if rendered_chars + added_chars > max_response_chars:
            budget_truncated = True
            break
        rendered.append(line_text)
        rendered_chars += added_chars
        selected.append(
            TranscriptGrepMatch(
                sequence=line.sequence,
                text=line.text,
                is_match=index in matched,
            )
        )

    return TranscriptGrepResult(
        mode="content",
        matches=tuple(selected),
        content="\n".join(rendered),
        num_lines=len(selected),
        num_matches=sum(match.is_match for match in selected),
        was_truncated=already_truncated or budget_truncated,
        applied_offset=applied_offset or None,
    )


def _logical_file_page(
    *, matched: bool, offset: int, head_limit: int
) -> tuple[tuple[str, ...], bool]:
    filenames = [JOURNAL_TRANSCRIPT_URI] if matched else []
    if head_limit == 0:
        return tuple(filenames[offset:]), False
    return (
        tuple(filenames[offset : offset + head_limit]),
        len(filenames) - offset > head_limit,
    )


def grep_transcript(
    transcript_lines: tuple[TranscriptLine, ...],
    request: TranscriptGrepRequest,
    *,
    max_response_chars: int,
) -> TranscriptGrepResult:
    """Search a single logical transcript without exposing server file paths."""

    try:
        flags = re.IGNORECASE if request.case_insensitive else 0
        if request.multiline:
            flags |= re.MULTILINE | re.DOTALL
        pattern = re.compile(request.pattern, flags)
    except re.error as error:
        raise TranscriptPatternError(str(error)) from error

    rendered = "\n".join(_render_line(line) for line in transcript_lines)
    matched_indices = (
        _multiline_match_indices(pattern, rendered, transcript_lines)
        if request.multiline
        else [
            index
            for index, line in enumerate(transcript_lines)
            if pattern.search(line.text)
        ]
    )

    if request.output_mode == "files_with_matches":
        filenames, was_truncated = _logical_file_page(
            matched=bool(matched_indices),
            offset=request.offset,
            head_limit=request.head_limit,
        )
        return TranscriptGrepResult(
            mode="files_with_matches",
            filenames=filenames,
            content="\n".join(filenames),
            num_files=len(filenames),
            was_truncated=was_truncated,
            applied_offset=request.offset or None,
        )

    if request.output_mode == "count":
        filenames, was_truncated = _logical_file_page(
            matched=bool(matched_indices),
            offset=request.offset,
            head_limit=request.head_limit,
        )
        count = len(matched_indices) if filenames else 0
        content = f"{JOURNAL_TRANSCRIPT_URI}:{count}" if filenames else ""
        return TranscriptGrepResult(
            mode="count",
            content=content,
            num_files=len(filenames),
            num_matches=count,
            was_truncated=was_truncated,
            applied_offset=request.offset or None,
        )

    before = (
        request.context
        if request.context is not None
        else request.context_before
    )
    after = (
        request.context
        if request.context is not None
        else request.context_after
    )
    expanded = _merge_context_indices(
        matched_indices, before, after, len(transcript_lines)
    )
    page, more = _page_indices(expanded, request.offset, request.head_limit)
    return _bounded_content_result(
        transcript_lines,
        page,
        matched_indices=matched_indices,
        applied_offset=request.offset,
        already_truncated=more,
        max_response_chars=max_response_chars,
    )
