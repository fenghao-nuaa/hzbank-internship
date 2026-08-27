"""Async composition root for the memory API and compression worker."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import redis.asyncio as redis_async

from short_term_memory.compression.async_headroom_client import AsyncHeadroomClient
from short_term_memory.compression.ccr_recall import CcrRecallClient
from short_term_memory.compression.generations import (
    GenerationAssembler,
    GenerationPlanner,
)
from short_term_memory.compression.auto_compact import AutoCompactContext
from short_term_memory.compression.session_memory_compact import (
    SessionMemoryCompactContext,
    try_session_memory_compaction,
)
from short_term_memory.compression.traditional_compact import (
    TraditionalCompactContext,
    compact_conversation,
)
from short_term_memory.compression.policy import HeadroomPolicy
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.config import ShortTermMemorySettings, load_settings
from short_term_memory.jobs.compression_worker import (
    CompressionWorker,
)
from short_term_memory.jobs.redis_compression_queue import RedisCompressionQueue
from short_term_memory.jobs.session_memory_queue import RedisSessionMemoryQueue
from short_term_memory.jobs.session_memory_worker import SessionMemoryWorker
from short_term_memory.jobs.redis_rebuild_completion import RedisRebuildCompletion
from short_term_memory.service.app import create_app
from short_term_memory.service.memory_service import MemoryService
from short_term_memory.service.session_activation import SessionActivator
from short_term_memory.service.context_coordinator import ContextCoordinator
from short_term_memory.storage.async_redis_memory_store import AsyncRedisMemoryStore
from short_term_memory.storage.journal_store import JournalStore
from short_term_memory.storage.vfs_adapter import VFSAdapter


class ApproximateTokenEstimator:
    """Provider-free conservative estimator for routing compression work."""

    def estimate(self, messages: tuple[dict[str, Any], ...]) -> int:
        characters = sum(len(str(message.get("content", ""))) for message in messages)
        return max(1, (characters + 3) // 4)


@dataclass
class ServiceRuntime:
    settings: ShortTermMemorySettings
    redis: Any
    headroom_http: Any
    store: AsyncRedisMemoryStore
    queue: RedisCompressionQueue
    completion: RedisRebuildCompletion
    worker: CompressionWorker
    session_memory_queue: RedisSessionMemoryQueue
    session_memory_worker: SessionMemoryWorker | None
    memory_service: MemoryService
    context_coordinator: ContextCoordinator
    session_activator: SessionActivator
    _owns_redis: bool
    _owns_headroom_http: bool
    _closed: bool = False

    @classmethod
    async def start(
        cls,
        settings: ShortTermMemorySettings,
        *,
        redis: Any | None = None,
        headroom_http: Any | None = None,
        own_injected: bool = False,
        token_estimator: Any | None = None,
        continuity_model: Any | None = None,
    ) -> "ServiceRuntime":
        """Construct one pool/client graph, closing owned partial state on failure."""

        if not settings.headroom_service.url:
            raise ValueError("HEADROOM_SERVICE_URL is required for the HTTP runtime")
        if settings.continuity_compaction.enabled and continuity_model is None:
            raise ValueError(
                "continuity_model is required when continuity compaction is enabled"
            )
        redis_client = redis
        http_client = headroom_http
        owns_redis = redis is None or own_injected
        owns_http = headroom_http is None or own_injected
        try:
            if redis_client is None:
                redis_client = redis_async.Redis.from_url(
                    settings.redis_session.url,
                    max_connections=settings.api.redis_pool_size,
                    decode_responses=True,
                    socket_connect_timeout=settings.api.request_timeout_seconds,
                    socket_timeout=settings.api.request_timeout_seconds,
                )
            if http_client is None:
                http_client = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=200, max_keepalive_connections=100
                    ),
                    timeout=settings.headroom_service.timeout_seconds,
                )

            journals = JournalStore(
                VFSAdapter(Path(settings.home).expanduser())
            )
            store = AsyncRedisMemoryStore(
                redis_client, ttl_seconds=settings.redis_session.ttl_seconds
            )
            queue = RedisCompressionQueue(
                redis_client, capacity=settings.compression_queue.capacity
            )
            session_memory_queue = RedisSessionMemoryQueue(
                redis_client,
                capacity=settings.compression_queue.capacity,
                lease_seconds=60,
            )
            scope_factory = OptimizationScopeFactory(
                settings.optimization_scope_secret
            )
            completion = RedisRebuildCompletion(
                redis_client,
                store=store,
                scope_factory=scope_factory,
                ttl_seconds=min(
                    settings.redis_session.ttl_seconds,
                    settings.headroom_service.ccr_ttl_seconds,
                ),
            )
            planner = GenerationPlanner(
                store,
                journals,
                max_segments=settings.headroom_service.max_compression_segments,
            )
            assembler = GenerationAssembler(
                max_segments=settings.headroom_service.max_compression_segments
            )
            headroom = AsyncHeadroomClient(
                settings.headroom_service.url,
                timeout_seconds=settings.headroom_service.timeout_seconds,
                http_client=http_client,
            )
            worker = CompressionWorker(
                queue=queue,
                store=store,
                planner=planner,
                headroom=headroom,
                compression_model=settings.headroom_service.compression_model,
                scope_factory=scope_factory,
                ccr_ttl_seconds=settings.headroom_service.ccr_ttl_seconds,
                ccr_refresh_seconds=settings.headroom_service.ccr_refresh_seconds,
                max_segments=settings.headroom_service.max_compression_segments,
                retain_budget=int(
                    settings.redis_session.context_window_tokens
                    * settings.redis_session.retain_ratio
                ),
                worker_concurrency=settings.compression_queue.worker_concurrency,
                completion_publisher=completion,
            )
            policy = HeadroomPolicy(
                context_window_tokens=settings.redis_session.context_window_tokens,
                trigger_ratio=settings.redis_session.trigger_ratio,
                max_messages=settings.redis_session.max_messages,
                max_session_seconds=settings.redis_session.max_session_seconds,
            )
            recall_client = CcrRecallClient(
                settings.headroom_service.url,
                timeout_seconds=settings.headroom_service.timeout_seconds,
                http_client=http_client,
            )
            estimator = token_estimator or ApproximateTokenEstimator()
            memory_service = MemoryService(
                store=store,
                journals=journals,
                assembler=assembler,
                compression_queue=queue,
                policy=policy,
                scope_factory=scope_factory,
                settings=settings,
                token_estimator=estimator,
                headroom_proxy_url=f"{settings.headroom_service.url.rstrip('/')}/v1",
                rebuild_waiter=completion,
                recall_client=recall_client,
                session_memory_queue=(
                    session_memory_queue
                    if settings.continuity_compaction.enabled
                    else None
                ),
            )
            def auto_context_factory(model_profile, query_source, session_memory):
                effective_source = (
                    query_source
                    if settings.continuity_compaction.enabled
                    else "compact"
                )

                async def l4(messages, threshold):
                    return await try_session_memory_compaction(
                        messages=messages,
                        session_memory=session_memory,
                        context=SessionMemoryCompactContext(
                            token_estimator=estimator,
                            history_turns=settings.redis_session.history_turns,
                            auto_compact_threshold=threshold,
                        ),
                    )

                async def l3(messages, tracking):
                    del tracking
                    return await compact_conversation(
                        messages,
                        TraditionalCompactContext(
                            model=continuity_model,
                            model_name=settings.continuity_compaction.model,
                            token_estimator=estimator,
                        ),
                        is_auto_compact=True,
                    )

                return AutoCompactContext(
                    model_profile=model_profile,
                    token_estimator=estimator,
                    query_source=effective_source,
                    try_session_memory=l4,
                    compact_conversation=l3,
                )

            context_coordinator = ContextCoordinator(
                store=store,
                checkpoint_journal=journals,
                token_estimator=estimator,
                auto_context_factory=auto_context_factory,
                history_turns=settings.redis_session.history_turns,
                headroom_proxy_url=f"{settings.headroom_service.url.rstrip('/')}/v1",
                scope_headers_factory=lambda user, session: scope_factory.for_session(
                    user, session
                ).as_headroom_headers(),
                microcompact_config=settings.time_based_microcompact,
            )
            session_activator = SessionActivator(
                store=store,
                journals=journals,
                compression_queue=queue,
                history_turns=settings.redis_session.history_turns,
                activation_timeout_seconds=settings.api.request_timeout_seconds,
            )
            session_memory_worker = (
                SessionMemoryWorker(
                    queue=session_memory_queue,
                    store=store,
                    journals=journals,
                    continuity_model=continuity_model,
                    model_name=settings.continuity_compaction.model,
                )
                if settings.continuity_compaction.enabled
                else None
            )
            return cls(
                settings=settings,
                redis=redis_client,
                headroom_http=http_client,
                store=store,
                queue=queue,
                completion=completion,
                worker=worker,
                session_memory_queue=session_memory_queue,
                session_memory_worker=session_memory_worker,
                memory_service=memory_service,
                context_coordinator=context_coordinator,
                session_activator=session_activator,
                _owns_redis=owns_redis,
                _owns_headroom_http=owns_http,
            )
        except BaseException:
            partial_closers = []
            if owns_http and http_client is not None:
                partial_closers.append(http_client.aclose())
            if owns_redis and redis_client is not None:
                partial_closers.append(redis_client.aclose())
            if partial_closers:
                await asyncio.gather(*partial_closers, return_exceptions=True)
            raise

    async def readiness(self) -> dict[str, bool]:
        """Return sanitized component booleans; never propagate endpoint details."""

        async def redis_ready() -> bool:
            try:
                return bool(await self.redis.ping())
            except Exception:
                return False

        async def headroom_ready() -> bool:
            try:
                response = await self.headroom_http.get(
                    f"{self.settings.headroom_service.url.rstrip('/')}/health",
                    timeout=min(
                        5.0, self.settings.headroom_service.timeout_seconds
                    ),
                )
                response.raise_for_status()
                return True
            except Exception:
                return False

        timeout_seconds = min(
            5.0,
            self.settings.api.request_timeout_seconds,
            self.settings.headroom_service.timeout_seconds,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                redis_ok, headroom_ok = await asyncio.gather(
                    redis_ready(), headroom_ready()
                )
        except TimeoutError:
            redis_ok = headroom_ok = False
        return {"redis": redis_ok, "headroom": headroom_ok}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        closers = []
        if self._owns_headroom_http:
            closers.append(self.headroom_http.aclose())
        if self._owns_redis:
            closers.append(self.redis.aclose())
        if not closers:
            return
        results = await asyncio.gather(*closers, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result


class _StartingMemoryService:
    async def write(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("memory runtime has not started")

    async def read(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("memory runtime has not started")


def create_runtime_app(
    settings: ShortTermMemorySettings | None = None,
    *,
    runtime_start: Callable[
        [ShortTermMemorySettings], Awaitable[ServiceRuntime]
    ] = ServiceRuntime.start,
):
    """Uvicorn app factory; every process owns exactly one async runtime."""

    effective_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app):
        runtime = await runtime_start(effective_settings)
        app.state.service_runtime = runtime
        app.state.memory_service = runtime.memory_service
        app.state.session_activator = runtime.session_activator
        if context_coordinator := getattr(runtime, "context_coordinator", None):
            app.state.context_coordinator = context_coordinator
        try:
            yield
        finally:
            await runtime.close()

    return create_app(
        lambda: _StartingMemoryService(),
        settings=effective_settings,
        lifespan=lifespan,
    )
