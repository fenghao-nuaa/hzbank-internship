import asyncio
from dataclasses import replace
import re

import httpx
import pytest
from redis.exceptions import RedisError

from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.service.app import create_app
from short_term_memory.service.memory_service import (
    MemoryReadUnavailableError,
    MemoryTranscriptScopeError,
    RetryableWriteError,
)
from short_term_memory.service.metrics import ApiMetrics
from short_term_memory.service.session_activation import SessionActivationResult
from short_term_memory.service.schemas import (
    HeadroomProxyContext,
    MemoryReadResponse,
    MemoryReadState,
    MemoryPrepareResponse,
    MemoryTranscriptGrepResponse,
    MemoryTranscriptReadResponse,
    MemoryWriteResponse,
    ReadTiming,
    WriteTiming,
)
from short_term_memory.storage.async_redis_memory_store import EventConflictError
from short_term_memory.storage.journal_store import JournalConflictError
from short_term_memory.transcript.grep_tool import TranscriptPatternError
from short_term_memory.transcript.read_tool import (
    TranscriptOffsetError,
    TranscriptResultTooLargeError,
)
from tests.factories import read_payload, write_payload


class RecordingMemoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str]] = []
        self.next_error: BaseException | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.transcript_calls: list[tuple[str, object, str, str]] = []

    async def write(self, request, request_id):
        self.calls.append(("write", request, request_id))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.next_error is not None:
            raise self.next_error
        return MemoryWriteResponse(
            request_id=request_id,
            accepted=True,
            sequence_from=1,
            sequence_through=1,
            duplicate_event_ids=[],
            compression_queued=False,
            policy_version="v1",
            timing_ms=WriteTiming(total=4.0, redis=1.0, journal=2.0, queue=0.5),
        )

    async def read(self, request, request_id):
        self.calls.append(("read", request, request_id))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.next_error is not None:
            raise self.next_error
        return MemoryReadResponse(
            request_id=request_id,
            messages=[{"role": "user", "content": "recent original"}],
            memory=MemoryReadState(
                compressed_through_sequence=0,
                latest_sequence=1,
                source="redis",
                compression_segments=0,
            ),
            headroom=HeadroomProxyContext(
                proxy_url="http://headroom:8787/v1",
                scope_headers={"x-headroom-session-id": "opaque"},
            ),
            effective_config=None,
            timing_ms=ReadTiming(total=3.0, redis=1.0, recovery=0.0, assembly=0.5),
        )

    async def prepare(self, request, request_id):
        self.calls.append(("prepare", request, request_id))
        if self.next_error is not None:
            raise self.next_error
        return MemoryPrepareResponse(
            request_id=request_id,
            messages=[{"role": "user", "content": "prepared"}],
            tools=[],
            headroom=HeadroomProxyContext(
                proxy_url="http://headroom:8787/v1", scope_headers={}
            ),
            compacted=False,
            boundary=None,
        )

    async def recall(self, request, request_id):
        self.calls.append(("recall", request, request_id))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.next_error is not None:
            raise self.next_error
        return {
            "request_id": request_id,
            "results": [
                {"hash": h, "content": f"original-{h}", "recovered": True}
                for h in request.hashes
            ],
        }

    async def grep_transcript(self, request, request_id, *, session_scope):
        self.transcript_calls.append(
            ("grep", request, request_id, session_scope)
        )
        if self.next_error is not None:
            raise self.next_error
        return MemoryTranscriptGrepResponse(
            request_id=request_id,
            mode="content",
            matches=[{"sequence": 87, "text": "TTL", "is_match": True}],
            content="87\tTTL",
            num_lines=1,
            num_matches=1,
        )

    async def read_transcript(self, request, request_id, *, session_scope):
        self.transcript_calls.append(
            ("read", request, request_id, session_scope)
        )
        if self.next_error is not None:
            raise self.next_error
        return MemoryTranscriptReadResponse(
            request_id=request_id,
            content="87\tTTL",
            sequence_from=87,
            sequence_through=87,
            num_lines=1,
            total_lines=1,
        )


class RecordingSessionActivator:
    def __init__(self) -> None:
        self.calls = []

    async def activate(self, user_id, session_id, history_turns=None):
        self.calls.append((user_id, session_id, history_turns))
        return SessionActivationResult(
            recovered=True,
            latest_sequence=180,
            checkpoint_id="sha256:" + "a" * 64,
            rebuild_queued=True,
        )


