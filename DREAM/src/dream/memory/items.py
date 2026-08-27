"""Atomic USER.md memory-item parsing and content-addressed identities."""

from dataclasses import dataclass
import hashlib
import re


ENTRY_DELIMITER = "\n§\n"
MEMORY_ID_PATTERN = r"^mem-[a-f0-9]{64}$"
_MEMORY_ID = re.compile(MEMORY_ID_PATTERN)
_SOURCE_COMMENT = re.compile(r"\n<!-- dream-sources?:\s*([^>]+) -->\s*$")


class InvalidReplaceTarget(ValueError):
    """A replace action does not identify one current atomic memory item."""

    code = "INVALID_REPLACE_TARGET"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class AtomicMemoryItem:
    memory_id: str
    content: str
    raw_entry: str


def memory_id_for(content: str) -> str:
    canonical = content.strip()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"mem-{digest}"


def entry_content(entry: str) -> str:
    return _SOURCE_COMMENT.sub("", entry).strip()


def parse_memory_items(markdown: str) -> tuple[AtomicMemoryItem, ...]:
    items: list[AtomicMemoryItem] = []
    for raw in markdown.split(ENTRY_DELIMITER):
        raw_entry = raw.strip()
        if not raw_entry:
            continue
        content = entry_content(raw_entry)
        items.append(
            AtomicMemoryItem(
                memory_id=memory_id_for(content),
                content=content,
                raw_entry=raw_entry,
            )
        )
    return tuple(items)


def require_atomic_text(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidReplaceTarget(f"{field} must be non-empty")
    if "§" in normalized:
        raise InvalidReplaceTarget(f"{field} must contain exactly one memory item")
    return normalized


def resolve_replace_target(
    markdown: str,
    *,
    memory_id: str,
    old_content: str,
) -> tuple[int, AtomicMemoryItem]:
    normalized_id = memory_id.strip()
    normalized_old = require_atomic_text(old_content, field="old_content")
    if not _MEMORY_ID.fullmatch(normalized_id):
        raise InvalidReplaceTarget("memory_id is missing or malformed")
    for index, item in enumerate(parse_memory_items(markdown)):
        if item.memory_id == normalized_id and item.content == normalized_old:
            return index, item
    raise InvalidReplaceTarget(
        "memory_id and old_content must identify the same current memory item"
    )
