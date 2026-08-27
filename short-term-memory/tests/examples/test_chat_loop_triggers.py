"""The example delegates autonomous recall to the SDK tool loop."""

from __future__ import annotations

from short_term_memory.agent.agent_chat import (
    HEADROOM_RETRIEVE_TOOL_DEFINITION,
    MEMORY_TOOL_DEFINITIONS,
)
from short_term_memory.transcript.tool_definitions import (
    TRANSCRIPT_TOOL_DEFINITIONS,
)


def test_sdk_registers_transcript_and_ccr_recall_tools() -> None:
    assert MEMORY_TOOL_DEFINITIONS == (
        *TRANSCRIPT_TOOL_DEFINITIONS,
        HEADROOM_RETRIEVE_TOOL_DEFINITION,
    )


def test_model_tool_schemas_do_not_expose_memory_tenant_identifiers() -> None:
    for tool in MEMORY_TOOL_DEFINITIONS:
        properties = tool["function"]["parameters"]["properties"]
        assert "user_id" not in properties
        assert "session_id" not in properties


def test_ccr_tool_requires_only_the_marker_hash() -> None:
    parameters = HEADROOM_RETRIEVE_TOOL_DEFINITION["function"]["parameters"]
    assert parameters["required"] == ["hash"]
    assert set(parameters["properties"]) == {"hash"}
