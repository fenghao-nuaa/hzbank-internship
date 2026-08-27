from pathlib import Path

from dream.memory.artifacts import AtomicArtifactStore
from dream.curators.ai import AICurator
from dream.curators.backend import AICurationPlan, UserCurationPlan
from dream.curators.user import UserCurator
from dream.memory.managers.decision_cards import DecisionCardManager
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.core.scope import ScopeIds, resolve_scope


def _decision(card_id: str, event_id: str) -> ReviewAction:
    return ReviewAction(
        kind=ArtifactKind.DECISION_CARD,
        tool_name="decision_card_manage",
        payload={
            "id": card_id,
            "title": "高风险操作前先验证",
            "scenario": "用户要求执行难以回滚的操作",
            "signals": ["操作不可逆"],
            "principle": "先完成只读验证，再决定是否执行。",
            "outcome": "避免错误修改。",
            "boundaries": "低风险且可回滚时不反复确认。",
            "confidence": 0.82,
        },
        source_event_id=event_id,
    )


def test_ai_curator_consolidates_cards_into_next_task_rules(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    cards = DecisionCardManager(paths)
    cards.apply(_decision("verify-risk-a", "evt-1"))
    cards.apply(_decision("verify-risk-b", "evt-2"))

    report = AICurator(paths).run()

    rules = (paths.agent_root / "DECISION_RULES.md").read_text(encoding="utf-8")
    assert "先完成只读验证，再决定是否执行。" in rules
    assert "verify-risk-a" in rules
    assert "verify-risk-b" in rules
    assert (paths.decision_cards_dir / ".archive" / "verify-risk-b.md").exists()
    assert report.curator == "ai"
    assert report.rollback_snapshot_id


def test_user_curator_merges_duplicate_profile_evidence(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    AtomicArtifactStore(paths.agent_root).write_text(
        Path("users/alice/USER.md"),
        "Prefers concise answers.\n<!-- dream-source: evt-1 -->\n"
        "§\n"
        "Prefers concise answers.\n<!-- dream-source: evt-2 -->\n",
    )

    report = UserCurator(paths).run()

    profile = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert profile.count("Prefers concise answers.") == 1
    assert "evt-1" in profile
    assert "evt-2" in profile
    assert report.curator == "user"
    assert report.rollback_snapshot_id


def test_curators_can_apply_llm_semantic_plans_within_scope(tmp_path: Path) -> None:
    class SemanticBackend:
        def curate_ai(self, *, cards, current_rules):
            assert {card_id for card_id, _ in cards} == {"verify-risk-a", "verify-risk-b"}
            return AICurationPlan(
                decision_rules_markdown=(
                    "# AI Decision Rules\n\n- 对不可逆操作，验证先于执行。\n"
                    "  - Evidence cards: verify-risk-a, verify-risk-b\n"
                ),
                archive_card_ids=("verify-risk-b",),
                summary="Semantic merge",
            )

        def curate_user(self, profile):
            assert "evt-1" in profile and "evt-2" in profile
            return UserCurationPlan(
                user_profile_markdown=(
                    "Prefers concise replies.\n"
                    "<!-- dream-sources: evt-1, evt-2 -->\n"
                ),
                summary="Semantic profile merge",
            )

    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    cards = DecisionCardManager(paths)
    cards.apply(_decision("verify-risk-a", "evt-1"))
    different = _decision("verify-risk-b", "evt-2")
    different.payload["principle"] = "不可逆操作应先确认关键信息。"
    cards.apply(different)
    AtomicArtifactStore(paths.agent_root).write_text(
        Path("users/alice/USER.md"),
        "Prefers concise answers.\n<!-- dream-source: evt-1 -->\n"
        "§\nPrefers brief replies.\n<!-- dream-source: evt-2 -->\n",
    )

    backend = SemanticBackend()
    AICurator(paths, semantic_backend=backend).run()
    UserCurator(paths, semantic_backend=backend).run()

    assert (paths.decision_cards_dir / ".archive" / "verify-risk-b.md").exists()
    assert "不可逆操作" in (paths.agent_root / "DECISION_RULES.md").read_text(
        encoding="utf-8"
    )
    assert "Prefers concise replies." in (paths.user_root / "USER.md").read_text(
        encoding="utf-8"
    )


def test_user_curator_storage_is_not_limited_by_context_budget(
    tmp_path: Path,
) -> None:
    class LargeProfileBackend:
        def curate_user(self, profile):
            assert "evt-large-user" in profile
            return UserCurationPlan(
                user_profile_markdown=(
                    ("Durable long-term user detail. " * 80)
                    + "\n<!-- dream-source: evt-large-user -->\n"
                ),
                summary="Preserve complete durable profile",
            )

    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    AtomicArtifactStore(paths.agent_root).write_text(
        Path("users/alice/USER.md"),
        "Seed detail.\n<!-- dream-source: evt-large-user -->\n",
    )

    UserCurator(paths, semantic_backend=LargeProfileBackend()).run()

    profile = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert len(profile) > 1_375
    assert "evt-large-user" in profile


def test_ai_curator_storage_is_not_limited_by_context_budget(tmp_path: Path) -> None:
    class LargeRulesBackend:
        def curate_ai(self, *, cards, current_rules):
            return AICurationPlan(
                decision_rules_markdown=(
                    "# AI Decision Rules\n\n"
                    + ("Complete durable decision detail. " * 1_700)
                    + "\n  - Evidence cards: verify-risk-a\n"
                ),
                archive_card_ids=(),
                summary="Preserve complete durable rules",
            )

    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    DecisionCardManager(paths).apply(_decision("verify-risk-a", "evt-large-ai"))

    AICurator(paths, semantic_backend=LargeRulesBackend()).run()

    rules = (paths.agent_root / "DECISION_RULES.md").read_text(encoding="utf-8")
    assert len(rules) > 50_000
    assert "verify-risk-a" in rules
