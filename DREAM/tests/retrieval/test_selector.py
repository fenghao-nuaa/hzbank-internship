from pathlib import Path

from dream.memory.artifacts import AtomicArtifactStore
from dream.retrieval.selector import ContextBudget, ContextSelector, estimated_tokens
from dream.core.scope import ScopeIds, resolve_scope
from dream.application.service import DreamService


def test_selector_retrieves_relevant_atomic_memory_within_token_budget() -> None:
    repository = (
        "User prefers concise answers.\n<!-- dream-source: evt-1 -->\n"
        "§\n"
        "User requires independent verification for supplier payment changes.\n"
        "<!-- dream-source: evt-2 -->\n"
        "§\n"
        "User prefers dark mode for dashboards.\n"
        "<!-- dream-source: evt-3 -->\n"
    )
    selector = ContextSelector(
        ContextBudget(
            user_profile_tokens=18,
            decision_rules_tokens=20,
            decision_cards_tokens=20,
        )
    )

    selected = selector.select(
        user_repository=repository,
        user_projection="",
        decision_rules_repository="# AI Decision Rules\n",
        character_projection="",
        decision_cards=(),
        query="supplier payment account change",
    )

    assert "supplier payment changes" in selected.user_profile
    assert "dark mode" not in selected.user_profile
    assert estimated_tokens(selected.user_profile) <= 18
    assert repository.count("dream-source") == 3


def test_selector_retrieves_relevant_decision_card_without_loading_all_cards() -> None:
    payment_card = (
        "# Verify Supplier Payment\n\n"
        "## 决策原则\n\nVerify supplier account changes independently.\n"
    )
    complaint_card = (
        "# Complaint Closure\n\n"
        "## 决策原则\n\nDo not close unresolved complaints for metrics.\n"
    )
    selector = ContextSelector(
        ContextBudget(
            user_profile_tokens=20,
            decision_rules_tokens=20,
            decision_cards_tokens=16,
        )
    )

    selected = selector.select(
        user_repository="",
        user_projection="",
        decision_rules_repository="# AI Decision Rules\n",
        character_projection="",
        decision_cards=(complaint_card, payment_card),
        query="supplier payment account verification",
    )

    assert len(selected.decision_cards) == 1
    assert "Supplier Payment" in selected.decision_cards[0]
    assert "Complaint Closure" not in selected.decision_cards[0]
    assert estimated_tokens(selected.decision_cards[0]) <= 16


def test_start_context_uses_bounded_projections_and_defers_skill_runtime(
    tmp_path: Path,
) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    paths = resolve_scope(tmp_path, ids)
    artifacts = AtomicArtifactStore(paths.agent_root)
    artifacts.write_text(
        Path("users/alice/USER.md"),
        ("Complete durable profile detail. " * 200)
        + "\n<!-- dream-source: evt-full -->\n",
    )
    artifacts.write_text(
        Path("users/alice/USER_PERSONA.md"),
        "Bounded user persona projection.\n",
    )
    artifacts.write_text(
        Path("DECISION_RULES.md"),
        "# Full Decision Repository\n" + ("Durable rule. " * 500),
    )
    artifacts.write_text(
        Path("CHARACTER_DEFINITION.md"),
        "# Bounded Character Projection\n\n- Verify before acting.\n",
    )
    artifacts.write_text(
        Path("skills/historical.skill"),
        "Historical pre-runtime skill.\n",
    )

    context = DreamService(tmp_path).start_context(ids)

    assert context["user_profile"].startswith("Bounded user persona projection.\n")
    assert "evt-full" in context["user_profile"]
    assert "Bounded Character Projection" in context["decision_rules"]
    assert "Full Decision Repository" not in context["decision_rules"]
    assert context["skills"] == []
    assert (paths.user_root / "USER.md").read_text(encoding="utf-8").startswith(
        "Complete durable profile detail."
    )


def test_start_context_loads_new_crypto_domain_from_persona_projection(
    tmp_path: Path,
) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    paths = resolve_scope(tmp_path, ids)
    artifacts = AtomicArtifactStore(paths.agent_root)
    artifacts.write_text(
        Path("users/alice/USER.md"),
        "Bank operations repository.\n<!-- dream-source: evt-bank -->\n"
        "§\n"
        "Crypto investment repository.\n<!-- dream-source: evt-crypto -->\n",
    )
    artifacts.write_text(
        Path("users/alice/USER_PERSONA.md"),
        "# User Persona\n\n"
        "- [bank_operation] User verifies bank payments independently.\n"
        "- [crypto_investment] User is a cryptocurrency beginner and prefers "
        "spot with low leverage.\n",
    )
    artifacts.write_text(
        Path("DECISION_RULES.md"),
        "# AI Decision Rules\n\n- Verify first.\n",
    )
    artifacts.write_text(
        Path("CHARACTER_DEFINITION.md"),
        "# Character Definition\n\n- Verify first.\n",
    )

    context = DreamService(tmp_path).start_context(ids)

    assert "[crypto_investment]" in context["user_profile"]
    assert "cryptocurrency beginner" in context["user_profile"]
    assert "spot with low leverage" in context["user_profile"]
