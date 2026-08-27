"""Atomic persistence for reusable DREAM skill artifacts."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from dream.memory.artifacts import ArtifactVersion, AtomicArtifactStore
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.memory.storage.rollback import RollbackService
from dream.core.scope import ScopePaths


_SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SOURCE_BLOCK = re.compile(r"source_event_ids:\n((?:\s+-\s+.+\n)*)")


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


class SkillManager:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.rollback = RollbackService(paths)
        self.last_snapshot_id = ""

    def apply(self, action: ReviewAction) -> ArtifactVersion:
        if action.kind is not ArtifactKind.SKILL:
            raise ValueError("skill manager only accepts skill artifacts")
        if action.tool_name != "skill_manage":
            raise ValueError("skill action must use skill_manage")
        skill_id = str(action.payload.get("id", "")).strip()
        if not _SKILL_ID.fullmatch(skill_id):
            raise ValueError("invalid skill id")
        title = self._text(action, "title")
        scenario = self._text(action, "scenario")
        output_template = self._text(action, "output_template")
        cautions = self._text(action, "cautions")
        inputs = self._string_list(action, "inputs")
        steps = self._string_list(action, "steps")
        confidence = float(action.payload.get("confidence", 0))
        if not 0 <= confidence <= 1:
            raise ValueError("skill confidence must be between zero and one")
        relative = Path("skills") / f"{skill_id}.skill"
        existing = self.artifacts.read_text(relative)
        sources = tuple(
            dict.fromkeys(_existing_sources(existing) + action.evidence_event_ids)
        )
        source_lines = "".join(f"  - {json.dumps(event_id)}\n" for event_id in sources)
        input_lines = "\n".join(f"- {value}" for value in inputs)
        step_lines = "\n".join(
            f"{index}. {value}" for index, value in enumerate(steps, start=1)
        )
        now = datetime.now(timezone.utc).isoformat()
        rendered = (
            "---\n"
            f"id: {json.dumps(skill_id)}\n"
            "status: active\n"
            f"confidence: {confidence:.2f}\n"
            f"updated_at: {json.dumps(now)}\n"
            "source_event_ids:\n"
            f"{source_lines}"
            "---\n\n"
            f"# {title}\n\n"
            f"## 使用场景\n\n{scenario}\n\n"
            f"## 输入\n\n{input_lines}\n\n"
            f"## 操作步骤\n\n{step_lines}\n\n"
            f"## 输出模板\n\n{output_template}\n\n"
            f"## 注意事项\n\n{cautions}\n"
        )
        self.last_snapshot_id = self.rollback.capture((relative,))
        return self.artifacts.write_text(relative, rendered)

    @staticmethod
    def _text(action: ReviewAction, name: str) -> str:
        value = action.payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"skill {name} must be non-empty text")
        return value.strip()

    @staticmethod
    def _string_list(action: ReviewAction, name: str) -> list[str]:
        value = action.payload.get(name)
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise ValueError(f"skill {name} must be a non-empty string list")
        return [item.strip() for item in value]
