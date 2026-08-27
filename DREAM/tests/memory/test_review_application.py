import json
import hashlib
from pathlib import Path

import pytest

from dream.memory.artifacts import AtomicArtifactStore
from dream.memory.managers.decision_cards import DecisionCardManager
from dream.memory.managers.persona import MemoryManager
from dream.memory.storage.reports import DreamReportStore
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.memory.storage.rollback import RollbackService
from dream.core.scope import ScopeIds, resolve_scope


def _memory_id(content: str) -> str:
    return "mem-" + hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def test_user_profile_action_is_scoped_and_cites_source(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    manager = MemoryManager(paths)
    version = manager.apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={"action": "add", "content": "Prefers concise answers."},
            source_event_id="evt-1",
        )
    )
    content = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert version.sha256
    assert "Prefers concise answers." in content
    assert "evt-1" in content
    assert not (paths.agent_root / "users" / "bob" / "USER.md").exists()


def test_user_profile_storage_accepts_content_larger_than_context_budget(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    durable_profile = "Durable preference detail. " * 80

    MemoryManager(paths).apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={"action": "add", "content": durable_profile},
            source_event_id="evt-large-profile",
        )
    )

    stored = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert len(stored) > 1_375
    assert durable_profile.strip() in stored


def test_decision_card_is_a_human_readable_markdown_artifact(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    manager = DecisionCardManager(paths)
    version = manager.apply(
        ReviewAction(
            kind=ArtifactKind.DECISION_CARD,
            tool_name="decision_card_manage",
            payload={
                "id": "verify-before-risky-action",
                "title": "高风险操作前先验证",
                "scenario": "用户要求执行难以回滚的操作",
                "signals": ["操作不可逆", "关键信息不足"],
                "principle": "先完成只读验证，再决定是否执行。",
                "outcome": "避免错误修改。",
                "boundaries": "低风险且可回滚的操作不需要反复确认。",
                "confidence": 0.82,
            },
            source_event_id="evt-2",
        )
    )
    card = paths.decision_cards_dir / "verify-before-risky-action.md"
    content = card.read_text(encoding="utf-8")
    assert version.sha256
    assert "# 高风险操作前先验证" in content
    assert "## 决策原则" in content
    assert "evt-2" in content


def test_decision_card_update_increments_version_and_preserves_history(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    manager = DecisionCardManager(paths)
    first = ReviewAction(
        kind=ArtifactKind.DECISION_CARD,
        tool_name="decision_card_manage",
        payload={
            "id": "verify-payment-status",
            "title": "Verify payment status",
            "scenario": "A payment remains pending.",
            "signals": ["pending status"],
            "principle": "Verify before retrying.",
            "outcome": "Avoided duplicate payment.",
            "boundaries": "Wait for official status.",
            "confidence": 0.9,
        },
        source_event_id="evt-card-v1",
    )
    manager.apply(first)
    card = paths.decision_cards_dir / "verify-payment-status.md"
    first_content = card.read_text(encoding="utf-8")
    created_line = next(
        line for line in first_content.splitlines() if line.startswith("created_at:")
    )

    second = ReviewAction(
        kind=ArtifactKind.DECISION_CARD,
        tool_name="decision_card_manage",
        payload={
            **first.payload,
            "principle": "Verify final status and no debit before retrying.",
            "confidence": 0.95,
        },
        source_event_id="evt-card-v2",
    )
    manager.apply(second)

    updated = card.read_text(encoding="utf-8")
    assert "version: 2" in updated
    assert created_line in updated
    assert "evt-card-v1" in updated
    assert "evt-card-v2" in updated
    assert "- v1 |" in updated
    assert "- v2 |" in updated
    assert "Verify final status and no debit before retrying." in updated


def test_managers_preserve_all_batch_evidence_ids(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    action = ReviewAction(
        kind=ArtifactKind.USER_PROFILE,
        tool_name="memory_manage",
        payload={"action": "add", "content": "Prefers concise answers."},
        source_event_id="evt-1",
        source_event_ids=("evt-1", "evt-2"),
    )

    MemoryManager(paths).apply(action)

    profile = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert "dream-sources: evt-1, evt-2" in profile


def test_memory_mutation_can_restore_the_previous_file(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    store = AtomicArtifactStore(paths.agent_root)
    store.write_text(Path("users/alice/USER.md"), "Original profile.\n")
    manager = MemoryManager(paths)
    manager.apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={"action": "add", "content": "Prefers concise answers."},
            source_event_id="evt-3",
        )
    )
    RollbackService(paths).restore(manager.last_snapshot_id)
    assert (paths.user_root / "USER.md").read_text(
        encoding="utf-8"
    ) == "Original profile.\n"


def test_memory_replace_accepts_curator_plural_evidence_comment(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    AtomicArtifactStore(paths.agent_root).write_text(
        Path("users/alice/USER.md"),
        "Prefers detailed steps.\n<!-- dream-sources: evt-1, evt-2 -->\n",
    )
    manager = MemoryManager(paths)

    manager.apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={
                "action": "replace",
                "memory_id": _memory_id("Prefers detailed steps."),
                "old_content": "Prefers detailed steps.",
                "content": "Prefers short checklists.",
            },
            source_event_id="evt-9",
        )
    )

    profile = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert "Prefers short checklists." in profile
    assert "Prefers detailed steps." not in profile
    assert "evt-1" in profile
    assert "evt-2" in profile
    assert "evt-9" in profile


def test_memory_replace_rejects_a_memory_id_that_does_not_identify_the_item(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    AtomicArtifactStore(paths.agent_root).write_text(
        Path("users/alice/USER.md"),
        "memory A\n<!-- dream-source: evt-1 -->\n"
        "§\n"
        "memory B\n<!-- dream-source: evt-2 -->\n",
    )

    with pytest.raises(ValueError, match="INVALID_REPLACE_TARGET"):
        MemoryManager(paths).apply(
            ReviewAction(
                kind=ArtifactKind.USER_PROFILE,
                tool_name="memory_manage",
                payload={
                    "action": "replace",
                    "memory_id": _memory_id("memory B"),
                    "old_content": "memory A",
                    "content": "memory A updated",
                },
                source_event_id="evt-3",
            )
        )


def test_memory_replace_rejects_a_whole_multi_item_document(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    whole_profile = "memory A\n§\nmemory B"
    AtomicArtifactStore(paths.agent_root).write_text(
        Path("users/alice/USER.md"),
        "memory A\n<!-- dream-source: evt-1 -->\n"
        "§\n"
        "memory B\n<!-- dream-source: evt-2 -->\n",
    )

    with pytest.raises(ValueError, match="INVALID_REPLACE_TARGET"):
        MemoryManager(paths).apply(
            ReviewAction(
                kind=ArtifactKind.USER_PROFILE,
                tool_name="memory_manage",
                payload={
                    "action": "replace",
                    "memory_id": _memory_id(whole_profile),
                    "old_content": whole_profile,
                    "content": "memory A updated\n§\nmemory B",
                },
                source_event_id="evt-3",
            )
        )


def test_incremental_memory_update_replaces_one_item_without_duplication(
    tmp_path: Path,
) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    AtomicArtifactStore(paths.agent_root).write_text(
        Path("users/alice/USER.md"),
        "Prefers concise responses.\n<!-- dream-source: evt-1 -->\n"
        "§\n"
        "Requires verified bank channels.\n<!-- dream-source: evt-2 -->\n",
    )

    MemoryManager(paths).apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={
                "action": "replace",
                "memory_id": _memory_id("Prefers concise responses."),
                "old_content": "Prefers concise responses.",
                "content": "Prefers concise responses for urgent financial operations.",
            },
            source_event_id="evt-6",
        )
    )

    profile = (paths.user_root / "USER.md").read_text(encoding="utf-8")
    assert profile.count("Prefers concise responses for urgent financial operations.") == 1
    assert "Prefers concise responses.\n<!--" not in profile
    assert profile.count("Requires verified bank channels.") == 1
    assert profile.count("\n§\n") == 1


def test_report_store_writes_a_disk_verifiable_json_report(tmp_path: Path) -> None:
    paths = resolve_scope(tmp_path, ScopeIds("acme", "assistant", "alice"))
    report_path = DreamReportStore(paths).write(
        {
            "run_id": "run-1",
            "status": "success",
            "source_event_ids": ["evt-1", "evt-2"],
        }
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run_id"] == "run-1"
    assert report["source_event_ids"] == ["evt-1", "evt-2"]
