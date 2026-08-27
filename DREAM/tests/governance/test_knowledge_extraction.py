import json

import pytest

from dream.governance.knowledge import KnowledgeType
from dream.governance.canonicalizer import (
    InvalidKnowledgeProposal,
    KnowledgeAdapter,
)
from dream.governance.router import KnowledgeRouter
from dream.extraction.models import ArtifactKind


EVENT_IDS = ("evt-formal-006", "evt-formal-007", "evt-formal-010")


def test_adapter_normalizes_markdown_knowledge_proposal() -> None:
    raw = (
        "```json\n"
        + json.dumps(
            {
                "knowledge_candidates": [
                    {
                        "type": "user_preference",
                        "content": "用户喜欢先给结论",
                        "confidence": 0.9,
                        "source_event_ids": "evt-formal-006",
                    }
                ]
            },
            ensure_ascii=False,
        )
        + "\n```"
    )

    proposal = KnowledgeAdapter().adapt(raw, event_ids=EVENT_IDS)

    assert len(proposal.candidates) == 1
    candidate = proposal.candidates[0]
    assert candidate.knowledge_type is KnowledgeType.USER_PREFERENCE
    assert candidate.source_event_ids == ("evt-formal-006",)


def test_adapter_rejects_unknown_or_out_of_batch_knowledge() -> None:
    with pytest.raises(InvalidKnowledgeProposal):
        KnowledgeAdapter().adapt(
            {
                "knowledge_candidates": [
                    {
                        "type": "unknown",
                        "content": "value",
                        "confidence": 0.9,
                        "source_event_ids": ["evt-other"],
                    }
                ]
            },
            event_ids=EVENT_IDS,
        )


def test_router_routes_persona_and_decision_but_defers_workflow_skill() -> None:
    proposal = KnowledgeAdapter().adapt(
        {
            "knowledge_candidates": [
                {
                    "type": "user_preference",
                    "content": "用户喜欢区分立即处理和等待处理",
                    "confidence": 0.9,
                    "source_event_ids": ["evt-formal-006"],
                },
                {
                    "type": "decision_rule",
                    "id": "supplier-account-verification",
                    "content": "供应商账户变更必须独立核验",
                    "confidence": 0.95,
                    "source_event_ids": ["evt-formal-007"],
                },
                {
                    "type": "workflow_skill",
                    "id": "bank-report-writing",
                    "name": "银行风险报告写作",
                    "content": "银行风险报告按照背景、风险、措施结构生成",
                    "trigger": "需要生成银行风险报告",
                    "steps": ["整理背景", "识别风险", "给出处置措施"],
                    "constraints": ["只使用已核实信息"],
                    "confidence": 0.9,
                    "source_event_ids": ["evt-formal-010"],
                },
            ]
        },
        event_ids=EVENT_IDS,
    )

    actions = KnowledgeRouter().route(proposal)

    assert [action.kind for action in actions] == [
        ArtifactKind.USER_PROFILE,
        ArtifactKind.DECISION_CARD,
    ]
    assert "用户喜欢区分立即处理和等待处理" in actions[0].payload["content"]
    assert "dream-persona-domain:" in actions[0].payload["content"]
    assert actions[1].payload["id"] == "supplier-account-verification"
    assert actions[1].payload["principle"] == "供应商账户变更必须独立核验"
    assert proposal.candidates[2].knowledge_type is KnowledgeType.WORKFLOW_SKILL
    assert proposal.candidates[2].knowledge_id == "bank-report-writing"


def test_router_replaces_and_losslessly_merges_a_related_atomic_persona() -> None:
    old_preference = (
        "User prefers concise responses for urgent operational issues: start with "
        "a one-sentence conclusion and actionable steps."
    )
    unrelated = "User requires de-identified data for external sharing."
    existing = (
        f"{old_preference}\n<!-- dream-source: evt-old -->\n"
        "§\n"
        f"{unrelated}\n<!-- dream-source: evt-security -->\n"
    )
    new_preference = (
        "The user requires risk-tiered responses: keep low-risk answers concise, "
        "but explain reasons, stop conditions, and continue conditions for high-risk "
        "financial or compliance issues."
    )
    proposal = KnowledgeAdapter().adapt(
        {
            "knowledge_candidates": [
                {
                    "type": "user_preference",
                    "id": "risk-tiered-response-structure",
                    "content": new_preference,
                    "confidence": 0.95,
                    "source_event_ids": ["evt-formal-021"],
                }
            ]
        },
        event_ids=("evt-formal-021",),
        existing_memory=existing,
    )

    actions = KnowledgeRouter().route(proposal, existing_memory=existing)

    assert len(actions) == 1
    action = actions[0]
    assert action.payload["action"] == "replace"
    assert action.payload["old_content"] == old_preference
    assert str(action.payload["memory_id"]).startswith("mem-")
    assert old_preference in str(action.payload["content"])
    assert new_preference in str(action.payload["content"])
    assert unrelated not in str(action.payload["content"])


def test_router_adds_an_unrelated_persona_as_a_new_atomic_item() -> None:
    proposal = KnowledgeAdapter().adapt(
        {
            "knowledge_candidates": [
                {
                    "type": "user_preference",
                    "content": "User prefers dark mode for visual dashboards.",
                    "confidence": 0.9,
                    "source_event_ids": ["evt-new"],
                }
            ]
        },
        event_ids=("evt-new",),
    )

    actions = KnowledgeRouter().route(
        proposal,
        existing_memory=(
            "User prefers concise financial answers.\n"
            "<!-- dream-source: evt-old -->\n"
        ),
    )

    assert len(actions) == 1
    assert actions[0].payload["action"] == "add"


def test_router_does_not_write_an_exact_duplicate_persona() -> None:
    content = "User prefers concise financial answers."
    proposal = KnowledgeAdapter().adapt(
        {
            "knowledge_candidates": [
                {
                    "type": "user_preference",
                    "content": content,
                    "confidence": 0.9,
                    "source_event_ids": ["evt-repeat"],
                }
            ]
        },
        event_ids=("evt-repeat",),
    )

    actions = KnowledgeRouter().route(
        proposal,
        existing_memory=f"{content}\n<!-- dream-source: evt-old -->\n",
    )

    assert actions == ()
