from datetime import datetime, timezone
from pathlib import Path

from dream.curators.ai import AICurator
from dream.curators.user import UserCurator
from dream.core.events import TaskCompletedEvent
from dream.memory.managers.decision_cards import DecisionCardManager
from dream.memory.managers.persona import MemoryManager
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.core.scope import ScopeIds, resolve_scope
from dream.application.service import DreamService


def _event(event_id: str, user_id: str = "alice") -> TaskCompletedEvent:
    return TaskCompletedEvent(
        event_id=event_id,
        task_id=f"task-{event_id}",
        scope=ScopeIds("acme", "assistant", user_id),
        completed_at="2026-07-21T00:00:00+08:00",
        interrupted=False,
        tool_iterations=1,
        transcript=(
            {"role": "user", "content": "I prefer concise answers"},
            {"role": "assistant", "content": "verify before risky action"},
        ),
        final_response="Verified.",
        source_refs=(),
    )


class ExplodingSemanticBackend:
    def curate_ai(self, **kwargs):
        raise AssertionError("deterministic batch curation must not call a model")

    def curate_user(self, profile):
        raise AssertionError("deterministic batch curation must not call a model")


def test_successful_batch_immediately_runs_only_deterministic_curators(
    tmp_path: Path,
) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    service = DreamService(
        tmp_path,
        semantic_curator_backend=ExplodingSemanticBackend(),
    )
    service.ingest_conversation(_event("evt-immediate"))

    runs = service.run_pending(ids)

    rules = (resolve_scope(tmp_path, ids).agent_root / "DECISION_RULES.md").read_text(
        encoding="utf-8"
    )
    assert runs[0]["status"] == "success"
    assert set(runs[0]["curator_runs"]) == {"ai", "user"}
    assert "先完成只读验证，再决定是否执行。" in rules


def test_multiple_user_batches_run_ai_once_and_user_curator_per_user(
    tmp_path: Path, monkeypatch
) -> None:
    ai_runs: list[str] = []
    user_runs: list[str] = []
    original_ai_run = AICurator.run
    original_user_run = UserCurator.run

    def record_ai(curator):
        ai_runs.append(str(curator.paths.agent_root))
        return original_ai_run(curator)

    def record_user(curator):
        user_runs.append(curator.paths.user_root.name)
        return original_user_run(curator)

    monkeypatch.setattr(AICurator, "run", record_ai)
    monkeypatch.setattr(UserCurator, "run", record_user)
    service = DreamService(tmp_path)
    service.ingest_conversation(_event("evt-alice", "alice"))
    service.ingest_conversation(_event("evt-bob", "bob"))

    service.run_pending()

    assert len(ai_runs) == 1
    assert user_runs == ["alice", "bob"]


def _write_uncurated_artifacts(home: Path, ids: ScopeIds, suffix: str = "") -> None:
    paths = resolve_scope(home, ids)
    MemoryManager(paths).apply(
        ReviewAction(
            kind=ArtifactKind.USER_PROFILE,
            tool_name="memory_manage",
            payload={
                "action": "add",
                "content": f"Prefers concise answers{suffix}.",
            },
            source_event_id=f"evt-user{suffix}",
        )
    )
    DecisionCardManager(paths).apply(
        ReviewAction(
            kind=ArtifactKind.DECISION_CARD,
            tool_name="decision_card_manage",
            payload={
                "id": f"verify-risk{suffix}",
                "title": "高风险操作前先验证",
                "scenario": "用户要求执行不可逆操作",
                "signals": ["不可逆"],
                "principle": f"先验证，再执行{suffix}。",
                "outcome": "避免错误修改。",
                "boundaries": "低风险操作除外。",
                "confidence": 0.8,
            },
            source_event_id=f"evt-ai{suffix}",
        )
    )


def test_daily_curator_waits_until_shanghai_0300_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    service = DreamService(tmp_path)
    service.ledger.append(_event("evt-ledger"))
    _write_uncurated_artifacts(tmp_path, ids)

    before = service.run_due_curators(
        datetime(2026, 7, 20, 18, 59, tzinfo=timezone.utc)
    )
    first = service.run_due_curators(datetime(2026, 7, 20, 19, 0, tzinfo=timezone.utc))
    second = service.run_due_curators(datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc))

    assert before == {}
    assert set(first) == {"acme/assistant/alice"}
    assert set(first["acme/assistant/alice"]) == {"ai", "user"}
    assert second == {}


def test_restart_after_missed_daily_time_runs_changed_scope_once(
    tmp_path: Path,
) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    first = DreamService(tmp_path)
    first.ingest_conversation(_event("evt-first"))
    first.run_pending(ids)
    _write_uncurated_artifacts(tmp_path, ids, "-new")

    restarted = DreamService(tmp_path)
    catch_up = restarted.run_due_curators(
        datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
    )
    duplicate = restarted.run_due_curators(
        datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc)
    )

    assert set(catch_up["acme/assistant/alice"]) == {"ai", "user"}
    assert duplicate == {}


def test_daily_check_with_no_artifact_changes_is_a_no_op(tmp_path: Path) -> None:
    ids = ScopeIds("acme", "assistant", "alice")
    service = DreamService(tmp_path)
    service.ingest_conversation(_event("evt-curated"))
    service.run_pending(ids)

    result = service.run_due_curators(datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc))

    assert result == {}
