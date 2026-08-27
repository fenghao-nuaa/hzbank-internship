"""Local file ingestion."""

from pathlib import Path

from auditgraph.core.models import SourceDocument


class FileIngestor:
    def __init__(self, *, max_bytes: int = 10_000_000) -> None:
        self.max_bytes = max_bytes

    def ingest(self, path: str | Path) -> SourceDocument:
        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"file source does not exist: {file_path}")
        size = file_path.stat().st_size
        if size > self.max_bytes:
            raise ValueError(f"file source exceeds {self.max_bytes} bytes: {file_path}")
        content_type = {
            ".json": "application/json",
            ".html": "text/html",
            ".htm": "text/html",
            ".csv": "text/csv",
        }.get(file_path.suffix.lower(), "text/plain")
        return SourceDocument(
            source_id=f"file:{file_path}",
            source_type="file",
            content=file_path.read_text(encoding="utf-8"),
            content_type=content_type,
            metadata={"path": str(file_path), "size": size},
        )
