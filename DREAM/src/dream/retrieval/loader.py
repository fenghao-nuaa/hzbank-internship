"""Read existing DREAM Markdown artifacts into retrieval records."""

import hashlib
import json
from pathlib import Path
import re

from dream.core.scope import ScopeIds, resolve_scope
from dream.retrieval.config import infer_domain, normalize_domain
from dream.retrieval.models import MemoryKind, MemoryRecord


_ENTRY_DELIMITER = "\n§\n"
_DREAM_COMMENT = re.compile(r"<!--\s*dream-[^>]+-->\s*", re.IGNORECASE)
_PERSONA_ID = re.compile(r"<!--\s*dream-persona-id:\s*([^>]+?)\s*-->", re.IGNORECASE)
_PERSONA_DOMAIN = re.compile(
    r"<!--\s*dream-persona-domain:\s*([^>]+?)\s*-->", re.IGNORECASE
)
_PERSONA_CONFIDENCE = re.compile(
    r"<!--\s*dream-persona-confidence:\s*([^>]+?)\s*-->",
    re.IGNORECASE,
)
_SOURCES = re.compile(r"<!--\s*dream-sources?:\s*([^>]+?)\s*-->", re.IGNORECASE)
_PROJECTION_ITEM = re.compile(r"^-\s+(?:\[([^\]]+)\]\s+)?(.+)$")


def _stable_id(prefix: str, content: str) -> str:
    digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _match_text(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match is not None else ""


def _source_ids(text: str) -> tuple[str, ...]:
    value = _match_text(_SOURCES, text)
    if not value:
        return ()
    return tuple(
        dict.fromkeys(item.strip() for item in value.split(",") if item.strip())
    )


def _confidence(value: str, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, parsed))


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return {}, text
    metadata: dict[str, object] = {}
    index = 1
    while index < closing:
        line = lines[index]
        if ":" not in line:
            index += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            try:
                metadata[key] = json.loads(raw_value)
            except json.JSONDecodeError:
                metadata[key] = raw_value
            index += 1
            continue
        values: list[str] = []
        index += 1
        while index < closing and lines[index].startswith("  - "):
            item = lines[index][4:].strip()
            try:
                values.append(str(json.loads(item)))
            except json.JSONDecodeError:
                values.append(item)
            index += 1
        metadata[key] = values
    return metadata, "\n".join(lines[closing + 1 :]).strip()


def _conflict_key(content: str) -> str | None:
    normalized = content.casefold()
    if any(
        value in normalized for value in ("中文", "英文", "chinese", "english")
    ) and any(value in normalized for value in ("回复", "回答", "response", "reply")):
        return "response_language"
    return None


def _persona_segments(content: str) -> tuple[str, ...]:
    """Split legacy entries that contain several independent persona statements."""
    segments: list[list[str]] = []
    current: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        is_detail = line.startswith("- ") or line.casefold() == (
            "additional durable requirements:"
        )
        if not is_detail and current:
            segments.append(current)
            current = []
        current.append(line)
    if current:
        segments.append(current)
    return tuple("\n".join(segment) for segment in segments)