def settings(
    *,
    token: str = "test-token",
    environment: str = "production",
    concurrency: int = 100,
    max_body_bytes: int = 10 * 1024 * 1024,
) -> ShortTermMemorySettings:
    base = ShortTermMemorySettings(environment=environment)
    return replace(
        base,
        api=replace(
            base.api,
            auth_token=token,
            concurrency_limit=concurrency,
            max_body_bytes=max_body_bytes,
        ),
    )


def app_for(service: RecordingMemoryService, *, activator=None, **setting_overrides):
    app = create_app(
        lambda: service,
        settings=settings(**setting_overrides),
        metrics=ApiMetrics(),
    )
    app.state.session_activator = activator or RecordingSessionActivator()
    return app


def auth_headers(**extra: str) -> dict[str, str]:
    return {"authorization": "Bearer test-token", **extra}


def business_scope(headers: list[tuple[bytes, bytes]]) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/memories/write",
        "raw_path": b"/v1/memories/write",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


async def call_asgi(app, scope, receive):
    sent: list[dict[str, object]] = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_only_approved_business_routes_exist_and_openapi_documents_auth() -> None:
    app = app_for(RecordingMemoryService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        schema = (await client.get("/openapi.json")).json()

    business = sorted(
        path for path in schema["paths"] if path.startswith("/v1/memories/")
    )
    assert business == [
        "/v1/memories/activate",
        "/v1/memories/prepare",
        "/v1/memories/read",
        "/v1/memories/recall",
        "/v1/memories/transcript/grep",
        "/v1/memories/transcript/read",
        "/v1/memories/write",
    ]
    for path in business:
        operation = schema["paths"][path]["post"]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert {"401", "413", "422", "429", "500", "503"} <= set(operation["responses"])
        assert operation["responses"]["422"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorResponse")


@pytest.mark.asyncio
async def test_activate_endpoint_is_authenticated_and_returns_recovery_state() -> None:
    activator = RecordingSessionActivator()
    app = app_for(RecordingMemoryService(), activator=activator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/activate",
            headers=auth_headers(**{"x-request-id": "activation-request"}),
            json={
                "user_id": "u",
                "session_id": "historical",
                "history_turns": 5,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "activation-request",
        "recovered": True,
        "latest_sequence": 180,
        "checkpoint_id": "sha256:" + "a" * 64,
        "rebuild_queued": True,
    }
    assert activator.calls == [("u", "historical", 5)]


@pytest.mark.asyncio
async def test_write_and_read_contract_call_only_the_injected_memory_service() -> None:
    service = RecordingMemoryService()
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        written = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=write_payload()
        )
        read = await client.post(
            "/v1/memories/read", headers=auth_headers(), json=read_payload()
        )

    assert written.status_code == 200
    assert written.json()["accepted"] is True
    assert read.status_code == 200
    assert read.json()["headroom"]["proxy_url"].endswith("/v1")
    assert [call[0] for call in service.calls] == ["write", "read"]
    assert all(
        call[2] == response.headers["x-request-id"]
        for call, response in zip(service.calls, (written, read))
    )


@pytest.mark.asyncio
async def test_recall_contract_calls_only_the_injected_memory_service() -> None:
    service = RecordingMemoryService()
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/recall",
            headers=auth_headers(),
            json={"user_id": "u-1", "session_id": "s-1", "hashes": ["abc123def456"]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["hash"] == "abc123def456"
    assert body["results"][0]["recovered"] is True
    assert [call[0] for call in service.calls] == ["recall"]


@pytest.mark.asyncio
async def test_prepare_contract_returns_model_context_and_request_id() -> None:
    service = RecordingMemoryService()
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/prepare",
            headers=auth_headers(**{"x-request-id": "prepare-request"}),
            json={
                "user_id": "u",
                "session_id": "s",
                "model_profile": {
                    "context_window_tokens": 200_000,
                    "max_output_tokens": 32_000,
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["request_id"] == "prepare-request"
    assert response.json()["messages"][0]["content"] == "prepared"
    assert [call[0] for call in service.calls] == ["prepare"]


@pytest.mark.asyncio
async def test_transcript_routes_bind_scope_and_propagate_request_id() -> None:
    service = RecordingMemoryService()
    app = app_for(service)
    headers = auth_headers(
        **{"x-memory-session-scope": "opaque-current-session"}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        grep = await client.post(
            "/v1/memories/transcript/grep",
            headers={**headers, "x-request-id": "grep-request"},
            json={
                "user_id": "u",
                "session_id": "s",
                "path": "journal://current-session",
                "pattern": "TTL",
                "output_mode": "content",
            },
        )
        read = await client.post(
            "/v1/memories/transcript/read",
            headers={**headers, "x-request-id": "read-request"},
            json={
                "user_id": "u",
                "session_id": "s",
                "file_path": "journal://current-session",
                "offset": 87,
                "limit": 1,
            },
        )

    assert grep.status_code == read.status_code == 200
    assert grep.json()["matches"][0]["sequence"] == 87
    assert read.json()["sequence_from"] == 87
    assert [call[0] for call in service.transcript_calls] == ["grep", "read"]
    assert all(call[3] == "opaque-current-session" for call in service.transcript_calls)
    assert service.transcript_calls[0][2] == "grep-request"
    assert service.transcript_calls[1][2] == "read-request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "path", "status_code", "error_code"),
    [
        (
            MemoryTranscriptScopeError("private scope"),
            "/v1/memories/transcript/grep",
            403,
            "scope_forbidden",
        ),
        (
            TranscriptPatternError("private pattern"),
            "/v1/memories/transcript/grep",
            422,
            "invalid_pattern",
        ),
        (
            TranscriptOffsetError("/private/journal/path"),
            "/v1/memories/transcript/read",
            404,
            "transcript_not_found",
        ),
        (
            TranscriptResultTooLargeError("private transcript"),
            "/v1/memories/transcript/read",
            413,
            "result_too_large",
        ),
        (
            OSError("/private/journal/path"),
            "/v1/memories/transcript/read",
            503,
            "service_unavailable",
        ),
    ],
)
async def test_transcript_errors_are_stable_and_sanitized(
    error, path, status_code, error_code
) -> None:
    service = RecordingMemoryService()
    service.next_error = error
    app = app_for(service)
    payload = {
        "user_id": "u",
        "session_id": "s",
        "path": "journal://current-session",
        "pattern": "TTL",
    }
    if path.endswith("/read"):
        payload = {
            "user_id": "u",
            "session_id": "s",
            "file_path": "journal://current-session",
        }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            path,
            headers=auth_headers(
                **{"x-memory-session-scope": "opaque-current-session"}
            ),
            json=payload,
        )

    assert response.status_code == status_code
    assert response.json()["error"] == error_code
    assert "/private" not in response.text
    assert "private" not in response.text


@pytest.mark.asyncio
async def test_missing_and_wrong_auth_return_identical_sanitized_401() -> None:
    app = app_for(RecordingMemoryService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post("/v1/memories/read", json=read_payload())
        wrong = await client.post(
            "/v1/memories/read",
            headers={"authorization": "Bearer private-wrong-token"},
            json=read_payload(),
        )

    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["error"] == wrong.json()["error"] == "unauthorized"
    assert set(missing.json()) == set(wrong.json()) == {"error", "request_id"}
    assert (
        missing.headers["www-authenticate"]
        == wrong.headers["www-authenticate"]
        == "Bearer"
    )
    assert "private-wrong-token" not in wrong.text


@pytest.mark.asyncio
async def test_unauthenticated_slow_body_is_rejected_without_receive_or_capacity() -> (
    None
):
    service = RecordingMemoryService()
    app = app_for(service, concurrency=1)
    body_reads = 0
    never_second_chunk = asyncio.Event()

    async def slow_receive():
        nonlocal body_reads
        body_reads += 1
        if body_reads == 1:
            return {"type": "http.request", "body": b"{", "more_body": True}
        await never_second_chunk.wait()
        raise AssertionError("unreachable")

    sent = await asyncio.wait_for(
        call_asgi(
            app,
            business_scope([(b"content-type", b"application/json")]),
            slow_receive,
        ),
        timeout=0.2,
    )

    assert body_reads == 0
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        legal = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=write_payload()
        )
    assert legal.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token"],
)
async def test_unauthenticated_malformed_json_is_401_not_validation(
    authorization: str | None,
) -> None:
    headers = {"content-type": "application/json"}
    if authorization is not None:
        headers["authorization"] = authorization
    app = app_for(RecordingMemoryService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write", headers=headers, content=b"not-json"
        )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-OVERSIZED-SECRET"])
async def test_unauthenticated_oversized_content_length_is_401_not_413(
    authorization: str | None,
) -> None:
    headers = {"content-type": "application/json"}
    if authorization is not None:
        headers["authorization"] = authorization
    app = app_for(RecordingMemoryService(), max_body_bytes=8)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write", headers=headers, content=b"OVERSIZED-SECRET"
        )

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert "OVERSIZED-SECRET" not in response.text


@pytest.mark.asyncio
async def test_unauthenticated_oversized_stream_is_401_without_consuming_stream() -> (
    None
):
    yielded = False

    async def private_stream():
        nonlocal yielded
        yielded = True
        yield b"STREAM-SECRET-TOO-LARGE"

    app = app_for(RecordingMemoryService(), max_body_bytes=8)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers={"content-type": "application/json"},
            content=private_stream(),
        )

    assert response.status_code == 401
    assert yielded is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization_headers",
    [
        [
            (b"authorization", b"Bearer test-token"),
            (b"authorization", b"Bearer test-token"),
        ],
        [(b"authorization", b"Bearer \xff")],
    ],
)
async def test_duplicate_or_non_ascii_authorization_is_robustly_rejected(
    authorization_headers: list[tuple[bytes, bytes]],
) -> None:
    app = app_for(RecordingMemoryService())
    received = False

    async def receive():
        nonlocal received
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    sent = await call_asgi(app, business_scope(authorization_headers), receive)

    assert received is False
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_preparse_401_has_request_id_metric_and_no_token_leak() -> None:
    metrics = ApiMetrics()
    app = create_app(
        lambda: RecordingMemoryService(), settings=settings(), metrics=metrics
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers={
                "authorization": "Bearer WRONG-TOKEN-SECRET",
                "x-request-id": "safe-request-401",
                "content-type": "application/json",
            },
            content=b"not-read",
        )
        rendered = (await client.get("/metrics")).text

    assert response.status_code == 401
    assert response.headers["x-request-id"] == "safe-request-401"
    assert response.json() == {
        "error": "unauthorized",
        "request_id": "safe-request-401",
    }
    assert 'status_class="4xx"' in rendered
    assert "WRONG-TOKEN-SECRET" not in rendered


