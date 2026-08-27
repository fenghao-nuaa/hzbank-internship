"""Convert ingested documents into plain, normalized representations."""

import json
from html.parser import HTMLParser

from auditgraph.core.models import SourceDocument


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)


class DocumentParser:
    def parse(self, document: SourceDocument) -> SourceDocument:
        content = document.content
        metadata = dict(document.metadata)
        metadata["parsed_from_content_type"] = document.content_type
        if document.content_type == "text/html":
            parser = _TextHTMLParser()
            parser.feed(content)
            content = "\n".join(parser.parts)
        elif document.content_type == "application/json":
            content = json.dumps(json.loads(content), ensure_ascii=False, sort_keys=True)
        return SourceDocument(
            source_id=document.source_id,
            source_type=document.source_type,
            content=content,
            content_type="text/plain",
            collected_at=document.collected_at,
            metadata=metadata,
        )
