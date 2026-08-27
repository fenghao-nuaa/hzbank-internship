import pytest

from dream.governance.canonicalizer import KnowledgeAdapter
from dream.governance.router import KnowledgeRouter
from dream.governance.persona_models import (
    PersonaCanonicalizationRequired,
    PersonaMergeType,
)
from dream.memory.items import memory_id_for


OLD_RISK_PREFERENCE = "User values explicit risk controls."
EXISTING_MEMORY = (
    f"{OLD_RISK_PREFERENCE}\n"
    "<!-- dream-source: evt-old -->\n"
    "§\n"
    "User prefers concise answers for ordinary tasks.\n"
    "<!-- dream-source: evt-old-2 -->\n"
)


def _proposal(candidate: dict[str, object], *, existing: str = EXISTING_MEMORY):
    return KnowledgeAdapter().adapt(
        {"knowledge_candidates": [candidate]},
        event_ids=("evt-new",),
        existing_memory=existing,
    )


def test_related_new_preference_extends_an_existing_atomic_persona() -> None:
    proposal = _proposal(
        {
            "type": "user_preference",
            "content": (
                "For high-risk issues, the user requires reasons and explicit "
                "conditions to proceed."
            ),
            "confidence": 0.95,
            "source_event_ids": ["evt-new"],
        }
    )

    candidate = proposal.candidates[0]
    actions = KnowledgeRouter().route(proposal, existing_memory=EXISTING_MEMORY)

    assert candidate.attributes["merge_type"] == PersonaMergeType.EXTENSION.value
    assert len(actions) == 1
    assert actions[0].payload["action"] == "replace"
    assert OLD_RISK_PREFERENCE in actions[0].payload["content"]
    assert "conditions to proceed" in actions[0].payload["content"]


def test_auxiliary_persona_information_is_canonicalized_before_dedup() -> None:
    proposal = _proposal(
        {
            "type": "user_preference",
            "id": "mem-placeholder",
            "content": OLD_RISK_PREFERENCE,
            "signals": ["Separate confirmed facts, inferences, and unknowns."],
            "steps": ["State an evidence-based confidence level."],
            "constraints": ["Do not guess an owner when responsibility is unknown."],
            "confidence": 0.95,
            "source_event_ids": ["evt-new"],
        }
    )

    candidate = proposal.candidates[0]
    actions = KnowledgeRouter().route(proposal, existing_memory=EXISTING_MEMORY)

    assert candidate.attributes["merge_type"] == PersonaMergeType.EXTENSION.value
    assert "confirmed facts, inferences, and unknowns" in candidate.content
    assert "evidence-based confidence level" in candidate.content
    assert "Do not guess an owner" in candidate.content
    assert len(actions) == 1
    assert actions[0].payload["action"] == "replace"
    assert actions[0].payload["old_content"] == OLD_RISK_PREFERENCE


def test_exact_persona_duplicate_without_new_information_is_skipped() -> None:
    proposal = _proposal(
        {
            "type": "user_preference",
            "content": OLD_RISK_PREFERENCE,
            "signals": [],
            "steps": [],
            "constraints": [],
            "confidence": 0.9,
            "source_event_ids": ["evt-new"],
        }
    )

    candidate = proposal.candidates[0]

    assert candidate.attributes["merge_type"] == PersonaMergeType.DUPLICATE.value
    assert KnowledgeRouter().route(proposal, existing_memory=EXISTING_MEMORY) == ()


def test_unrelated_persona_is_added_as_a_new_atomic_memory() -> None:
    proposal = _proposal(
        {
            "type": "user_preference",
            "content": "User prefers dark mode for analytical dashboards.",
            "confidence": 0.9,
            "source_event_ids": ["evt-new"],
        }
    )

    candidate = proposal.candidates[0]
    actions = KnowledgeRouter().route(proposal, existing_memory=EXISTING_MEMORY)

    assert candidate.attributes["merge_type"] == PersonaMergeType.NEW.value
    assert len(actions) == 1
    assert actions[0].payload["action"] == "add"


def test_canonical_new_crypto_persona_is_not_merged_into_bank_operations() -> None:
    existing = (
        "User prefers conservative financial transaction safety controls and "
        "low-risk operational strategies.\n"
        "<!-- dream-source: evt-old -->\n"
    )
    proposal = _proposal(
        {
            "type": "user_preference",
            "id": "persona-crypto-beginner",
            "domain": "crypto_investment",
            "merge_type": "new",
            "content": (
                "User is a cryptocurrency beginner with limited funds who "
                "prefers conservative low-risk trading strategies and "
                "transaction safety."
            ),
            "signals": [
                "Needs cryptocurrency jargon explained in plain language.",
                "Prefers spot and low leverage over high-leverage contracts.",
            ],
            "confidence": 0.95,
            "source_event_ids": ["evt-new"],
        },
        existing=existing,
    )

    candidate = proposal.candidates[0]
    actions = KnowledgeRouter().route(proposal, existing_memory=existing)

    assert candidate.attributes["merge_type"] == PersonaMergeType.NEW.value
    assert candidate.attributes["domain"] == "crypto_investment"
    assert len(actions) == 1
    assert actions[0].payload["action"] == "add"
    assert "dream-persona-domain: crypto_investment" in actions[0].payload["content"]


def test_same_domain_explicit_update_replaces_and_persists_persona_metadata() -> None:
    proposal = _proposal(
        {
            "type": "user_preference",
            "id": "persona-bank-risk",
            "domain": "bank_operation",
            "merge_type": "update",
            "target_memory_id": memory_id_for(OLD_RISK_PREFERENCE),
            "content": (
                "User values explicit bank-operation risk controls and requires "
                "confirmed facts to be separated from unknowns."
            ),
            "signals": ["Separate confirmed facts from unknowns."],
            "confidence": 0.95,
            "source_event_ids": ["evt-new"],
        }
    )

    actions = KnowledgeRouter().route(proposal, existing_memory=EXISTING_MEMORY)

    assert len(actions) == 1
    assert actions[0].payload["action"] == "replace"
    assert actions[0].payload["old_content"] == OLD_RISK_PREFERENCE
    assert "dream-persona-domain: bank_operation" in actions[0].payload["content"]


def test_unknown_explicit_persona_target_is_rejected_before_writeback() -> None:
    with pytest.raises(PersonaCanonicalizationRequired, match="target_memory_id"):
        _proposal(
            {
                "type": "user_preference",
                "target_memory_id": "mem-does-not-exist",
                "content": "User requires an explicit evidence status.",
                "new_information": "Add evidence status to risk responses.",
                "confidence": 0.9,
                "source_event_ids": ["evt-new"],
            }
        )


def test_complete_updated_statement_does_not_repeat_the_old_persona() -> None:
    updated = (
        f"{OLD_RISK_PREFERENCE} For high-risk issues, require explicit evidence "
        "status and conditions to proceed."
    )
    proposal = _proposal(
        {
            "type": "user_preference",
            "target_memory_id": memory_id_for(OLD_RISK_PREFERENCE),
            "content": OLD_RISK_PREFERENCE,
            "statement": updated,
            "new_information": (
                "For high-risk issues, require explicit evidence status and "
                "conditions to proceed."
            ),
            "confidence": 0.95,
            "source_event_ids": ["evt-new"],
        }
    )

    candidate = proposal.candidates[0]
    actions = KnowledgeRouter().route(proposal, existing_memory=EXISTING_MEMORY)

    assert candidate.content.count(OLD_RISK_PREFERENCE) == 1
    assert len(actions) == 1
    assert actions[0].payload["content"].count(OLD_RISK_PREFERENCE) == 1