def test_app_construction_enforces_auth_environment_policy() -> None:
    with pytest.raises(ValueError, match="MEMORY_API_AUTH_TOKEN"):
        app_for(RecordingMemoryService(), token="", environment="production")

    app_for(RecordingMemoryService(), token="", environment="development")


@pytest.mark.asyncio
async def test_development_blank_token_consistently_disables_preparse_auth() -> None:
    service = RecordingMemoryService()
    app = app_for(service, token="", environment="development")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/memories/write", json=write_payload())

    assert response.status_code == 200
    assert [call[0] for call in service.calls] == ["write"]


@pytest.mark.asyncio
async def test_request_id_is_bounded_sanitized_and_always_returned() -> None:
    app = app_for(RecordingMemoryService())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.get("/health", headers={"x-request-id": "client.req-7"})
        control = await client.get("/health", headers={"x-request-id": "bad\x01value"})
        oversized = await client.get("/health", headers={"x-request-id": "x" * 1024})
        missing = await client.get("/does-not-exist")

    assert accepted.headers["x-request-id"] == "client.req-7"
    for response in (control, oversized, missing):
        generated = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{32}", generated)
        assert len(generated) == 32


@pytest.mark.asyncio
async def test_content_length_limit_rejects_before_body_parsing() -> None:
    service = RecordingMemoryService()
    app = app_for(service, max_body_bytes=64)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers={**auth_headers(), "content-type": "application/json"},
            content=b"{" + (b"SENSITIVE" * 20),
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert "SENSITIVE" not in response.text
    assert service.calls == []


