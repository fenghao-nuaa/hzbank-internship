"""Observable Headroom service metrics without conversation content."""

from dataclasses import dataclass
from threading import Lock
from typing import Protocol


@dataclass(frozen=True)
class HeadroomMetricsSnapshot:
    success_count: int
    failure_count: int
    fallback_count: int
    noop_count: int
    compression_ratios: tuple[float, ...]
    context_attached_count: int
    scope_generation_failure_count: int

    def as_metrics(self) -> dict[str, int | tuple[float, ...]]:
        return {
            "headroom_compression_success_count": self.success_count,
            "headroom_compression_failure_count": self.failure_count,
            "headroom_fallback_count": self.fallback_count,
            "headroom_noop_count": self.noop_count,
            "headroom_compression_ratio": self.compression_ratios,
            "headroom_context_attached_count": self.context_attached_count,
            "headroom_scope_generation_failure_count": (
                self.scope_generation_failure_count
            ),
        }


class HeadroomTelemetry(Protocol):
    def record_success(
        self,
        *,
        tokens_before: int | None,
        tokens_after: int | None,
    ) -> None: ...

    def record_failure(self) -> None: ...

    def record_fallback(self) -> None: ...

    def record_noop(self) -> None: ...

    def record_context_attached(self) -> None: ...

    def record_scope_generation_failure(self) -> None: ...


class InMemoryHeadroomTelemetry:
    """Thread-safe default metrics store replaceable by enterprise telemetry."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._success_count = 0
        self._failure_count = 0
        self._fallback_count = 0
        self._noop_count = 0
        self._compression_ratios: list[float] = []
        self._context_attached_count = 0
        self._scope_generation_failure_count = 0

    def record_success(
        self,
        *,
        tokens_before: int | None,
        tokens_after: int | None,
    ) -> None:
        with self._lock:
            self._success_count += 1
            if tokens_before is not None and tokens_after is not None:
                self._compression_ratios.append(tokens_before / max(tokens_after, 1))

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1

    def record_fallback(self) -> None:
        with self._lock:
            self._fallback_count += 1

    def record_noop(self) -> None:
        with self._lock:
            self._noop_count += 1

    def record_context_attached(self) -> None:
        with self._lock:
            self._context_attached_count += 1

    def record_scope_generation_failure(self) -> None:
        with self._lock:
            self._scope_generation_failure_count += 1

    def snapshot(self) -> HeadroomMetricsSnapshot:
        with self._lock:
            return HeadroomMetricsSnapshot(
                success_count=self._success_count,
                failure_count=self._failure_count,
                fallback_count=self._fallback_count,
                noop_count=self._noop_count,
                compression_ratios=tuple(self._compression_ratios),
                context_attached_count=self._context_attached_count,
                scope_generation_failure_count=(
                    self._scope_generation_failure_count
                ),
            )
