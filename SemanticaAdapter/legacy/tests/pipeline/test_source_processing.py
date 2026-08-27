import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from auditgraph.core.models import SourceDocument
from auditgraph.ingest import APIIngestor, DBIngestor, FileIngestor, WebIngestor
from auditgraph.normalize import DateNormalizer, NumberNormalizer, TextNormalizer
from auditgraph.parse import DocumentParser
from auditgraph.semantic_extract import SemanticExtractor
from auditgraph.split import TextSplitter


SEMANTIC_TEXT = """\
ENTITY|A-1|LoanApplication|Application A-1
ENTITY|POL-001|Policy|Credit Risk Policy
RELATION|A-1|governed_by|POL-001
EVENT|application_submitted|A-1|2026-08-24
TRIPLE|A-1|risk_score|82
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/page":
            payload = f"<html><body><h1>Policy</h1><pre>{SEMANTIC_TEXT}</pre></body></html>".encode()
            content_type = "text/html"
        else:
            payload = json.dumps({"application": "A-1", "risk_score": 82}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def test_all_sources_become_documents(tmp_path, local_server) -> None:
    file_path = tmp_path / "policy.txt"
    file_path.write_text(SEMANTIC_TEXT, encoding="utf-8")

    db_path = tmp_path / "applications.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE applications (application_id TEXT, risk_score INTEGER)")
    connection.execute("INSERT INTO applications VALUES ('A-1', 82)")
    connection.commit()
    connection.close()

    documents = [
        FileIngestor().ingest(file_path),
        WebIngestor(allow_private_network=True).ingest(f"{local_server}/page"),
        DBIngestor().ingest_sqlite(db_path, "SELECT * FROM applications"),
        APIIngestor(allow_private_network=True).ingest(f"{local_server}/api"),
    ]

    assert {document.source_type for document in documents} == {"file", "web", "database", "api"}
    assert all(document.content and document.source_id for document in documents)
    assert all(document.collected_at.tzinfo is not None for document in documents)


def test_http_ingestors_block_private_network_by_default(local_server) -> None:
    with pytest.raises(ValueError, match="private network"):
        WebIngestor().ingest(f"{local_server}/page")
    with pytest.raises(ValueError, match="private network"):
        APIIngestor().ingest(f"{local_server}/api")


def test_parse_normalize_and_split_preserve_source() -> None:
    raw = SourceDocument(
        source_id="web:policy",
        source_type="web",
        content="<html><body>  Risk\u00a0 Policy  </body></html>",
        content_type="text/html",
    )

    parsed = DocumentParser().parse(raw)
    normalized = TextNormalizer().normalize_document(parsed)
    chunks = TextSplitter(max_chars=6).split(normalized)

    assert normalized.content == "Risk Policy"
    assert len(chunks) == 2
    assert all(chunk.source_id == "web:policy" for chunk in chunks)
    assert "parent_source_id" in chunks[0].metadata


def test_date_and_number_normalization() -> None:
    assert DateNormalizer().normalize("2026年8月24日") == "2026-08-24"
    assert NumberNormalizer().normalize("人民币10万元") == 100000


def test_processing_produces_all_semantic_outputs() -> None:
    document = SourceDocument(source_id="file:policy", source_type="file", content=SEMANTIC_TEXT)
    chunks = TextSplitter(max_chars=10_000).split(document)
    result = SemanticExtractor().extract(chunks)

    assert {entity.entity_id for entity in result.entities} == {"A-1", "POL-001"}
    assert result.relations[0].predicate == "governed_by"
    assert result.events[0].event_type == "application_submitted"
    assert result.triplets[0].object == 82
    assert result.triplets[0].source_id == "file:policy"


def test_splitter_never_cuts_a_semantic_record() -> None:
    long_name = "A regulated application name containing many spaces"
    document = SourceDocument(
        source_id="file:long",
        source_type="file",
        content=f"ENTITY|A-1|LoanApplication|{long_name}\nTRIPLE|A-1|risk_score|82",
    )
    chunks = TextSplitter(max_chars=20).split(document)
    assert chunks[0].content == f"ENTITY|A-1|LoanApplication|{long_name}"
    result = SemanticExtractor().extract(chunks)
    assert result.entities[0].name == long_name
    assert result.triplets[0].object == 82