@pytest.mark.asyncio
async def test_streaming_body_limit_rejects_without_content_length() -> None:
    service = RecordingMemoryService()
    app = app_for(service, max_body_bytes=20)

    async def chunks():
        yield b'{"private":"'
        yield b"STREAMING-SECRET-TOO-LARGE"
        yield b'"}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers={**auth_headers(), "content-type": "application/json"},
            content=chunks(),
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert "STREAMING-SECRET" not in response.text
    assert service.calls == []


@pytest.mark.asyncio
async def test_overload_is_immediate_and_happens_before_json_parsing() -> None:
    service = RecordingMemoryService()
    service.release = asyncio.Event()
    app = app_for(service, concurrency=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        active = asyncio.create_task(
            client.post(
                "/v1/memories/write", headers=auth_headers(), json=write_payload()
            )
        )
        await asyncio.wait_for(service.entered.wait(), timeout=1)
        overloaded = await asyncio.wait_for(
            client.post(
                "/v1/memories/write",
                headers={**auth_headers(), "content-type": "application/json"},
                content=b"not-json",
            ),
            timeout=0.2,
        )
        service.release.set()
        completed = await active

    assert overloaded.status_code == 429
    assert overloaded.json()["error"] == "overloaded"
    assert overloaded.headers["retry-after"] == "1"
    assert completed.status_code == 200


@pytest.mark.asyncio
async def test_cancelled_request_releases_concurrency_capacity() -> None:
    service = RecordingMemoryService()
    service.release = asyncio.Event()
    app = app_for(service, concurrency=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        cancelled = asyncio.create_task(
            client.post(
                "/v1/memories/write", headers=auth_headers(), json=write_payload()
            )
        )
        await asyncio.wait_for(service.entered.wait(), timeout=1)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        service.release.set()
        next_response = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=write_payload()
        )

    assert next_response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (EventConflictError("ORIGINAL PRIVATE MESSAGE"), 409, "event_id_conflict"),
        (JournalConflictError("ORIGINAL PRIVATE MESSAGE"), 409, "event_id_conflict"),
        (RetryableWriteError("private-event", ()), 503, "service_unavailable"),
        (
            MemoryReadUnavailableError("ORIGINAL PRIVATE MESSAGE"),
            503,
            "service_unavailable",
        ),
        (RedisError("redis://user:private-password@host"), 503, "service_unavailable"),
        (RuntimeError("ORIGINAL PRIVATE MESSAGE"), 500, "internal_error"),
    ],
)
async def test_exceptions_map_to_stable_sanitized_errors(error, status, code) -> None:
    service = RecordingMemoryService()
    service.next_error = error
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=write_payload()
        )

    assert response.status_code == status
    assert response.json() == {
        "error": code,
        "request_id": response.headers["x-request-id"],
    }
    assert "ORIGINAL PRIVATE MESSAGE" not in response.text
    assert "private-password" not in response.text
    assert "private-event" not in response.text


