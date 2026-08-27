"""Deterministic word-boundary splitter with source offsets."""

import hashlib

from auditgraph.core.models import Chunk, SourceDocument


class TextSplitter:
    def __init__(self, *, max_chars: int = 1_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def split(self, document: SourceDocument) -> list[Chunk]:
        text = document.content
        if not text:
            return []
        if "\n" not in text and not text.lstrip().startswith(
            ("ENTITY|", "RELATION|", "EVENT|", "TRIPLE|", "{", "[")
        ):
            return self._split_plain_text(document)
        chunks: list[Chunk] = []
        line_spans = list(self._line_spans(text))
        current_start: int | None = None
        current_end = 0
        for line_start, line_end in line_spans:
            line_content = text[line_start:line_end].strip()
            if not line_content:
                continue
            if current_start is None:
                current_start, current_end = line_start, line_end
                continue
            proposed = line_end - current_start
            if proposed <= self.max_chars:
                current_end = line_end
                continue
            self._append_chunk(chunks, document, current_start, current_end)
            current_start, current_end = line_start, line_end
        if current_start is not None:
            self._append_chunk(chunks, document, current_start, current_end)
        return chunks

    def _split_plain_text(self, document: SourceDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        start = 0
        text = document.content
        while start < len(text):
            end = min(start + self.max_chars, len(text))
            if end < len(text):
                boundary = text.rfind(" ", start, end + 1)
                if boundary > start:
                    end = boundary
            self._append_chunk(chunks, document, start, end)
            start = end
            while start < len(text) and text[start].isspace():
                start += 1
        return chunks

    @staticmethod
    def _line_spans(text: str):
        start = 0
        for line in text.splitlines(keepends=True):
            end = start + len(line)
            yield start, end
            start = end
        if start < len(text):
            yield start, len(text)

    @staticmethod
    def _append_chunk(
        chunks: list[Chunk], document: SourceDocument, start: int, end: int
    ) -> None:
        content = document.content[start:end].strip()
        if not content:
            return
        digest = hashlib.sha256(f"{document.source_id}:{start}:{end}".encode()).hexdigest()[:16]
        chunks.append(
            Chunk(
                chunk_id=f"chunk:{digest}",
                source_id=document.source_id,
                content=content,
                start=start,
                end=end,
                metadata={"parent_source_id": document.source_id},
            )
        )
