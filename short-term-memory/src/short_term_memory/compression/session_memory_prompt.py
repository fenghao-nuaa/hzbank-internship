"""Claude L4 Session Memory template and update prompt.

Source: ``services/SessionMemory/prompts.ts``. Claude's local Edit-tool action
is adapted to returning the complete text because this service persists it via
Redis CAS in a different process/container.
"""

from collections.abc import Mapping
import re

MAX_SESSION_MEMORY_SECTION_TOKENS = 2_000
MAX_TOTAL_SESSION_MEMORY_TOKENS = 12_000

SESSION_MEMORY_HEADINGS = (
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

EMPTY_SESSION_MEMORY = """# Session Title
_A short and distinctive 5-10 word descriptive title for the session. Super info dense, no filler_

# Current State
_What is actively being worked on right now? Pending tasks not yet completed. Immediate next steps._

# Task specification
_What did the user ask to build? Any design decisions or other explanatory context_

# Files and Functions
_What are the important files? In short, what do they contain and why are they relevant?_

# Workflow
_What bash commands are usually run and in what order? How to interpret their output if not obvious?_

# Errors & Corrections
_Errors encountered and how they were fixed. What did the user correct? What approaches failed and should not be tried again?_

# Codebase and System Documentation
_What are the important system components? How do they work/fit together?_

# Learnings
_What has worked well? What has not? What to avoid? Do not duplicate items from other sections_

# Key results
_If the user asked a specific output such as an answer to a question, a table, or other document, repeat the exact result here_

# Worklog
_Step by step, what was attempted, done? Very terse summary for each step_
"""

_UPDATE_PROMPT = """IMPORTANT: This message and these instructions are NOT part of the actual user conversation. Do NOT include any references to "note-taking", "session notes extraction", or these update instructions in the notes content.

Based on the user conversation supplied with this request (EXCLUDING this note-taking instruction message as well as system prompt, project instruction entries, or any past session summaries), update the session notes.

The logical storage target is {{memoryPath}}. Here are its current contents:
<current_notes_content>
{{currentNotes}}
</current_notes_content>

Your ONLY task is to return the complete updated notes text, then stop. Do not call tools and do not include commentary, analysis, or Markdown fences around the notes.

CRITICAL RULES FOR EDITING:
- The notes must maintain their exact structure with all sections, headers, and italic descriptions intact
-- NEVER modify, delete, or add section headers (the lines starting with '#' like # Task specification)
-- NEVER modify or delete the italic _section description_ lines (these are the lines in italics immediately following each header - they start and end with underscores)
-- The italic _section descriptions_ are TEMPLATE INSTRUCTIONS that must be preserved exactly as-is - they guide what content belongs in each section
-- ONLY update the actual content that appears BELOW the italic _section descriptions_ within each existing section
-- Do NOT add any new sections, summaries, or information outside the existing structure
- Do NOT reference this note-taking process or instructions anywhere in the notes
- It's OK to skip updating a section if there are no substantial new insights to add. Do not add filler content like "No info yet", just leave sections blank/unedited if appropriate.
- Write DETAILED, INFO-DENSE content for each section - include specifics like logical file paths, function names, error messages, exact commands, technical details, etc.
- For "Key results", include the complete, exact output the user requested (e.g., full table, full answer, etc.)
- Do not include information that's already in project instruction files included in the context
- Keep each section under ~2000 tokens/words - if a section is approaching this limit, condense it by cycling out less important details while preserving the most critical information
- Keep the entire session memory under the maximum of 12000 tokens
- Focus on actionable, specific information that would help someone understand or recreate the work discussed in the conversation
- IMPORTANT: Always update "Current State" to reflect the most recent work - this is critical for continuity after compaction

STRUCTURE PRESERVATION REMINDER:
Each section has TWO parts that must be preserved exactly as they appear in the current notes:
1. The section header (line starting with #)
2. The italic description line (the _italicized text_ immediately after the header - this is a template instruction)

You ONLY update the actual content that comes AFTER these two preserved lines. The italic description lines starting and ending with underscores are part of the template structure, NOT content to be edited or removed.

REMEMBER: Return the complete updated notes text and stop. Only include insights from the actual user conversation, never from these note-taking instructions. Do not delete or change section headers or italic _section descriptions_."""

_VARIABLE_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def _rough_token_count(content: str) -> int:
    """Match Claude's ``Math.round(content.length / 4)`` estimate."""

    return int((len(content) / 4) + 0.5)


def _substitute_variables(template: str, variables: Mapping[str, str]) -> str:
    return _VARIABLE_PATTERN.sub(
        lambda match: variables.get(match.group(1), match.group(0)), template
    )


def _section_sizes(content: str) -> dict[str, int]:
    sections: dict[str, int] = {}
    current_heading = ""
    current_lines: list[str] = []
    for line in content.split("\n"):
        if line.startswith("# "):
            if current_heading and current_lines:
                sections[current_heading] = _rough_token_count(
                    "\n".join(current_lines).strip()
                )
            current_heading = line
            current_lines = []
        else:
            current_lines.append(line)
    if current_heading and current_lines:
        sections[current_heading] = _rough_token_count(
            "\n".join(current_lines).strip()
        )
    return sections


def _size_reminders(current_memory: str) -> str:
    total_tokens = _rough_token_count(current_memory)
    over_budget = total_tokens > MAX_TOTAL_SESSION_MEMORY_TOKENS
    oversized = sorted(
        (
            (heading, tokens)
            for heading, tokens in _section_sizes(current_memory).items()
            if tokens > MAX_SESSION_MEMORY_SECTION_TOKENS
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    if not over_budget and not oversized:
        return ""

    parts: list[str] = []
    if over_budget:
        parts.append(
            "\n\nCRITICAL: The session memory is currently "
            f"~{total_tokens} tokens, which exceeds the maximum of "
            f"{MAX_TOTAL_SESSION_MEMORY_TOKENS} tokens. You MUST condense it "
            "to fit within this budget. Aggressively shorten oversized sections "
            "by removing less important details, merging related items, and "
            'summarizing older entries. Prioritize keeping "Current State" and '
            '"Errors & Corrections" accurate and detailed.'
        )
    if oversized:
        title = (
            "Oversized sections to condense"
            if over_budget
            else "IMPORTANT: The following sections exceed the per-section "
            "limit and MUST be condensed"
        )
        lines = "\n".join(
            f'- "{heading}" is ~{tokens} tokens '
            f"(limit: {MAX_SESSION_MEMORY_SECTION_TOKENS})"
            for heading, tokens in oversized
        )
        parts.append(f"\n\n{title}:\n{lines}")
    return "".join(parts)


def build_session_memory_update_prompt(
    current_memory: str,
    memory_path: str = "redis://current-session/session-memory",
) -> str:
    """Build the isolated L4 update request using Claude's prompt structure."""

    prompt = _substitute_variables(
        _UPDATE_PROMPT,
        {"currentNotes": current_memory, "memoryPath": memory_path},
    )
    return prompt + _size_reminders(current_memory)


def truncate_session_memory_for_compact(content: str) -> tuple[str, bool]:
    """Port ``truncateSessionMemoryForCompact`` and its line-boundary flush."""

    max_chars_per_section = MAX_SESSION_MEMORY_SECTION_TOKENS * 4
    output: list[str] = []
    section_header = ""
    section_lines: list[str] = []
    was_truncated = False

    def flush(header: str, lines: list[str]) -> tuple[list[str], bool]:
        if not header:
            return list(lines), False
        if len("\n".join(lines)) <= max_chars_per_section:
            return [header, *lines], False
        kept = [header]
        char_count = 0
        for line in lines:
            if char_count + len(line) + 1 > max_chars_per_section:
                break
            kept.append(line)
            char_count += len(line) + 1
        kept.append("\n[... section truncated for length ...]")
        return kept, True

    for line in content.split("\n"):
        if line.startswith("# "):
            rendered, truncated = flush(section_header, section_lines)
            output.extend(rendered)
            was_truncated = was_truncated or truncated
            section_header = line
            section_lines = []
        else:
            section_lines.append(line)
    rendered, truncated = flush(section_header, section_lines)
    output.extend(rendered)
    return "\n".join(output), was_truncated or truncated