class MemoryLoader:
    """Filesystem-backed, read-only MemorySource for one strict DREAM scope."""

    def __init__(
        self,
        *,
        home: Path,
        tenant_id: str,
        agent_id: str,
        user_id: str,
    ) -> None:
        self.scope = ScopeIds(tenant_id, agent_id, user_id)
        self.paths = resolve_scope(home, self.scope)

    def list_records(self) -> tuple[MemoryRecord, ...]:
        records: list[MemoryRecord] = []
        records.extend(self._load_personas())
        records.extend(self._load_decision_rules())
        records.extend(self._load_decision_cards())
        return tuple(records)

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _load_personas(self) -> tuple[MemoryRecord, ...]:
        repository = self._read(self.paths.user_root / "USER.md")
        projection = self._read(self.paths.user_root / "USER_PERSONA.md")
        records = self._repository_personas(repository)
        if records:
            return records
        return self._projection_personas(projection)

    def _repository_personas(self, markdown: str) -> tuple[MemoryRecord, ...]:
        records: list[MemoryRecord] = []
        for raw_entry in markdown.split(_ENTRY_DELIMITER):
            entry = raw_entry.strip()
            content = _DREAM_COMMENT.sub("", entry).strip()
            if not content:
                continue
            explicit_domain = normalize_domain(_match_text(_PERSONA_DOMAIN, entry))
            explicit_id = _match_text(_PERSONA_ID, entry)
            segments = _persona_segments(content)
            for segment in segments:
                domain = infer_domain(segment) or explicit_domain
                memory_id = (
                    explicit_id
                    if len(segments) == 1 and explicit_id
                    else _stable_id(explicit_id or "persona", segment)
                )
                conflict_key = _conflict_key(segment)
                metadata = (
                    {"conflict_key": conflict_key} if conflict_key is not None else {}
                )
                records.append(
                    MemoryRecord(
                        memory_id=memory_id,
                        kind=MemoryKind.USER_PERSONA,
                        content=segment,
                        tenant_id=self.scope.tenant_id,
                        agent_id=self.scope.agent_id,
                        user_id=self.scope.user_id,
                        confidence=_confidence(
                            _match_text(_PERSONA_CONFIDENCE, entry),
                            0.8,
                        ),
                        source_event_ids=_source_ids(entry),
                        source="USER.md",
                        domain=domain,
                        metadata=metadata,
                    )
                )
        return tuple(records)

    def _projection_personas(self, markdown: str) -> tuple[MemoryRecord, ...]:
        records: list[MemoryRecord] = []
        for line in markdown.splitlines():
            match = _PROJECTION_ITEM.match(line.strip())
            if match is None:
                continue
            domain = normalize_domain(match.group(1))
            content = match.group(2).strip()
            if not content:
                continue
            records.append(
                MemoryRecord(
                    memory_id=_stable_id("persona-projection", content),
                    kind=MemoryKind.USER_PERSONA,
                    content=content,
                    tenant_id=self.scope.tenant_id,
                    agent_id=self.scope.agent_id,
                    user_id=self.scope.user_id,
                    confidence=0.7,
                    source="USER_PERSONA.md",
                    domain=domain or infer_domain(content),
                )
            )
        return tuple(records)

    def _load_decision_rules(self) -> tuple[MemoryRecord, ...]:
        markdown = self._read(self.paths.agent_root / "DECISION_RULES.md")
        records: list[MemoryRecord] = []
        for line in markdown.splitlines():
            if not line.startswith("- "):
                continue
            content = line[2:].strip()
            if not content:
                continue
            records.append(
                MemoryRecord(
                    memory_id=_stable_id("decision-rule", content),
                    kind=MemoryKind.DECISION_RULE,
                    content=content,
                    tenant_id=self.scope.tenant_id,
                    agent_id=self.scope.agent_id,
                    user_id=None,
                    confidence=0.85,
                    source="DECISION_RULES.md",
                    domain=infer_domain(content),
                )
            )
        return tuple(records)

    def _load_decision_cards(self) -> tuple[MemoryRecord, ...]:
        if not self.paths.decision_cards_dir.exists():
            return ()
        records: list[MemoryRecord] = []
        for path in sorted(self.paths.decision_cards_dir.glob("*.md")):
            raw = self._read(path)
            metadata, content = _frontmatter(raw)
            if not content:
                continue
            card_id = str(metadata.get("id", "")).strip() or path.stem
            raw_sources = metadata.get("source_event_ids", ())
            source_ids = (
                tuple(str(item) for item in raw_sources)
                if isinstance(raw_sources, list)
                else ()
            )
            updated_at = str(metadata.get("updated_at", "")).strip()
            records.append(
                MemoryRecord(
                    memory_id=card_id,
                    kind=MemoryKind.DECISION_CARD,
                    content=content,
                    tenant_id=self.scope.tenant_id,
                    agent_id=self.scope.agent_id,
                    user_id=None,
                    confidence=_confidence(metadata.get("confidence", ""), 0.8),
                    source_event_ids=source_ids,
                    source=f"decision-cards/{path.name}",
                    domain=infer_domain(content),
                    updated_at=updated_at,
                )
            )
        return tuple(records)
