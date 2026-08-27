import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from short_term_memory.config import ShortTermMemorySettings
from short_term_memory.compression.auto_compact import ModelProfile
from short_term_memory.compression.continuity_model import CompactionModelResponse
from short_term_memory.models import AutoCompactTrackingState, SessionCompressionMessage
from short_term_memory.service.runtime import ServiceRuntime, create_runtime_app


class FakeClosableRedis:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.closed = 0

    async def ping(self):
        if not self.ready:
            raise ConnectionError("redis://private:password@host")
        return True

    async def aclose(self):
        self.closed += 1


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://safe")
            raise httpx.HTTPStatusError("secret response", request=request, response=self)


class FakeClosableAsyncClient:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.is_closed = False
        self.urls: list[str] = []

    async def get(self, url: str, **kwargs):
        self.urls.append(url)
        if not self.ready:
            raise httpx.ConnectError(
                "http://token:secret@headroom", request=httpx.Request("GET", url)
            )
        return FakeResponse()

    async def post(self, *args, **kwargs):
        return FakeResponse()

    async def aclose(self):
        self.is_closed = True


class FailingCloseHttp(FakeClosableAsyncClient):
    async def aclose(self):
        self.is_closed = True
        raise RuntimeError("http close failed")


def settings(tmp_path: Path) -> ShortTermMemorySettings:
    base = ShortTermMemorySettings(home=str(tmp_path), environment="development")
    return replace(
        base,
        headroom_service=replace(
            base.headroom_service, url="http://token:secret@headroom:8787"
        ),
        continuity_compaction=replace(
            base.continuity_compaction, enabled=False
        ),
    )


@pytest.mark.asyncio
async def test_runtime_requires_continuity_model_when_compaction_enabled(tmp_path) -> None:
    configured = settings(tmp_path)
    configured = replace(
        configured,
        continuity_compaction=replace(
            configured.continuity_compaction, enabled=True
        ),
    )
    with pytest.raises(ValueError, match="continuity_model is required"):
        await ServiceRuntime.start(
            configured,
            redis=FakeClosableRedis(),
            headroom_http=FakeClosableAsyncClient(),
        )


@pytest.mark.asyncio
async def test_runtime_shares_injected_continuity_model_with_l4_and_l3(tmp_path) -> None:
    class ContinuityModel:
        def __init__(self):
            self.compact_calls = 0

        async def compact(self, **kwargs):
            self.compact_calls += 1
            return CompactionModelResponse(
                content="<summary>continuity</summary>", output_tokens=10
            )

        async def update_session_memory(self, **kwargs):
            return kwargs["current_memory"]

    configured = settings(tmp_path)
    configured = replace(
        configured,
        continuity_compaction=replace(
            configured.continuity_compaction, enabled=True, model="compact-model"
        ),
    )
    model = ContinuityModel()
    runtime = await ServiceRuntime.start(
        configured,
        redis=FakeClosableRedis(),
        headroom_http=FakeClosableAsyncClient(),
        continuity_model=model,
    )
    assert runtime.session_memory_worker.continuity_model is model
    auto = runtime.context_coordinator.auto_context_factory(
        ModelProfile(context_window_tokens=100_000, max_output_tokens=8_000),
        "main",
        None,
    )
    await auto.compact_conversation(
        (SessionCompressionMessage(role="user", content="context"),),
        AutoCompactTrackingState(),
    )
    assert model.compact_calls == 1


@pytest.mark.asyncio
async def test_runtime_reuses_one_redis_and_one_headroom_http_client(tmp_path) -> None:
    redis = FakeClosableRedis()
    http = FakeClosableAsyncClient()
    runtime = await ServiceRuntime.start(
        settings(tmp_path), redis=redis, headroom_http=http
    )

    assert runtime.memory_service.store.client is runtime.redis
    assert runtime.queue.client is runtime.redis
    assert runtime.completion.client is runtime.redis
    assert runtime.worker.headroom.http is runtime.headroom_http
    assert runtime.session_activator.store is runtime.store
    assert runtime.session_activator.journals is runtime.memory_service.journals
    assert runtime.session_activator.compression_queue is runtime.queue

    await runtime.close()
    await runtime.close()
    assert redis.closed == 0
    assert http.is_closed is False


@pytest.mark.asyncio
async def test_runtime_can_take_explicit_ownership_of_injected_resources(tmp_path) -> None:
    redis = FakeClosableRedis()
    http = FakeClosableAsyncClient()
    runtime = await ServiceRuntime.start(
        settings(tmp_path),
        redis=redis,
        headroom_http=http,
        own_injected=True,
    )

    await runtime.close()
    await runtime.close()

    assert redis.closed == 1
    assert http.is_closed is True


