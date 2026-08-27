import pytest

from short_term_memory.compression.policy import HeadroomPolicy


def test_any_plan_threshold_triggers_compression() -> None:
    policy = HeadroomPolicy(
        context_window_tokens=1000,
        trigger_ratio=0.65,
        max_messages=100,
        max_session_seconds=3600,
    )

    assert policy.should_compress(
        estimated_tokens=650, message_count=1, session_seconds=1
    )
    assert policy.should_compress(
        estimated_tokens=1, message_count=100, session_seconds=1
    )
    assert policy.should_compress(
        estimated_tokens=1, message_count=1, session_seconds=3600
    )


@pytest.mark.parametrize("ratio", [0.59, 0.71])
def test_policy_rejects_ratio_outside_plan(ratio: float) -> None:
    with pytest.raises(ValueError, match="between 0.60 and 0.70"):
        HeadroomPolicy(1000, ratio, 100, 3600)
