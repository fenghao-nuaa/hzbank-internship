import pytest

import short_term_memory.compression.scope as optimization_scope_module
from short_term_memory.compression.scope import OptimizationScopeFactory
from short_term_memory.compression.telemetry import InMemoryHeadroomTelemetry


def test_scope_is_stable_private_and_session_isolated() -> None:
    factory = OptimizationScopeFactory("test-secret-with-enough-entropy")
    first = factory.for_session("alice@example.com", "session-1")

    assert first == factory.for_session("alice@example.com", "session-1")
    assert first.session_scope != factory.for_session(
        "alice@example.com", "session-2"
    ).session_scope
    assert first.user_scope != factory.for_session(
        "bob@example.com", "session-1"
    ).user_scope
    rendered = " ".join(first.__dict__.values())
    assert "alice" not in rendered
    assert "session-1" not in rendered


def test_scope_maps_to_official_headroom_headers() -> None:
    scope = OptimizationScopeFactory("test-secret").for_session(
        "user", "session"
    )

    assert scope.as_headroom_headers() == {
        "x-headroom-user-id": scope.user_scope,
        "x-headroom-session-id": scope.session_scope,
        "x-headroom-project-id": scope.workspace_scope,
    }


@pytest.mark.parametrize(
    ("user_id", "session_id"),
    (("", "session"), ("user", "")),
)
def test_scope_factory_rejects_blank_identity(
    user_id: str,
    session_id: str,
) -> None:
    factory = OptimizationScopeFactory("test-secret-with-enough-entropy")

    with pytest.raises(ValueError, match="user_id and session_id"):
        factory.for_session(user_id, session_id)


def test_scope_factory_rejects_blank_secret() -> None:
    with pytest.raises(ValueError, match="scope secret must not be blank"):
        OptimizationScopeFactory("   ")


def test_scope_generation_failure_is_counted_and_reraised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = InMemoryHeadroomTelemetry()
    factory = OptimizationScopeFactory("test-secret", telemetry=telemetry)

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("private HMAC failure")

    monkeypatch.setattr(optimization_scope_module.hmac, "new", fail)

    with pytest.raises(RuntimeError, match="private HMAC failure"):
        factory.for_session("private-user", "private-session")

    assert telemetry.snapshot().scope_generation_failure_count == 1
