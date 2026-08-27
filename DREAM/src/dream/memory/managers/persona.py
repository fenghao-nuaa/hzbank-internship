"""Hermes-style bounded USER.md and MEMORY.md mutations."""

import hashlib
from pathlib import Path
import re

from dream.memory.artifacts import ArtifactVersion, AtomicArtifactStore
from dream.memory.items import (
    ENTRY_DELIMITER,
    InvalidReplaceTarget,
    entry_content,
    resolve_replace_target,
)
from dream.extraction.models import ArtifactKind, ReviewAction
from dream.memory.storage.rollback import RollbackService
from dream.core.scope import ScopePaths


_SOURCE_COMMENT = re.compile(r"\n<!-- dream-sources?:\s*([^>]+) -->\s*$")


def _entry_content(entry: str) -> str:
    return entry_content(entry)


def _entry_sources(entry: str) -> tuple[str, ...]:
    match = _SOURCE_COMMENT.search(entry)
    if match is None:
        return ()
    return tuple(value.strip() for value in match.group(1).split(",") if value.strip())


def _sourced_entry(content: str, sources: tuple[str, ...]) -> str:
    unique_sources = tuple(dict.fromkeys(sources))
    label = "dream-sources" if len(unique_sources) > 1 else "dream-source"
    return f"{content}\n<!-- {label}: {', '.join(unique_sources)} -->"


class MemoryManager:
    def __init__(self, paths: ScopePaths) -> None:
        self.paths = paths
        self.artifacts = AtomicArtifactStore(paths.agent_root)
        self.rollback = RollbackService(paths)
        self.last_snapshot_id = ""

    def _relative_path(self, action: ReviewAction) -> Path:
        if action.kind is ArtifactKind.USER_PROFILE:
            return Path("users") / self.paths.user_root.name / "USER.md"
        if action.kind is ArtifactKind.AGENT_MEMORY:
            return Path("MEMORY.md")
        raise ValueError(f"unsupported memory artifact: {action.kind.value}")

    def apply(self, action: ReviewAction) -> ArtifactVersion:
        if action.tool_name != "memory_manage":
            raise ValueError("memory action must use memory_manage")
        relative = self._relative_path(action)
        existing = self.artifacts.read_text(relative)
        operation = str(action.payload.get("action", "add"))
        content = str(action.payload.get("content", "")).strip()
        if not content or "\x00" in content:
            raise ValueError("memory content must be non-empty text")
        if "§" in content:
            if operation == "replace":
                raise InvalidReplaceTarget(
                    "content must contain exactly one memory item"
                )
            raise ValueError("memory content must contain exactly one atomic item")
        entries = [
            entry.strip() for entry in existing.split(ENTRY_DELIMITER) if entry.strip()
        ]
        sourced_entry = _sourced_entry(content, action.evidence_event_ids)
        if operation == "add":
            if not any(_entry_content(entry) == content for entry in entries):
                entries.append(sourced_entry)
        elif operation == "replace":
            old_content = str(action.payload.get("old_content", "")).strip()
            memory_id = str(action.payload.get("memory_id", "")).strip()
            index, _ = resolve_replace_target(
                existing,
                memory_id=memory_id,
                old_content=old_content,
            )
            previous_sources = _entry_sources(entries[index])
            entries[index] = _sourced_entry(
                content,
                previous_sources + action.evidence_event_ids,
            )
        elif operation == "remove":
            old_content = str(action.payload.get("old_content", content)).strip()
            entries = [
                entry for entry in entries if _entry_content(entry) != old_content
            ]
        else:
            raise ValueError(f"unsupported memory action: {operation}")
        rendered = ENTRY_DELIMITER.join(entries)
        if rendered:
            rendered += "\n"
        self.last_snapshot_id = self.rollback.capture((relative,))
        if rendered == existing:
            encoded = existing.encode("utf-8")
            return ArtifactVersion(
                sha256=hashlib.sha256(encoded).hexdigest(),
                byte_length=len(encoded),
                updated_at="unchanged",
            )
        return self.artifacts.write_text(relative, rendered)
