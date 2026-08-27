import asyncio
from dataclasses import replace

import pytest

from short_term_memory import cli
from short_term_memory.config import ShortTermMemorySettings


def configured_settings() -> ShortTermMemorySettings:
    base = ShortTermMemorySettings(environment="development")
    return replace(
        base,
        headroom_service=replace(base.headroom_service, url="http://headroom:8787"),
        api=replace(
            base.api,
            host="0.0.0.0",
            port=9090,
            workers=4,
            concurrency_limit=64,
        ),
    )


def test_api_cli_uses_factory_workers_and_at_least_100_concurrency(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(cli, "load_settings", configured_settings)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli.api_main([])

    args, kwargs = calls[0]
    assert args == ("short_term_memory.service.runtime:create_runtime_app",)
    assert kwargs["factory"] is True
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9090
    assert kwargs["workers"] == 4
    assert kwargs["limit_concurrency"] == 100


def test_worker_cli_does_not_swallow_configuration_error(monkeypatch) -> None:
    def invalid_settings():
        raise ValueError("invalid runtime configuration")

    monkeypatch.setattr(cli, "load_settings", invalid_settings)

    with pytest.raises(ValueError, match="invalid runtime configuration"):
        cli.worker_main([])


def test_worker_cli_runs_async_worker_process(monkeypatch) -> None:
    observed = []
    settings = configured_settings()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    def run(coroutine):
        observed.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(asyncio, "run", run)

    cli.worker_main([])

    assert len(observed) == 1


class FakeRuntime:
    def __init__(self, worker) -> None:
        self.worker = worker
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_sigterm_stop_waits_for_active_work_within_grace() -> None:
    class Worker:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def run_forever(self, *, stop_event):
            self.entered.set()
            await self.release.wait()
            assert stop_event.is_set()

    worker = Worker()
    runtime = FakeRuntime(worker)
    stop = asyncio.Event()
    running = asyncio.create_task(
        cli.run_worker_process(
            configured_settings(), runtime_start=lambda _settings: _ready(runtime),
            stop_event=stop,
        )
    )
    await worker.entered.wait()
    stop.set()
    await asyncio.sleep(0)
    assert not running.done()
    worker.release.set()

    await asyncio.wait_for(running, timeout=0.2)
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_worker_fatal_error_propagates_and_closes_runtime() -> None:
    class Worker:
        async def run_forever(self, *, stop_event):
            raise RuntimeError("fatal worker")

    runtime = FakeRuntime(Worker())
    with pytest.raises(RuntimeError, match="fatal worker"):
        await cli.run_worker_process(
            configured_settings(), runtime_start=lambda _settings: _ready(runtime)
        )
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_shutdown_timeout_cancels_only_after_grace_expires() -> None:
    class Worker:
        def __init__(self):
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run_forever(self, *, stop_event):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    worker = Worker()
    runtime = FakeRuntime(worker)
    stop = asyncio.Event()
    settings = configured_settings()
    settings = replace(
        settings,
        compression_queue=replace(
            settings.compression_queue, shutdown_grace_seconds=0.05
        ),
    )
    running = asyncio.create_task(
        cli.run_worker_process(
            settings,
            runtime_start=lambda _settings: _ready(runtime),
            stop_event=stop,
        )
    )
    await worker.entered.wait()
    stop.set()
    await asyncio.sleep(0.01)
    assert worker.cancelled.is_set() is False

    await asyncio.wait_for(running, timeout=0.3)
    assert worker.cancelled.is_set() is True
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_pre_stopped_idle_worker_exits_immediately() -> None:
    class Worker:
        async def run_forever(self, *, stop_event):
            assert stop_event.is_set()

    runtime = FakeRuntime(Worker())
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(
        cli.run_worker_process(
            configured_settings(), runtime_start=lambda _settings: _ready(runtime),
            stop_event=stop,
        ),
        timeout=0.2,
    )
    assert runtime.closed is True


async def _ready(value):
    return value
