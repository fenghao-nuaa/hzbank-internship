"""Read-only SQLite ingestion for deterministic local and test use."""

import json
import sqlite3
from pathlib import Path

from auditgraph.core.models import SourceDocument


class DBIngestor:
    def ingest_sqlite(self, path: str | Path, query: str, parameters: tuple[object, ...] = ()) -> SourceDocument:
        normalized_query = query.lstrip().lower()
        if not normalized_query.startswith(("select", "with", "pragma")):
            raise ValueError("database ingestion only accepts read-only SELECT/WITH/PRAGMA queries")
        db_path = Path(path).resolve()
        if not db_path.is_file():
            raise FileNotFoundError(f"database source does not exist: {db_path}")
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in connection.execute(query, parameters).fetchall()]
        finally:
            connection.close()
        return SourceDocument(
            source_id=f"database:{db_path}:{query}",
            source_type="database",
            content=json.dumps(rows, ensure_ascii=False, sort_keys=True),
            content_type="application/json",
            metadata={"database": str(db_path), "query": query, "row_count": len(rows)},
        )
