#!/usr/bin/env python3
"""Run content-free load scenarios against a live short-term-memory API."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from statistics import fmean
import sys
from time import perf_counter_ns
from typing import Any, Sequence
from uuid import uuid4

import httpx


WRITE_SLO_MS = {"p95": 150.0, "p99": 300.0}
READ_SLO_MS = {"p95": 100.0, "p99": 200.0}
SCENARIOS = ("write", "read", "mixed", "same-session", "queue-saturated")


@dataclass(frozen=True)
class LatencyStatistics:
    count: int
    minimum: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    maximum: float | None
    mean: float | None

    def to_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


@dataclass(frozen=True)
class ScenarioReport:
    scenario: str
    attempted: int
    successes: int
    errors: int
    latency_ms: LatencyStatistics
    status_codes: dict[str, int] = field(default_factory=dict)
    error_categories: dict[str, int] = field(default_factory=dict)
    server_timing_ms: dict[str, LatencyStatistics] = field(default_factory=dict)
    duration_ms: float | None = None
    throughput_rps: float | None = None
    operations: dict[str, "ScenarioReport"] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "requests": {
                "attempted": self.attempted,
                "successes": self.successes,
                "errors": self.errors,
            },
            "latency_ms": self.latency_ms.to_dict(),
            "status_codes": dict(sorted(self.status_codes.items())),
            "error_categories": dict(sorted(self.error_categories.items())),
            "server_timing_ms": {
                name: stats.to_dict()
                for name, stats in sorted(self.server_timing_ms.items())
            },
            "duration_ms": self.duration_ms,
            "throughput_rps": self.throughput_rps,
            "operations": {
                name: report.to_dict()
                for name, report in sorted(self.operations.items())
            },
        }


@dataclass(frozen=True)
class SloEvaluation:
    passed: bool
    exit_code: int
    thresholds_ms: dict[str, dict[str, float]]
    violations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RequestSpec:
    operation: str
    path: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RequestMeasurement:
    operation: str
    status_code: int | None
    latency_ms: float
    server_timing_ms: dict[str, float]
    error_category: str | None


def _nearest_rank(sorted_values: Sequence[float], percentile: int) -> float:
    """Return the inclusive nearest-rank percentile without interpolation."""

    rank = max(1, math.ceil(percentile / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize_ms(values: Sequence[float]) -> LatencyStatistics:
    """Summarize millisecond values using inclusive nearest-rank percentiles."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return LatencyStatistics(0, None, None, None, None, None, None)
    return LatencyStatistics(
        count=len(ordered),
        minimum=ordered[0],
        p50=_nearest_rank(ordered, 50),
        p95=_nearest_rank(ordered, 95),
        p99=_nearest_rank(ordered, 99),
        maximum=ordered[-1],
        mean=fmean(ordered),
    )


def scenario_report(
    scenario: str,
    *,
    latencies: Sequence[float],
    errors: int,
    status_codes: dict[str, int] | None = None,
    error_categories: dict[str, int] | None = None,
    server_timings: dict[str, Sequence[float]] | None = None,
    attempted: int | None = None,
    duration_ms: float | None = None,
    operations: dict[str, ScenarioReport] | None = None,
) -> ScenarioReport:
    """Build the serializable, content-free summary used by tests and the CLI."""

    successful = len(latencies)
    total = successful + errors if attempted is None else attempted
    throughput = None
    if duration_ms is not None and duration_ms > 0:
        throughput = total / (duration_ms / 1_000)
    return ScenarioReport(
        scenario=scenario,
        attempted=total,
        successes=successful,
        errors=errors,
        latency_ms=summarize_ms(latencies),
        status_codes=status_codes or {},
        error_categories=error_categories or {},
        server_timing_ms={
            name: summarize_ms(samples)
            for name, samples in (server_timings or {}).items()
        },
        duration_ms=duration_ms,
        throughput_rps=throughput,
        operations=operations or {},
    )


def _thresholds_for(scenario: str) -> dict[str, float] | None:
    if scenario == "read":
        return READ_SLO_MS
    if scenario in {
        "write",
        "same-session",
        "idempotent-replay",
        "queue-saturated",
    }:
        return WRITE_SLO_MS
    return None


