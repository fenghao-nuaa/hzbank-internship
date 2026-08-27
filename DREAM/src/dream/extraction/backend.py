"""Replaceable backend used by the Hermes-style review fork."""

from typing import Protocol

from dream.extraction.models import (
    ArtifactKind,
    ReviewAction,
    ReviewBatchRequest,
    ReviewRequest,
    ReviewResult,
)


class ReviewBackend(Protocol):
    def review(self, request: ReviewRequest) -> ReviewResult: ...


class BatchReviewBackend(Protocol):
    def review_batch(self, request: ReviewBatchRequest) -> ReviewResult: ...


class DeterministicReviewBackend:
    """Small closure-test backend; a real LLM backend keeps this interface."""

    def review(self, request: ReviewRequest) -> ReviewResult:
        transcript = request.transcript_text.casefold()
        actions: list[ReviewAction] = []
        preference_signals = (
            "i prefer concise answers",
            "prefer concise answers",
            "喜欢简洁回答",
            "偏好简洁回答",
        )
        if "memory_manage" in request.allowed_tools and any(
            signal in transcript for signal in preference_signals
        ):
            actions.append(
                ReviewAction(
                    kind=ArtifactKind.USER_PROFILE,
                    tool_name="memory_manage",
                    payload={
                        "action": "add",
                        "target": "user",
                        "content": "Prefers concise answers.",
                        "confidence": 0.8,
                    },
                    source_event_id=request.event_id,
                )
            )
        decision_signals = (
            "verify before risky action",
            "高风险操作前先验证",
            "不可逆操作前先验证",
        )
        if "decision_card_manage" in request.allowed_tools and any(
            signal in transcript for signal in decision_signals
        ):
            actions.append(
                ReviewAction(
                    kind=ArtifactKind.DECISION_CARD,
                    tool_name="decision_card_manage",
                    payload={
                        "id": "verify-before-risky-action",
                        "title": "高风险操作前先验证",
                        "scenario": "用户要求执行难以回滚的操作。",
                        "signals": ["操作不可逆", "关键信息不足"],
                        "principle": "先完成只读验证，再决定是否执行。",
                        "outcome": "降低错误修改的概率。",
                        "boundaries": "低风险且可回滚的操作无需反复确认。",
                        "confidence": 0.82,
                    },
                    source_event_id=request.event_id,
                )
            )
        if not actions:
            return ReviewResult(actions=(), summary="Nothing to save.")
        return ReviewResult(
            actions=tuple(actions),
            summary=f"Captured {len(actions)} priority memory artifact(s).",
        )

    def review_batch(self, request: ReviewBatchRequest) -> ReviewResult:
        actions: list[ReviewAction] = []
        for event in request.events:
            result = self.review(
                ReviewRequest(
                    event_id=event.event_id,
                    transcript_text=event.transcript_text,
                    final_response=event.final_response,
                    allowed_tools=event.allowed_tools,
                    current_user_profile=request.current_user_profile,
                    current_decision_rules=request.current_decision_rules,
                    current_decision_cards=request.current_decision_cards,
                )
            )
            actions.extend(result.actions)
        return ReviewResult(
            actions=tuple(actions),
            summary=f"Captured {len(actions)} priority memory artifact(s).",
        )
