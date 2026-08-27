"""Human-readable AI decision card persistence."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from dream.memory.artifacts import ArtifactVersion, AtomicArtifactStore
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.memory.storage.rollback import RollbackService
from dream.core.scope import ScopePaths


_CARD_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SOURCE_BLOCK = re.compile(r"source_event_ids:\n((?:\s+-\s+.+\n)*)")
_VERSION = re.compile(r"^version:\s*(\d+)\s*$", re.MULTILINE)
_CREATED_AT = re.compile(r"^created_at:\s*(.+)\s*$", re.MULTILINE)
_UPDATED_AT = re.compile(r"^updated_at:\s*(.+)\s*$", re.MULTILINE)
_HISTORY = re.compile(r"## 更新历史\s*\n\s*(.+)\Z", re.DOTALL)


def _existing_sources(content: str) -> tuple[str, ...]:
    match = _SOURCE_BLOCK.search(content)
    if match is None:
        return ()
    values: list[str] = []
    for line in match.group(1).splitlines():
        raw = line.split("-", 1)[1].strip()
        try:
            value = str(json.loads(raw))
        except json.JSONDecodeError:
            value = raw.strip("\"'")
        if value and value not in values:
            values.append(value)
    return tuple(values)


class DecisionCardManager:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.rollback = RollbackService(paths)
        self.last_snapshot_id = ""

    def apply(self, action: ReviewAction) -> ArtifactVersion:
        if action.kind is not ArtifactKind.DECISION_CARD:
            raise ValueError("decision card manager only accepts decision cards")
        if action.tool_name != "decision_card_manage":
            raise ValueError("decision card action must use decision_card_manage")
        card_id = str(action.payload.get("id", ""))
        if not _CARD_ID.fullmatch(card_id):
            raise ValueError("invalid decision card id")
        relative = Path("decision-cards") / f"{card_id}.md"
        existing = self.artifacts.read_text(relative)
        self.last_snapshot_id = self.rollback.capture((relative,))
        now = datetime.now(timezone.utc).isoformat()
        title = str(action.payload.get("title", "")).strip()
        scenario = str(action.payload.get("scenario", "")).strip()
        principle = str(action.payload.get("principle", "")).strip()
        outcome = str(action.payload.get("outcome", "")).strip()
        boundaries = str(action.payload.get("boundaries", "")).strip()
        signals = action.payload.get("signals", [])
        confidence = float(action.payload.get("confidence", 0.5))
        if not all((title, scenario, principle, outcome, boundaries)):
            raise ValueError("decision card fields must be non-empty")
        if not isinstance(signals, list) or not all(
            isinstance(item, str) for item in signals
        ):
            raise ValueError("decision card signals must be a list of strings")
        signal_lines = "\n".join(f"- {item}" for item in signals) or "- 无"
        source_ids = tuple(
            dict.fromkeys(_existing_sources(existing) + action.evidence_event_ids)
        )
        source_lines = "".join(
            f"  - {json.dumps(event_id)}\n" for event_id in source_ids
        )
        version, created_at, history = self._revision_metadata(
            existing,
            now=now,
            previous_sources=_existing_sources(existing),
            current_sources=action.evidence_event_ids,
        )
        rendered = (
            "---\n"
            f"id: {json.dumps(card_id, ensure_ascii=False)}\n"
            "status: active\n"
            f"version: {version}\n"
            f"confidence: {confidence:.2f}\n"
            f"created_at: {created_at}\n"
            f"updated_at: {json.dumps(now)}\n"
            "source_event_ids:\n"
            f"{source_lines}"
            "---\n\n"
            f"# {title}\n\n"
            f"## 使用场景\n\n{scenario}\n\n"
            f"## 决策信号\n\n{signal_lines}\n\n"
            f"## 决策原则\n\n{principle}\n\n"
            f"## 本次结果\n\n{outcome}\n\n"
            f"## 反例与边界\n\n{boundaries}\n\n"
            f"## 更新历史\n\n{history}\n"
        )
        return self.artifacts.write_text(relative, rendered)

    @staticmethod
    def _revision_metadata(
        existing: str,
        *,
        now: str,
        previous_sources: tuple[str, ...],
        current_sources: tuple[str, ...],
    ) -> tuple[int, str, str]:
        if not existing:
            version = 1
            created_at = json.dumps(now)
            history_lines: list[str] = []
        else:
            version_match = _VERSION.search(existing)
            previous_version = int(version_match.group(1)) if version_match else 1
            version = previous_version + 1
            created_match = _CREATED_AT.search(existing)
            created_at = (
                created_match.group(1).strip()
                if created_match is not None
                else json.dumps(now)
            )
            history_match = _HISTORY.search(existing)
            history_lines = (
                [
                    line.strip()
                    for line in history_match.group(1).splitlines()
                    if line.strip().startswith("- v")
                ]
                if history_match is not None
                else []
            )
            if not history_lines:
                updated_match = _UPDATED_AT.search(existing)
                previous_at = (
                    updated_match.group(1).strip()
                    if updated_match is not None
                    else created_at
                )
                history_lines.append(
                    DecisionCardManager._history_line(
                        previous_version,
                        previous_at,
                        previous_sources,
                    )
                )
        history_lines.append(
            DecisionCardManager._history_line(
                version,
                json.dumps(now),
                current_sources,
            )
        )
        return version, created_at, "\n".join(history_lines)

    @staticmethod
    def _history_line(
        version: int,
        timestamp: str,
        sources: tuple[str, ...],
    ) -> str:
        evidence = ", ".join(sources) if sources else "none"
        return f"- v{version} | {timestamp} | sources: {evidence}"