@pytest.mark.asyncio
async def test_close_attempts_all_owned_resources_when_one_close_fails(tmp_path) -> None:
    redis = FakeClosableRedis()
    http = FailingCloseHttp()
    runtime = await ServiceRuntime.start(
        settings(tmp_path), redis=redis, headroom_http=http, own_injected=True
    )

    with pytest.raises(RuntimeError, match="http close failed"):
        await runtime.close()

    assert http.is_closed is True
    assert redis.closed == 1


@pytest.mark.asyncio
async def test_readiness_checks_components_concurrently_and_sanitizes_failures(
    tmp_path,
) -> None:
    redis = FakeClosableRedis(ready=False)
    http = FakeClosableAsyncClient(ready=False)
    runtime = await ServiceRuntime.start(
        settings(tmp_path), redis=redis, headroom_http=http
    )

    readiness = await runtime.readiness()

    assert readiness == {"redis": False, "headroom": False}
    assert "secret" not in repr(readiness)
    await runtime.close()


@pytest.mark.asyncio
async def test_readiness_total_timeout_bounds_never_returning_components(tmp_path) -> None:
    class NeverRedis(FakeClosableRedis):
        async def ping(self):
            await asyncio.Event().wait()

    class NeverHttp(FakeClosableAsyncClient):
        async def get(self, url, **kwargs):
            await asyncio.Event().wait()

    bounded = settings(tmp_path)
    bounded = replace(
        bounded,
        api=replace(bounded.api, request_timeout_seconds=0.03),
        headroom_service=replace(
            bounded.headroom_service, timeout_seconds=0.03
        ),
    )
    runtime = await ServiceRuntime.start(
        bounded, redis=NeverRedis(), headroom_http=NeverHttp()
    )

    started = asyncio.get_running_loop().time()
    assert await runtime.readiness() == {"redis": False, "headroom": False}
    assert asyncio.get_running_loop().time() - started < 0.15


@pytest.mark.asyncio
async def test_readiness_external_cancellation_propagates(tmp_path) -> None:
    class NeverRedis(FakeClosableRedis):
        async def ping(self):
            await asyncio.Event().wait()

    class NeverHttp(FakeClosableAsyncClient):
        async def get(self, url, **kwargs):
            await asyncio.Event().wait()

    runtime = await ServiceRuntime.start(
        settings(tmp_path), redis=NeverRedis(), headroom_http=NeverHttp()
    )
    checking = asyncio.create_task(runtime.readiness())
    await asyncio.sleep(0)
    checking.cancel()

    with pytest.raises(asyncio.CancelledError):
        await checking


@pytest.mark.asyncio
async def test_owned_redis_pool_has_connect_and_socket_timeouts(
    tmp_path, monkeypatch
) -> None:
    captured = {}
    redis = FakeClosableRedis()

    def from_url(url, **kwargs):
        captured.update(kwargs)
        return redis

    monkeypatch.setattr(
        "short_term_memory.service.runtime.redis_async.Redis.from_url", from_url
    )
    runtime = await ServiceRuntime.start(
        settings(tmp_path), headroom_http=FakeClosableAsyncClient()
    )

    assert 0 < captured["socket_connect_timeout"] <= 10
    assert 0 < captured["socket_timeout"] <= 10
    await runtime.close()


class FakeRuntime:
    def __init__(self) -> None:
        self.memory_service = object()
        self.session_activator = object()
        self.closed = False

    async def readiness(self):
        return {"redis": True, "headroom": True}

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_fastapi_lifespan_owns_runtime_and_ready_status(tmp_path) -> None:
    runtime = FakeRuntime()
    starts = 0

    async def start(_settings):
        nonlocal starts
        starts += 1
        return runtime

    app = create_runtime_app(settings(tmp_path), runtime_start=start)
    async with app.router.lifespan_context(app):
        assert app.state.memory_service is runtime.memory_service
        assert app.state.session_activator is runtime.session_activator
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = await client.get("/health")
            ready = await client.get("/ready")

        assert health.status_code == 200
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "ready",
            "components": {"redis": True, "headroom": True},
        }

    assert starts == 1
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_ready_returns_sanitized_503_when_a_component_is_down(tmp_path) -> None:
    runtime = FakeRuntime()

    async def readiness():
        return {"redis": True, "headroom": False}

    runtime.readiness = readiness

    async def start(_settings):
        return runtime

    app = create_runtime_app(settings(tmp_path), runtime_start=start)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"redis": True, "headroom": False},
    }
    assert "token" not in response.text
    assert "secret" not in response.text
