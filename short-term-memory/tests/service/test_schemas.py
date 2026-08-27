import pytest

from short_term_memory.service.schemas import (
    EffectiveMemoryConfig,
    HeadroomProxyContext,
    MemoryReadResponse,
    MemoryReadState,
    MemoryReadRequest,
    MemoryPrepareRequest,
    MemoryRecallResult,
    MemoryTranscriptGrepRequest,
    MemoryTranscriptGrepResponse,
    MemoryTranscriptReadRequest,
    MemoryTranscriptReadResponse,
    MemoryWriteResponse,
    MemoryWriteRequest,
    ReadTiming,
    WriteTiming,
)
from short_term_memory.compression.auto_compact import ModelProfile
from short_term_memory.transcript.tool_definitions import (
    TRANSCRIPT_TOOL_DEFINITIONS,
)


def test_write_schema_accepts_all_four_content_types() -> None:
    request = MemoryWriteRequest.model_validate(
        {
            "user_id": "u1",
            "session_id": "s1",
            "events": [
                {
                    "event_id": f"e-{kind}",
                    "role": "user",
                    "content_type": kind,
                    "content": f"original-{kind}",
                    "metadata": {},
                }
                for kind in ("conversation", "code", "document", "skill")
            ],
        }
    )

    assert [event.content_type.value for event in request.events] == [
        "conversation",
        "code",
        "document",
        "skill",
    ]


def test_write_schema_excludes_server_generated_event_fields() -> None:
    with pytest.raises(ValueError):
        MemoryWriteRequest.model_validate(
            {
                "user_id": "u1",
                "session_id": "s1",
                "events": [
                    {
                        "event_id": "e1",
                        "role": "user",
                        "content_type": "conversation",
                        "content": "original",
                        "metadata": {},
                        "sequence": 1,
                    }
                ],
            }
        )


def test_read_request_accepts_optional_effective_config() -> None:
    request = MemoryReadRequest(
        user_id="u1",
        session_id="s1",
        history_turns=10,
        include_effective_config=True,
    )

    assert request.include_effective_config is True


def test_prepare_schema_rejects_unknown_fields_and_embeds_model_profile() -> None:
    request = MemoryPrepareRequest(
        user_id="u",
        session_id="s",
        model_profile=ModelProfile(
            context_window_tokens=200_000, max_output_tokens=32_000
        ),
    )
    assert request.query_source == "main"
    assert request.model_profile.context_window_tokens == 200_000
    with pytest.raises(ValueError):
        MemoryPrepareRequest.model_validate(
            {**request.model_dump(), "unexpected": True}
        )


def test_transcript_schemas_bind_session_outside_model_tool_arguments() -> None:
    grep = MemoryTranscriptGrepRequest(
        user_id="u1",
        session_id="s1",
        path="journal://current-session",
        pattern="TTL",
        output_mode="content",
    )
    read = MemoryTranscriptReadRequest(
        user_id="u1",
        session_id="s1",
        file_path="journal://current-session",
        offset=87,
        limit=3,
    )

    assert grep.user_id == read.user_id == "u1"
    assert grep.session_id == read.session_id == "s1"
    assert [tool["function"]["name"] for tool in TRANSCRIPT_TOOL_DEFINITIONS] == [
        "Grep",
        "Read",
    ]
    for tool in TRANSCRIPT_TOOL_DEFINITIONS:
        properties = tool["function"]["parameters"]["properties"]
        assert "user_id" not in properties
        assert "session_id" not in properties


def test_transcript_response_schemas_preserve_request_id_and_tool_content() -> None:
    grep = MemoryTranscriptGrepResponse(
        request_id="req-grep",
        mode="content",
        matches=[{"sequence": 87, "text": "TTL", "is_match": True}],
        content="87\tTTL",
        num_lines=1,
        num_matches=1,
    )
    read = MemoryTranscriptReadResponse(
        request_id="req-read",
        content="87\tTTL",
        sequence_from=87,
        sequence_through=87,
        num_lines=1,
        total_lines=100,
    )

    assert grep.request_id == "req-grep"
    assert grep.matches[0].sequence == 87
    assert read.request_id == "req-read"
    assert read.sequence_through == 87


def test_effective_config_never_contains_secrets() -> None:
    assert set(EffectiveMemoryConfig.model_fields) == {
        "history_turns",
        "redis_ttl_seconds",
        "ccr_ttl_seconds",
        "journal_retention_days",
        "trigger_ratio",
        "policy_version",
    }


def test_write_response_dumps_the_approved_nullable_sequence_contract() -> None:
    response = MemoryWriteResponse(
        request_id="req-1",
        accepted=True,
        sequence_from=None,
        sequence_through=None,
        duplicate_event_ids=["event-1"],
        compression_queued=False,
        policy_version="v1",
        timing_ms=WriteTiming(total=42.6, redis=8.1, journal=28.4, queue=1.2),
    )

    assert response.model_dump(mode="json") == {
        "request_id": "req-1",
        "accepted": True,
        "sequence_from": None,
        "sequence_through": None,
        "duplicate_event_ids": ["event-1"],
        "compression_queued": False,
        "policy_version": "v1",
        "timing_ms": {"total": 42.6, "redis": 8.1, "journal": 28.4, "queue": 1.2},
    }


def test_read_response_dumps_the_approved_optional_config_contract() -> None:
    response = MemoryReadResponse(
        request_id="req-2",
        messages=[{"role": "user", "content": "recent original message"}],
        memory=MemoryReadState(
            compressed_through_sequence=100,
            latest_sequence=101,
            source="redis",
            compression_segments=1,
        ),
        headroom=HeadroomProxyContext(
            proxy_url="http://headroom:8787/v1",
            scope_headers={"x-headroom-user-id": "opaque-value"},
        ),
        effective_config=None,
        timing_ms=ReadTiming(total=31.5, redis=12.2, recovery=0.0, assembly=3.1),
    )

    assert response.model_dump(mode="json") == {
        "request_id": "req-2",
        "messages": [{"role": "user", "content": "recent original message"}],
        "memory": {
            "compressed_through_sequence": 100,
            "latest_sequence": 101,
            "source": "redis",
            "compression_segments": 1,
        },
        "headroom": {
            "proxy_url": "http://headroom:8787/v1",
            "scope_headers": {"x-headroom-user-id": "opaque-value"},
        },
        "ccr_markers": [],
        "effective_config": None,
        "timing_ms": {"total": 31.5, "redis": 12.2, "recovery": 0.0, "assembly": 3.1},
    }


def test_unrecovered_ccr_result_allows_empty_content_for_tool_fallback() -> None:
    result = MemoryRecallResult(hash="abc123def456", content="", recovered=False)

    assert result.content == ""
    assert result.recovered is False
