import pytest

from short_term_memory.compression.compact_prompt import (
    NO_TOOLS_PREAMBLE,
    NO_TOOLS_TRAILER,
    format_compact_summary,
    get_compact_prompt,
    get_compact_user_summary_message,
    get_partial_compact_prompt,
)


FULL_HEADINGS = (
    "Primary Request and Intent",
    "Key Technical Concepts",
    "Files and Code Sections",
    "Errors and fixes",
    "Problem Solving",
    "All user messages",
    "Pending Tasks",
    "Current Work",
    "Optional Next Step",
)


def test_full_prompt_preserves_source_sections_and_no_tools_wrappers() -> None:
    prompt = get_compact_prompt()

    assert prompt.startswith(NO_TOOLS_PREAMBLE)
    assert prompt.endswith(NO_TOOLS_TRAILER)
    positions = [prompt.index(f"{index}. {heading}") for index, heading in enumerate(FULL_HEADINGS, 1)]
    assert positions == sorted(positions)
    assert "<analysis>" in prompt and "<summary>" in prompt


def test_partial_directions_match_claude_scopes() -> None:
    recent = get_partial_compact_prompt(direction="from")
    prefix = get_partial_compact_prompt(direction="up_to")

    assert "RECENT portion" in recent
    assert "8. Current Work" in recent
    assert "9. Optional Next Step" in recent
    assert "newer messages that build on this context will follow" in prefix
    assert "8. Work Completed" in prefix
    assert "9. Context for Continuing Work" in prefix
    assert "8. Current Work" not in prefix


@pytest.mark.parametrize("builder", [get_compact_prompt, get_partial_compact_prompt])
def test_custom_instructions_are_inserted_exactly_once(builder) -> None:
    prompt = builder("focus-on-this-marker")
    assert prompt.count("Additional Instructions:") == 1
    assert prompt.count("focus-on-this-marker") == 1


def test_format_compact_summary_discards_analysis_and_keeps_summary() -> None:
    raw = "<analysis>private chain</analysis>\n<summary>\nA\n\n\nB\n</summary>"
    assert format_compact_summary(raw) == "A\n\nB"


def test_format_without_summary_tag_still_removes_analysis() -> None:
    raw = "before<analysis>private</analysis>\n\n\nvisible"
    assert format_compact_summary(raw) == "before\n\nvisible"


def test_continuation_message_points_to_virtual_transcript_for_automatic_recall() -> None:
    message = get_compact_user_summary_message(
        "structured summary",
        suppress_follow_up_questions=True,
        recent_messages_preserved=True,
    )
    content = str(message.content)

    assert message.role == "user"
    assert message.model_extra["is_compact_summary"] is True
    assert "journal://current-session" in content
    assert "Grep" in content and "Read" in content
    assert "Agent" in content and "自动" in content
    assert "最近消息仍按原文保留" in content
    assert "不要复述摘要" in content