def evaluate_slo(report: ScenarioReport) -> SloEvaluation:
    """Apply per-operation latency limits and fail on every request error."""

    violations: list[str] = []
    if report.errors:
        violations.append(f"{report.scenario}: {report.errors} request error(s)")

    measured_reports = report.operations.values() if report.operations else (report,)
    for measured in measured_reports:
        if measured is not report and measured.errors:
            violations.append(
                f"{measured.scenario}: {measured.errors} request error(s)"
            )
        thresholds = _thresholds_for(measured.scenario)
        if thresholds is None:
            violations.append(f"{measured.scenario}: no SLO is defined")
            continue
        if measured.latency_ms.count == 0:
            violations.append(f"{measured.scenario}: no successful latency samples")
            continue
        for percentile, limit_ms in thresholds.items():
            observed = getattr(measured.latency_ms, percentile)
            if observed is not None and observed > limit_ms:
                violations.append(
                    f"{measured.scenario} {percentile} {observed:.3f} ms "
                    f"exceeds {limit_ms:.3f} ms"
                )

    return SloEvaluation(
        passed=not violations,
        exit_code=0 if not violations else 1,
        thresholds_ms={"write": WRITE_SLO_MS, "read": READ_SLO_MS},
        violations=tuple(violations),
    )


def _write_spec(
    *,
    run_id: str,
    index: int,
    user_id: str,
    session_id: str,
    payload_bytes: int,
) -> RequestSpec:
    prefix = f"load-{run_id}-{index}:"
    content = prefix + "x" * max(1, payload_bytes - len(prefix))
    return RequestSpec(
        operation="write",
        path="/v1/memories/write",
        payload={
            "user_id": user_id,
            "session_id": session_id,
            "events": [
                {
                    "event_id": f"event-{run_id}-{index}",
                    "role": "user",
                    "content_type": "conversation",
                    "content": content,
                    "metadata": {"source": "load-test"},
                }
            ],
            "session_seconds": 0,
        },
    )


def _read_spec(*, user_id: str, session_id: str, history_turns: int) -> RequestSpec:
    return RequestSpec(
        operation="read",
        path="/v1/memories/read",
        payload={
            "user_id": user_id,
            "session_id": session_id,
            "history_turns": history_turns,
            "include_effective_config": False,
        },
    )


