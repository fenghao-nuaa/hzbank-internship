from short_term_memory.service.metrics import ApiMetrics


def test_http_metrics_use_only_bounded_labels() -> None:
    metrics = ApiMetrics()

    metrics.observe_http("/v1/memories/write", "POST", 200, 0.012)
    metrics.observe_http(
        "/v1/memories/write/private-user/private-session", "DELETE", 418, 0.003
    )
    rendered = metrics.render().decode()

    assert 'route="/v1/memories/write"' in rendered
    assert 'route="__other__"' in rendered
    assert 'method="POST"' in rendered
    assert 'method="OTHER"' in rendered
    assert 'status_class="2xx"' in rendered
    assert 'status_class="4xx"' in rendered
    assert "private-user" not in rendered
    assert "private-session" not in rendered


def test_phase_metrics_reject_unbounded_stage_values() -> None:
    metrics = ApiMetrics()

    metrics.observe_phase("redis", 12.5)
    metrics.observe_phase("user-SECRET-stage", 8.0)
    rendered = metrics.render().decode()

    assert 'stage="redis"' in rendered
    assert 'stage="other"' in rendered
    assert "SECRET" not in rendered


def test_in_flight_metric_tracks_and_releases_requests() -> None:
    metrics = ApiMetrics()

    with metrics.track_in_flight():
        active = metrics.render().decode()
        assert "short_term_memory_http_in_flight 1.0" in active

    released = metrics.render().decode()
    assert "short_term_memory_http_in_flight 0.0" in released
