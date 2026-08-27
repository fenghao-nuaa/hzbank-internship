"""Console entrypoints for the independent API and compression worker."""

import argparse
import asyncio
import os
from pathlib import Path
import signal
from typing import Awaitable, Callable, Sequence

import uvicorn

from short_term_memory.config import ShortTermMemorySettings, load_settings
from short_term_memory.service.runtime import ServiceRuntime


def _arguments(argv: Sequence[str] | None, description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to a .env file loaded with process environment taking "
            "priority. Defaults to $SHORT_TERM_MEMORY_ENV_FILE, then ./.env "
            "in the working directory."
        ),
    )
    return parser.parse_args(argv)


def _settings_for(args: argparse.Namespace) -> ShortTermMemorySettings:
    """Load settings, honouring ``--env-file``.

    ``load_settings`` discovers the .env file on its own (explicit path ->
    ``SHORT_TERM_MEMORY_ENV_FILE`` -> ``./.env``). When ``--env-file`` is given
    we set that variable first so it also reaches uvicorn worker subprocesses,
    which fork before the app factory runs.
    """
    if args.env_file is not None:
        os.environ["SHORT_TERM_MEMORY_ENV_FILE"] = str(
            Path(args.env_file).expanduser().resolve()
        )
    return load_settings()


def api_main(argv: Sequence[str] | None = None) -> None:
    args = _arguments(argv, "Run the short-term memory HTTP API")
    settings = _settings_for(args)
    uvicorn.run(
        "short_term_memory.service.runtime:create_runtime_app",
        factory=True,
        host=settings.api.host,
        port=settings.api.port,
        workers=settings.api.workers,
        limit_concurrency=max(100, settings.api.concurrency_limit),
    )


async def run_worker_process(
    settings: ShortTermMemorySettings,
    *,
    runtime_start: Callable[
        [ShortTermMemorySettings], Awaitable[ServiceRuntime]
    ] = ServiceRuntime.start,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run bounded worker loops until cancellation or a termination signal."""

    runtime = await runtime_start(settings)
    loop = asyncio.get_running_loop()
    stopping = stop_event or asyncio.Event()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass

    workers = [runtime.worker]
    if session_worker := getattr(runtime, "session_memory_worker", None):
        workers.append(session_worker)
    worker_tasks = [
        asyncio.create_task(worker.run_forever(stop_event=stopping))
        for worker in workers
    ]
    signal_task = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait(
            {*worker_tasks, signal_task}, return_when=asyncio.FIRST_COMPLETED
        )
        completed_worker = next(
            (task for task in worker_tasks if task in done), None
        )
        if completed_worker is not None:
            await completed_worker
        else:
            try:
                async with asyncio.timeout(
                    settings.compression_queue.shutdown_grace_seconds
                ):
                    await asyncio.shield(asyncio.gather(*worker_tasks))
            except TimeoutError:
                for task in worker_tasks:
                    task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
    finally:
        signal_task.cancel()
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(signal_task, *worker_tasks, return_exceptions=True)
        for signum in installed:
            loop.remove_signal_handler(signum)
        await runtime.close()


def worker_main(argv: Sequence[str] | None = None) -> None:
    args = _arguments(argv, "Run the short-term memory compression worker")
    settings = _settings_for(args)
    asyncio.run(run_worker_process(settings))