def _extract_server_timing(response: httpx.Response) -> dict[str, float]:
    if not 200 <= response.status_code < 300:
        return {}
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    timing = payload.get("timing_ms") if isinstance(payload, dict) else None
    if not isinstance(timing, dict):
        return {}
    return {
        str(name): float(value)
        for name, value in timing.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


async def _measure_one(
    client: httpx.AsyncClient, spec: RequestSpec
) -> RequestMeasurement:
    started_ns = perf_counter_ns()
    try:
        response = await client.post(spec.path, json=spec.payload)
        latency_ms = (perf_counter_ns() - started_ns) / 1_000_000
        successful = 200 <= response.status_code < 300
        return RequestMeasurement(
            operation=spec.operation,
            status_code=response.status_code,
            latency_ms=latency_ms,
            server_timing_ms=_extract_server_timing(response),
            error_category=None if successful else f"http_{response.status_code}",
        )
    except Exception as error:  # A failed request is data; the run must continue.
        return RequestMeasurement(
            operation=spec.operation,
            status_code=None,
            latency_ms=(perf_counter_ns() - started_ns) / 1_000_000,
            server_timing_ms={},
            error_category=type(error).__name__,
        )


async def _run_synchronized(
    client: httpx.AsyncClient,
    specs: Sequence[RequestSpec],
    *,
    concurrency: int,
) -> tuple[list[RequestMeasurement], float]:
    """Release a bounded worker set together, then drain all request specs."""

    if not specs:
        return [], 0.0
    start = asyncio.Event()
    worker_count = min(concurrency, len(specs))

    async def worker(worker_index: int) -> list[RequestMeasurement]:
        await start.wait()
        measurements: list[RequestMeasurement] = []
        for index in range(worker_index, len(specs), worker_count):
            measurements.append(await _measure_one(client, specs[index]))
        return measurements

    tasks = [asyncio.create_task(worker(index)) for index in range(worker_count)]
    await asyncio.sleep(0)
    started_ns = perf_counter_ns()
    start.set()
    grouped = await asyncio.gather(*tasks)
    duration_ms = (perf_counter_ns() - started_ns) / 1_000_000
    return [item for group in grouped for item in group], duration_ms


def _report_measurements(
    scenario: str,
    measurements: Sequence[RequestMeasurement],
    duration_ms: float,
) -> ScenarioReport:
    successful_latencies: list[float] = []
    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    server_timings: defaultdict[str, list[float]] = defaultdict(list)
    for measurement in measurements:
        if measurement.status_code is not None:
            statuses[str(measurement.status_code)] += 1
        if measurement.error_category is None:
            successful_latencies.append(measurement.latency_ms)
            for name, value in measurement.server_timing_ms.items():
                server_timings[name].append(value)
        else:
            errors[measurement.error_category] += 1
    return scenario_report(
        scenario,
        latencies=successful_latencies,
        errors=sum(errors.values()),
        status_codes=dict(statuses),
        error_categories=dict(errors),
        server_timings=dict(server_timings),
        attempted=len(measurements),
        duration_ms=duration_ms,
    )


def _split_operations(
    report_name: str, measurements: Sequence[RequestMeasurement], duration_ms: float
) -> ScenarioReport:
    operation_reports = {
        operation: _report_measurements(
            operation,
            [item for item in measurements if item.operation == operation],
            duration_ms,
        )
        for operation in sorted({item.operation for item in measurements})
    }
    overall = _report_measurements(report_name, measurements, duration_ms)
    return ScenarioReport(**{**overall.__dict__, "operations": operation_reports})


async def _queue_backlog(redis_url: str) -> dict[str, int]:
    from redis.asyncio import Redis

    redis = Redis.from_url(redis_url, decode_responses=False)
    try:
        ready, inflight, retry, pending, dead, corrupt = await asyncio.gather(
            redis.llen("dream:compression:ready"),
            redis.zcard("dream:compression:inflight"),
            redis.zcard("dream:compression:retry"),
            redis.scard("dream:compression:pending"),
            redis.zcard("dream:compression:dead"),
            redis.zcard("dream:compression:corrupt"),
        )
    finally:
        await redis.aclose()
    active = int(ready) + int(inflight) + int(retry) + int(pending)
    return {
        "ready": int(ready),
        "inflight": int(inflight),
        "retry": int(retry),
        "pending": int(pending),
        "active_total": active,
        "dead": int(dead),
        "corrupt": int(corrupt),
    }


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    run_id = uuid4().hex
    user_id = f"{args.user_prefix}-{run_id}"
    base_url = args.base_url.rstrip("/")
    headers = {"user-agent": "short-term-memory-load-gate/1"}
    if args.token:
        headers["authorization"] = f"Bearer {args.token}"
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )

    setup_report: ScenarioReport | None = None
    replay_report: ScenarioReport | None = None
    backlog_before: dict[str, int] | None = None
    backlog_after: dict[str, int] | None = None

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=args.timeout,
        limits=limits,
    ) as client:
        if args.scenario in {"read", "mixed"}:
            read_count = (
                args.requests if args.scenario == "read" else args.requests // 2
            )
            setup_specs = [
                _write_spec(
                    run_id=f"{run_id}-warm",
                    index=index,
                    user_id=user_id,
                    session_id=f"warm-session-{run_id}-{index}",
                    payload_bytes=args.payload_bytes,
                )
                for index in range(read_count)
            ]
            setup_measurements, setup_duration = await _run_synchronized(
                client, setup_specs, concurrency=args.concurrency
            )
            setup_report = _report_measurements(
                "write", setup_measurements, setup_duration
            )

        if args.scenario == "queue-saturated":
            backlog_before = await _queue_backlog(args.redis_url)

        if args.scenario == "write":
            specs = [
                _write_spec(
                    run_id=run_id,
                    index=index,
                    user_id=user_id,
                    session_id=f"write-session-{run_id}-{index}",
                    payload_bytes=args.payload_bytes,
                )
                for index in range(args.requests)
            ]
        elif args.scenario == "read":
            specs = [
                _read_spec(
                    user_id=user_id,
                    session_id=f"warm-session-{run_id}-{index}",
                    history_turns=args.history_turns,
                )
                for index in range(args.requests)
            ]
        elif args.scenario == "mixed":
            specs = []
            read_index = 0
            for index in range(args.requests):
                if index % 2 == 0:
                    specs.append(
                        _write_spec(
                            run_id=run_id,
                            index=index,
                            user_id=user_id,
                            session_id=f"mixed-write-{run_id}-{index}",
                            payload_bytes=args.payload_bytes,
                        )
                    )
                else:
                    specs.append(
                        _read_spec(
                            user_id=user_id,
                            session_id=f"warm-session-{run_id}-{read_index}",
                            history_turns=args.history_turns,
                        )
                    )
                    read_index += 1
        elif args.scenario == "same-session":
            specs = [
                _write_spec(
                    run_id=run_id,
                    index=index,
                    user_id=user_id,
                    session_id=f"same-session-{run_id}",
                    payload_bytes=args.payload_bytes,
                )
                for index in range(args.requests)
            ]
        else:
            specs = [
                _write_spec(
                    run_id=run_id,
                    index=index,
                    user_id=user_id,
                    session_id=f"queue-session-{run_id}-{index}",
                    payload_bytes=args.payload_bytes,
                )
                for index in range(args.requests)
            ]

        measurements, duration_ms = await _run_synchronized(
            client, specs, concurrency=args.concurrency
        )
        report = _split_operations(args.scenario, measurements, duration_ms)

        if args.scenario == "same-session":
            replay_specs = [
                RequestSpec("idempotent-replay", spec.path, spec.payload)
                for spec in specs
            ]
            replay_measurements, replay_duration = await _run_synchronized(
                client, replay_specs, concurrency=args.concurrency
            )
            replay_report = _report_measurements(
                "idempotent-replay", replay_measurements, replay_duration
            )
            operations = {**report.operations, "idempotent-replay": replay_report}
            report = ScenarioReport(
                **{
                    **report.__dict__,
                    "errors": report.errors + replay_report.errors,
                    "operations": operations,
                }
            )

        if args.scenario == "queue-saturated":
            backlog_after = await _queue_backlog(args.redis_url)

    if setup_report is not None and setup_report.errors:
        report = ScenarioReport(
            **{**report.__dict__, "errors": report.errors + setup_report.errors}
        )
    evaluation = evaluate_slo(report)
    output = {
        "schema_version": 1,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "scenario": args.scenario,
        "parameters": {
            "base_url": base_url,
            "concurrency": args.concurrency,
            "requests": args.requests,
            "history_turns": args.history_turns,
            "payload_bytes": args.payload_bytes,
            "timeout_seconds": args.timeout,
        },
        "measurement": report.to_dict(),
        "setup": None if setup_report is None else setup_report.to_dict(),
        "idempotent_replay": (
            None if replay_report is None else replay_report.to_dict()
        ),
        "queue_backlog": {
            "before": backlog_before,
            "after": backlog_after,
        },
        "slo": evaluation.to_dict(),
    }
    return output, evaluation.exit_code


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a content-free JSON SLO report against a live memory API."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--concurrency", type=_positive_int, default=100)
    parser.add_argument("--requests", type=_positive_int, default=1_000)
    parser.add_argument("--token", default=os.getenv("MEMORY_API_AUTH_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--user-prefix", default="load-user")
    parser.add_argument("--history-turns", type=_positive_int, default=10)
    parser.add_argument("--payload-bytes", type=_positive_int, default=1_024)
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", ""),
        help="Required only for read-only queue backlog snapshots.",
    )
    parser.add_argument(
        "--confirm-test-queue-configuration",
        action="store_true",
        help="Confirm queue-saturated uses an isolated, test-only worker setup.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.scenario == "queue-saturated":
        if not args.confirm_test_queue_configuration:
            parser.error("queue-saturated requires --confirm-test-queue-configuration")
        if not args.redis_url:
            parser.error("queue-saturated requires --redis-url or REDIS_URL")

    try:
        output, exit_code = asyncio.run(_run(args))
    except Exception as error:
        output = {
            "schema_version": 1,
            "scenario": args.scenario,
            "fatal_error": type(error).__name__,
            "slo": {
                "passed": False,
                "exit_code": 1,
                "violations": ["load run could not be completed"],
            },
        }
        exit_code = 1
    serialized = json.dumps(output, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
