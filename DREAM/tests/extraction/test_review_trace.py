import json
from pathlib import Path

from dream.core.events import TaskCompletedEvent
from dream.extraction.provider_adapter import ReviewAdapter, ReviewAdapterContext
from dream.extraction.models import ReviewResult
from dream.core.scope import ScopeIds, resolve_scope
from dream.application.service import DreamService


IDS = ScopeIds("bank-lab", "assistant", "user-1")


class TraceBackend:
    def review_batch(self, request) -> ReviewResult:
        return ReviewResult(
            actions=(),
            summary="Nothing durable.",
            trace={
                "raw_llm_output": {"knowledge_candidates": []},
                "adapter_output": {"knowledge_candidates": []},
                "canonical_review": {"actions": []},
            },
        )


def test_service_persists_review_trace_separately_from_run_report(
    tmp_path: Path,
) -> None:
    service = DreamService(tmp_path, backend=TraceBackend())
    service.ingest_conversation(
        TaskCompletedEvent(
            event_id="evt-trace",
            task_id="task-trace",
            scope=IDS,
            completed_at="2026-07-22T10:00:00+08:00",
            interrupted=False,
            tool_iterations=1,
            transcript=({"role": "user", "content": "ordinary request"},),
            final_response="done",
            source_refs=(),
        )
    )

    runs = service.run_pending(IDS)

    trace_path = (
        resolve_scope(tmp_path, IDS).agent_root
        / "dream-reports"
        / "review-traces"
        / f"{runs[0]['run_id']}.json"
    )
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["source_event_ids"] == ["evt-trace"]
    assert trace["raw_llm_output"] == {"knowledge_candidates": []}
    assert trace["canonical_review"] == {"actions": []}


class PendingSkillTraceBackend:
    def review_batch(self, request) -> ReviewResult:
        event_ids = tuple(event.event_id for event in request.events)
        canonical = ReviewAdapter().adapt(
            {
                "knowledge_candidates": {
                    "user_persona": [],
                    "decision_rules": [],
                    "skills": [
                        {
                            "id": "risk-report-writing",
                            "name": "Risk Report Writing",
                            "content": "Produce an evidence-backed risk report.",
                            "trigger": "A risk report is requested.",
                            "inputs": ["verified incidents"],
                            "steps": ["Summarize", "Classify", "Assign actions"],
                            "output_template": "Conclusion, risks, actions",
                            "constraints": ["Label uncertain data."],
                            "confidence": 0.91,
                            "source_event_ids": [event_ids[0]],
                        }
                    ],
                },
                "event_dispositions": [
                    {
                        "event_id": event_ids[0],
                        "disposition": "used",
                        "reason": None,
                    }
                ],
                "nothing_to_save_reason": None,
            },
            ReviewAdapterContext(
                event_ids=event_ids,
                existing_memory=request.current_user_profile,
                existing_decision_cards=request.current_decision_cards,
                allowed_tools_by_event={
                    event.event_id: event.allowed_tools for event in request.events
                },
            ),
        )
        return ReviewResult(
            actions=canonical.actions,
            summary="Recorded one pending skill candidate.",
            event_dispositions=canonical.event_dispositions,
            trace={
                "knowledge_proposal": canonical.knowledge_proposal,
                "adapter_diagnostics": canonical.adapter_diagnostics,
                "canonical_review": {"actions": []},
            },
        )


def test_skill_candidate_is_persisted_only_in_trace(tmp_path: Path) -> None:
    service = DreamService(tmp_path, backend=PendingSkillTraceBackend())
    service.ingest_conversation(
        TaskCompletedEvent(
            event_id="evt-pending-skill",
            task_id="task-pending-skill",
            scope=IDS,
            completed_at="2026-07-22T10:00:00+08:00",
            interrupted=False,
            tool_iterations=1,
            transcript=(
                {"role": "user", "content": "请形成一套风险报告写作流程。"},
            ),
            final_response="done",
            source_refs=(),
        )
    )

    runs = service.run_pending(IDS)

    paths = resolve_scope(tmp_path, IDS)
    trace_path = (
        paths.agent_root
        / "dream-reports"
        / "review-traces"
        / f"{runs[0]['run_id']}.json"
    )
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    candidate = trace["knowledge_proposal"]["knowledge_candidates"][0]
    assert candidate["type"] == "workflow_skill"
    assert candidate["status"] == "pending_skill_implementation"
    assert trace["canonical_review"]["actions"] == []
    assert not paths.skills_dir.exists()
