"""Deterministic risk policy for automatic memory activation."""

from dataclasses import dataclass, field
from enum import StrEnum
import re

from dream.extraction.models import ArtifactKind


class GovernanceMode(StrEnum):
    AUTO_ACTIVATE = "auto_activate"
    OBSERVE = "observe"
    REQUIRE_REVIEW = "require_review"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class GovernanceArtifact:
    artifact_type: ArtifactKind
    source_event_ids: tuple[str, ...]
    confidence: float
    content: str
    attributes: dict[str, object] = field(default_factory=dict)
    declared_risk: RiskLevel | None = None

    @classmethod
    def from_review_action(cls, action) -> "GovernanceArtifact":
        payload = dict(action.payload)
        raw_confidence = payload.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(
            raw_confidence, (int, float)
        ):
            confidence = 0.0
        else:
            confidence = float(raw_confidence)
        if action.kind is ArtifactKind.USER_PROFILE:
            content = str(payload.get("content", "")).strip()
        else:
            text_values: list[str] = []
            for value in payload.values():
                if isinstance(value, str) and value.strip():
                    text_values.append(value.strip())
                elif isinstance(value, list):
                    text_values.extend(
                        entry.strip()
                        for entry in value
                        if isinstance(entry, str) and entry.strip()
                    )
            content = "\n".join(text_values)
        return cls(
            artifact_type=action.kind,
            source_event_ids=action.evidence_event_ids,
            confidence=confidence,
            content=content,
            attributes=payload,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type.value,
            "source_event_ids": list(self.source_event_ids),
            "confidence": self.confidence,
            "content": self.content,
            "attributes": self.attributes,
            "declared_risk": (
                self.declared_risk.value if self.declared_risk is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "GovernanceArtifact":
        raw_sources = value.get("source_event_ids", [])
        raw_attributes = value.get("attributes", {})
        raw_risk = value.get("declared_risk")
        if not isinstance(raw_sources, list) or not all(
            isinstance(item, str) for item in raw_sources
        ):
            raise ValueError("governance artifact sources must be a string list")
        if not isinstance(raw_attributes, dict):
            raise ValueError("governance artifact attributes must be an object")
        return cls(
            artifact_type=ArtifactKind(str(value["artifact_type"])),
            source_event_ids=tuple(raw_sources),
            confidence=float(value["confidence"]),
            content=str(value.get("content", "")),
            attributes=dict(raw_attributes),
            declared_risk=RiskLevel(str(raw_risk)) if raw_risk is not None else None,
        )


@dataclass(frozen=True)
class AutoWritebackDecision:
    mode: GovernanceMode
    risk_level: RiskLevel
    reason: str


_HIGH_RISK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:my|user(?:'s)?)\s+(?:password|verification code|full card number|identity number)\b",
        r"\b(?:my|user(?:'s)?)\s+(?:passport|identity|social security|tax)(?:\s+(?:number|id))?\s*(?:is|:|=)",
        r"(?:我的|用户的)(?:密码|验证码|完整卡号|身份证号)",
        r"(?:我的|用户的)(?:护照号|身份证号|社保号|税号)\s*(?:是|为|：|:)",
        r"(?<!never )(?<!not )(?<!don't )\b(?:skip|bypass)\b.{0,24}\b(?:approval|confirmation|verification)\b",
        r"\b(?:do not|does not|don't|doesn't) need\b.{0,20}\b(?:approval|confirmation|verification)\b",
        r"\b(?:allow|permit).{0,24}\b(?:payment|transfer)s?\b.{0,24}\bwithout (?:approval|confirmation|verification)\b",
        r"(?:转账|付款).{0,16}(?:不用|无需|跳过).{0,12}(?:确认|审批|验证)",
        r"(?:绕过|跳过).{0,16}(?:验证|审批|权限|安全)",
    )
)


class MemoryGovernancePolicy:
    """Classify canonical review artifacts without invoking another model."""

    def decide(self, artifact: GovernanceArtifact) -> AutoWritebackDecision:
        if artifact.declared_risk is RiskLevel.HIGH or self._is_high_risk(
            artifact.content
        ):
            return AutoWritebackDecision(
                mode=GovernanceMode.REQUIRE_REVIEW,
                risk_level=RiskLevel.HIGH,
                reason="sensitive, permission, or high-risk instruction",
            )
        if not artifact.source_event_ids:
            return self._observe("artifact lacks source evidence")
        if artifact.artifact_type is ArtifactKind.USER_PROFILE:
            if artifact.confidence < 0.7:
                return self._observe("user preference needs more evidence")
            return AutoWritebackDecision(
                mode=GovernanceMode.AUTO_ACTIVATE,
                risk_level=RiskLevel.LOW,
                reason="stable user preference",
            )
        if artifact.artifact_type is ArtifactKind.DECISION_CARD:
            if artifact.confidence < 0.8:
                return self._observe("decision experience needs more evidence")
            required = {"scenario", "signals", "principle", "boundaries"}
            if not self._has_required_attributes(artifact, required):
                return self._observe("decision card is incomplete")
            return AutoWritebackDecision(
                mode=GovernanceMode.AUTO_ACTIVATE,
                risk_level=RiskLevel.LOW,
                reason="complete reusable decision experience",
            )
        if artifact.artifact_type is ArtifactKind.SKILL:
            if artifact.confidence < 0.8:
                return self._observe("skill needs more evidence")
            required = {
                "scenario",
                "inputs",
                "steps",
                "output_template",
                "cautions",
            }
            if not self._has_required_attributes(artifact, required):
                return self._observe("skill is incomplete")
            return AutoWritebackDecision(
                mode=GovernanceMode.AUTO_ACTIVATE,
                risk_level=RiskLevel.LOW,
                reason="complete reusable skill",
            )
        return AutoWritebackDecision(
            mode=GovernanceMode.REQUIRE_REVIEW,
            risk_level=RiskLevel.HIGH,
            reason="artifact type is not eligible for automatic activation",
        )

    def decide_all(
        self,
        artifacts: tuple[GovernanceArtifact, ...],
    ) -> AutoWritebackDecision:
        if not artifacts:
            return AutoWritebackDecision(
                mode=GovernanceMode.AUTO_ACTIVATE,
                risk_level=RiskLevel.LOW,
                reason="no artifact changes require review",
            )
        decisions = tuple(self.decide(artifact) for artifact in artifacts)
        for mode in (GovernanceMode.REQUIRE_REVIEW, GovernanceMode.OBSERVE):
            selected = next(
                (decision for decision in decisions if decision.mode is mode),
                None,
            )
            if selected is not None:
                return selected
        return AutoWritebackDecision(
            mode=GovernanceMode.AUTO_ACTIVATE,
            risk_level=RiskLevel.LOW,
            reason="all artifacts satisfy automatic activation policy",
        )

    @staticmethod
    def _observe(reason: str) -> AutoWritebackDecision:
        return AutoWritebackDecision(
            mode=GovernanceMode.OBSERVE,
            risk_level=RiskLevel.MEDIUM,
            reason=reason,
        )

    @staticmethod
    def _has_required_attributes(
        artifact: GovernanceArtifact,
        required: set[str],
    ) -> bool:
        for name in required:
            value = artifact.attributes.get(name)
            if value is None or value == "" or value == [] or value == ():
                return False
        return True

    @staticmethod
    def _is_high_risk(content: str) -> bool:
        return any(pattern.search(content) for pattern in _HIGH_RISK_PATTERNS)
