from short_term_memory.compression.telemetry import InMemoryHeadroomTelemetry


def test_in_memory_telemetry_records_required_metrics() -> None:
    telemetry = InMemoryHeadroomTelemetry()

    telemetry.record_success(tokens_before=120, tokens_after=40)
    telemetry.record_noop()
    telemetry.record_failure()
    telemetry.record_fallback()

    snapshot = telemetry.snapshot()
    assert snapshot.success_count == 1
    assert snapshot.failure_count == 1
    assert snapshot.fallback_count == 1
    assert snapshot.noop_count == 1
    assert snapshot.compression_ratios == (3.0,)
    assert snapshot.as_metrics() == {
        "headroom_compression_success_count": 1,
        "headroom_compression_failure_count": 1,
        "headroom_fallback_count": 1,
        "headroom_noop_count": 1,
        "headroom_compression_ratio": (3.0,),
        "headroom_context_attached_count": 0,
        "headroom_scope_generation_failure_count": 0,
    }


def test_compression_ratio_avoids_division_by_zero() -> None:
    telemetry = InMemoryHeadroomTelemetry()

    telemetry.record_success(tokens_before=20, tokens_after=0)

    assert telemetry.snapshot().compression_ratios == (20.0,)


def test_success_without_token_fields_still_increments_success_count() -> None:
    telemetry = InMemoryHeadroomTelemetry()

    telemetry.record_success(tokens_before=None, tokens_after=None)

    snapshot = telemetry.snapshot()
    assert snapshot.success_count == 1
    assert snapshot.compression_ratios == ()


def test_in_memory_telemetry_records_context_and_scope_metrics() -> None:
    telemetry = InMemoryHeadroomTelemetry()

    telemetry.record_context_attached()
    telemetry.record_scope_generation_failure()

    metrics = telemetry.snapshot().as_metrics()
    assert metrics["headroom_context_attached_count"] == 1
    assert metrics["headroom_scope_generation_failure_count"] == 1
