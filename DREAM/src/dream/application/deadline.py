"""Cooperative total dream deadline with safe isolation for model waits."""

from queue import Empty, Queue
from threading import Thread
import time
from typing import Callable, TypeVar, cast


T = TypeVar("T")


class DreamDeadlineExceeded(TimeoutError):
    """The ordinary dream transaction exhausted its total time budget."""


class DreamDeadline:
    def __init__(
        self,
        seconds: float = 300.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if seconds <= 0:
            raise ValueError("dream deadline must be positive")
        self.seconds = seconds
        self._clock = clock
        self._started = clock()

    @property
    def elapsed(self) -> float:
        return max(0.0, self._clock() - self._started)

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    def checkpoint(self) -> None:
        if self.remaining <= 0:
            raise DreamDeadlineExceeded("dream transaction deadline exceeded")

    def call(self, operation: Callable[[], T]) -> T:
        """Wait at most the remaining budget for a side-effect-free model call."""

        self.checkpoint()
        outcomes: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcomes.put((True, operation()))
            except BaseException as exc:
                outcomes.put((False, exc))

        worker = Thread(target=invoke, name="dream-model-call", daemon=True)
        worker.start()
        try:
            succeeded, value = outcomes.get(timeout=self.remaining)
        except Empty as exc:
            raise DreamDeadlineExceeded(
                "dream transaction deadline exceeded during model call"
            ) from exc
        self.checkpoint()
        if not succeeded:
            raise cast(BaseException, value)
        return cast(T, value)
