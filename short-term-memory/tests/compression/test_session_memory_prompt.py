from short_term_memory.compression.session_memory_prompt import (
    EMPTY_SESSION_MEMORY,
    SESSION_MEMORY_HEADINGS,
    build_session_memory_update_prompt,
)


def headings(content: str) -> tuple[str, ...]:
    return tuple(line[2:] for line in content.splitlines() if line.startswith("# "))


def test_empty_session_memory_has_exact_claude_ten_section_order() -> None:
    assert headings(EMPTY_SESSION_MEMORY) == SESSION_MEMORY_HEADINGS == (
        "Session Title",
        "Current State",
        "Task specification",
        "Files and Functions",
        "Workflow",
        "Errors & Corrections",
        "Codebase and System Documentation",
        "Learnings",
        "Key results",
        "Worklog",
    )


def test_update_prompt_preserves_claude_limits_and_structure_rules() -> None:
    prompt = build_session_memory_update_prompt(EMPTY_SESSION_MEMORY)

    assert "under ~2000 tokens/words" in prompt
    assert "maximum of 12000 tokens" in prompt
    assert "NEVER modify, delete, or add section headers" in prompt
    assert "italic _section descriptions_" in prompt
    assert "Always update \"Current State\"" in prompt
    assert "return the complete updated notes text" in prompt


def test_update_prompt_includes_current_memory_without_double_substitution() -> None:
    current = "# Session Title\n_Description_\nLiteral {{memoryPath}} marker"

    prompt = build_session_memory_update_prompt(
        current, memory_path="redis://current/session-memory"
    )

    assert current in prompt
    assert "Literal {{memoryPath}} marker" in prompt
    assert "redis://current/session-memory" in prompt


def test_oversized_memory_prioritizes_current_state_and_errors() -> None:
    oversized = EMPTY_SESSION_MEMORY.replace(
        "# Worklog\n_Step by step, what was attempted, done? Very terse summary for each step_",
        "# Worklog\n_Step by step, what was attempted, done? Very terse summary for each step_\n"
        + ("x" * 49_000),
    )

    prompt = build_session_memory_update_prompt(oversized)

    assert "exceeds the maximum of 12000 tokens" in prompt
    assert 'Prioritize keeping "Current State" and "Errors & Corrections"' in prompt
    assert '"# Worklog" is ~' in prompt
    assert "limit: 2000" in prompt


def test_l4_prompt_does_not_contain_l3_nine_section_headings() -> None:
    prompt = build_session_memory_update_prompt(EMPTY_SESSION_MEMORY)

    for l3_heading in (
        "Primary Request and Intent",
        "Key Technical Concepts",
        "Files and Code Sections",
        "All user messages",
        "Pending Tasks",
        "Optional Next Step",
    ):
        assert l3_heading not in prompt
