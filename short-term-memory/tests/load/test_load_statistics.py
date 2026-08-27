import json

from scripts.load_test_memory_api import (
    evaluate_slo,
    scenario_report,
    summarize_ms,
)


def test_percentiles_use_inclusive_nearest_rank() -> None:
    stats = summarize_ms(list(range(1, 101)))

    assert stats.p50 == 50
    assert stats.p95 == 95
    assert stats.p99 == 99
    assert stats.maximum == 100


def test_nearest_rank_does_not_interpolate_small_samples() -> None:
    stats = summarize_ms([10.0, 20.0])

    assert stats.p50 == 10.0
    assert stats.p95 == 20.0
    assert stats.p99 == 20.0


def test_gate_fails_when_write_p95_exceeds_150_ms() -> None:
    report = scenario_report("write", latencies=[100.0] * 94 + [151.0] * 6, errors=0)

    result = evaluate_slo(report)

    assert result.exit_code == 1
    assert result.passed is False
    assert any("p95" in violation for violation in result.violations)


def test_gate_fails_when_read_p99_exceeds_200_ms() -> None:
    report = scenario_report("read", latencies=[10.0] * 98 + [201.0] * 2, errors=0)

    assert evaluate_slo(report).exit_code == 1


def test_gate_fails_on_any_request_error() -> None:
    report = scenario_report("read", latencies=[10.0] * 99, errors=1)

    assert evaluate_slo(report).exit_code == 1


def test_gate_passes_at_inclusive_write_limits() -> None:
    report = scenario_report(
        "write", latencies=[1.0] * 94 + [150.0] * 4 + [300.0] * 2, errors=0
    )

    assert report.latency_ms.p95 == 150.0
    assert report.latency_ms.p99 == 300.0
    assert evaluate_slo(report).exit_code == 0


def test_json_report_contains_no_response_body_or_content_fields() -> None:
    report = scenario_report("read", latencies=[10.0], errors=0)
    serialized = json.dumps(report.to_dict())

    assert '"body"' not in serialized
    assert '"content"' not in serialized
