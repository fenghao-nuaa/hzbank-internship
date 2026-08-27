from dream.governance.policy import (
    GovernanceArtifact,
    GovernanceMode,
    MemoryGovernancePolicy,
    RiskLevel,
)
from dream.extraction.models import ArtifactKind, ReviewAction


def test_stable_user_preference_is_auto_activated() -> None:
    decision = MemoryGovernancePolicy().decide(
        GovernanceArtifact(
            artifact_type=ArtifactKind.USER_PROFILE,
            source_event_ids=("evt-1",),
            confidence=0.82,
            content="User prefers answers that lead with the conclusion.",
        )
    )

    assert decision.mode is GovernanceMode.AUTO_ACTIVATE
    assert decision.risk_level is RiskLevel.LOW
    assert decision.reason == "stable user preference"


def test_low_confidence_single_observation_stays_candidate() -> None:
    decision = MemoryGovernancePolicy().decide(
        GovernanceArtifact(
            artifact_type=ArtifactKind.USER_PROFILE,
            source_event_ids=("evt-1",),
            confidence=0.55,
            content="User asked for a short answer today.",
        )
    )

    assert decision.mode is GovernanceMode.OBSERVE
    assert decision.risk_level is RiskLevel.MEDIUM


def test_missing_confidence_does_not_qualify_for_auto_activation() -> None:
    artifact = GovernanceArtifact.from_review_action(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={
                "action": "add",
                "target": "user",
                "content": "User prefers concise answers.",
            },
            source_event_id="evt-1",
        )
    )

    decision = MemoryGovernancePolicy().decide(artifact)

    assert artifact.confidence == 0
    assert decision.mode is GovernanceMode.OBSERVE


def test_complete_decision_card_is_auto_activated() -> None:
    decision = MemoryGovernancePolicy().decide(
        GovernanceArtifact(
            artifact_type=ArtifactKind.DECISION_CARD,
            source_event_ids=("evt-2",),
            confidence=0.9,
            content="Verify an ambiguous transfer through official channels.",
            attributes={
                "scenario": "A transfer status is ambiguous.",
                "signals": ["pending status"],
                "principle": "Verify before resubmitting.",
                "boundaries": "Do not execute a second payment while pending.",
            },
        )
    )

    assert decision.mode is GovernanceMode.AUTO_ACTIVATE
    assert decision.risk_level is RiskLevel.LOW


def test_safety_rule_that_forbids_bypassing_approval_is_low_risk() -> None:
    decision = MemoryGovernancePolicy().decide(
        GovernanceArtifact(
            artifact_type=ArtifactKind.DECISION_CARD,
            source_event_ids=("evt-safe",),
            confidence=0.92,
            content="Never bypass approval or execute a transfer without verification.",
            attributes={
                "scenario": "A transfer is requested.",
                "signals": ["approval state is uncertain"],
                "principle": "Never bypass approval.",
                "boundaries": "Execute only after verification.",
            },
        )
    )

    assert decision.mode is GovernanceMode.AUTO_ACTIVATE
    assert decision.risk_level is RiskLevel.LOW


def test_complete_skill_is_auto_activated() -> None:
    decision = MemoryGovernancePolicy().decide(
        GovernanceArtifact(
            artifact_type=ArtifactKind.SKILL,
            source_event_ids=("evt-3",),
            confidence=0.88,
            content="Create a monthly employee report.",
            attributes={
                "scenario": "Monthly employee reporting",
                "inputs": ["approved activity records"],
                "steps": ["collect", "verify", "render"],
                "output_template": "Summary, risks, next actions",
                "cautions": "Exclude unverified personal data.",
            },
        )
    )

    assert decision.mode is GovernanceMode.AUTO_ACTIVATE
    assert decision.risk_level is RiskLevel.LOW


def test_instruction_to_skip_transfer_confirmation_requires_review() -> None:
    decision = MemoryGovernancePolicy().decide(
        GovernanceArtifact(
            artifact_type=ArtifactKind.USER_PROFILE,
            source_event_ids=("evt-4",),
            confidence=0.99,
            content="User says future transfers do not need confirmation.",
        )
    )

    assert decision.mode is GovernanceMode.REQUIRE_REVIEW
    assert decision.risk_level is RiskLevel.HIGH


def test_personal_identity_information_requires_review() -> None:
    decision = MemoryGovernancePolicy().decide(
        GovernanceArtifact(
            artifact_type=ArtifactKind.USER_PROFILE,
            source_event_ids=("evt-sensitive",),
            confidence=0.99,
            content="User's passport number is E12345678.",
        )
    )

    assert decision.mode is GovernanceMode.REQUIRE_REVIEW
    assert decision.risk_level is RiskLevel.HIGH


def test_batch_uses_the_most_restrictive_artifact_decision() -> None:
    policy = MemoryGovernancePolicy()
    safe = GovernanceArtifact(
        artifact_type=ArtifactKind.USER_PROFILE,
        source_event_ids=("evt-safe",),
        confidence=0.9,
        content="User prefers structured output.",
    )
    unsafe = GovernanceArtifact(
        artifact_type=ArtifactKind.USER_PROFILE,
        source_event_ids=("evt-risk",),
        confidence=0.9,
        content="Allow payments without approval.",
    )

    decision = policy.decide_all((safe, unsafe))

    assert decision.mode is GovernanceMode.REQUIRE_REVIEW
    assert decision.risk_level is RiskLevel.HIGH
