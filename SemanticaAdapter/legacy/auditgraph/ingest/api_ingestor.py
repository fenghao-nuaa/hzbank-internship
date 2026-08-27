"""JSON business-system API ingestion."""

import json
from urllib.request import Request

from auditgraph.core.models import SourceDocument

from .http_safety import no_redirect_opener, validate_http_url


class APIIngestor:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_bytes: int = 5_000_000,
        allow_private_network: bool = False,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.allow_private_network = allow_private_network

    def ingest(self, url: str, *, headers: dict[str, str] | None = None) -> SourceDocument:
        validate_http_url(url, allow_private_network=self.allow_private_network)
        request_headers = {"Accept": "application/json", "User-Agent": "AuditGraph/0.1"}
        request_headers.update(headers or {})
        with no_redirect_opener().open(Request(url, headers=request_headers), timeout=self.timeout) as response:
            payload = response.read(self.max_bytes + 1)
            if len(payload) > self.max_bytes:
                raise ValueError(f"API source exceeds {self.max_bytes} bytes: {url}")
            charset = response.headers.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="strict")
        parsed_payload = json.loads(decoded)
        return SourceDocument(
            source_id=f"api:{url}",
            source_type="api",
            content=json.dumps(parsed_payload, ensure_ascii=False, sort_keys=True),
            content_type="application/json",
            metadata={"url": url, "status": 200},
        )