@pytest.mark.asyncio
async def test_validation_error_is_sanitized_422_and_service_is_not_called() -> None:
    service = RecordingMemoryService()
    app = app_for(service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers=auth_headers(),
            json={"user_id": "u", "session_id": "s", "events": []},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert set(response.json()) == {"error", "request_id"}
    assert service.calls == []


@pytest.mark.asyncio
async def test_configured_write_batch_limit_maps_to_validation_error() -> None:
    service = RecordingMemoryService()
    custom = settings()
    custom = replace(custom, api=replace(custom.api, write_max_batch_events=1))
    app = create_app(lambda: service, settings=custom, metrics=ApiMetrics())
    payload = write_payload()
    payload["events"].append({**payload["events"][0], "event_id": "event-2"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write", headers=auth_headers(), json=payload
        )

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert service.calls == []


@pytest.mark.asyncio
async def test_metrics_are_content_free_and_have_no_high_cardinality_labels() -> None:
    service = RecordingMemoryService()
    metrics = ApiMetrics()
    app = create_app(lambda: service, settings=settings(), metrics=metrics)
    payload = write_payload(content="SECRET_ANCHOR")
    payload["user_id"] = "SECRET-USER"
    payload["session_id"] = "SECRET-SESSION"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/memories/write",
            headers=auth_headers(**{"x-request-id": "SECRET-REQUEST"}),
            json=payload,
        )
        rendered = (await client.get("/metrics")).text

    assert response.status_code == 200
    for secret in ("SECRET_ANCHOR", "SECRET-USER", "SECRET-SESSION", "SECRET-REQUEST"):
        assert secret not in rendered
    assert 'route="/v1/memories/write"' in rendered
    for stage in ("total", "redis", "journal", "queue"):
        assert f'stage="{stage}"' in rendered
