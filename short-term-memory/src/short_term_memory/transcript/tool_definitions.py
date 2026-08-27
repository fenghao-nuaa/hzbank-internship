"""Model-facing Claude-style Grep and Read definitions for Journal recall."""

from typing import Any

from short_term_memory.transcript.journal_transcript import (
    JOURNAL_TRANSCRIPT_URI,
)


TRANSCRIPT_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": (
                "Search the complete current-session Journal transcript with a "
                "regular expression before using Read for an exact range."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {
                        "type": "string",
                        "enum": [JOURNAL_TRANSCRIPT_URI],
                    },
                    "pattern": {"type": "string", "minLength": 1},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "default": "files_with_matches",
                    },
                    "context_before": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "context_after": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "context": {"type": ["integer", "null"], "minimum": 0},
                    "head_limit": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 250,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "default": True,
                    },
                    "multiline": {"type": "boolean", "default": False},
                },
                "required": ["path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": (
                "Read an exact sequence range from the complete current-session "
                "Journal transcript. Use Grep first to locate relevant sequences."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "file_path": {
                        "type": "string",
                        "enum": [JOURNAL_TRANSCRIPT_URI],
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                    },
                    "limit": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                        "maximum": 2_000,
                    },
                },
                "required": ["file_path"],
            },
        },
    },
)
