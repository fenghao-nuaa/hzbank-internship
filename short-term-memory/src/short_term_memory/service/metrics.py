"""Content-free, bounded-label Prometheus metrics for the memory API."""

from contextlib import contextmanager
from typing import Iterator

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class ApiMetrics:
    """Own an isolated registry so application instances cannot collide."""

    _ROUTES = frozenset(
        {
            "/v1/memories/write",
            "/v1/memories/read",
            "/health",
            "/ready",
            "/metrics",
            "/openapi.json",
        }
    )
    _METHODS = frozenset({"GET", "POST"})
    _STAGES = frozenset({"total", "redis", "journal", "queue", "recovery", "assembly"})

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "short_term_memory_http_requests_total",
            "Memory API HTTP requests.",
            ("route", "method", "status_class"),
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "short_term_memory_http_request_duration_seconds",
            "Memory API HTTP request duration.",
            ("route", "method", "status_class"),
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "short_term_memory_http_in_flight",
            "Memory API requests currently executing.",
            registry=self.registry,
        )
        self.phase_duration = Histogram(
            "short_term_memory_phase_duration_seconds",
            "Memory API use-case phase duration.",
            ("stage",),
            registry=self.registry,
        )

    @contextmanager
    def track_in_flight(self) -> Iterator[None]:
        self.in_flight.inc()
        try:
            yield
        finally:
            self.in_flight.dec()

    def observe_http(
        self, path: str, method: str, status_code: int, duration_seconds: float
    ) -> None:
        route = path if path in self._ROUTES else "__other__"
        normalized_method = method.upper()
        if normalized_method not in self._METHODS:
            normalized_method = "OTHER"
        status_class = (
            f"{status_code // 100}xx" if 100 <= status_code <= 599 else "other"
        )
        labels = (route, normalized_method, status_class)
        self.requests.labels(*labels).inc()
        self.request_duration.labels(*labels).observe(max(duration_seconds, 0.0))

    def observe_phase(self, stage: str, duration_ms: float) -> None:
        normalized_stage = stage if stage in self._STAGES else "other"
        self.phase_duration.labels(normalized_stage).observe(
            max(duration_ms, 0.0) / 1_000
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
