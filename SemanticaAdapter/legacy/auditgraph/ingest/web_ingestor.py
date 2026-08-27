"""HTTP web-page ingestion with bounded reads."""

from urllib.request import Request

from auditgraph.core.models import SourceDocument

from .http_safety import no_redirect_opener, validate_http_url


class WebIngestor:
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

    def ingest(self, url: str) -> SourceDocument:
        validate_http_url(url, allow_private_network=self.allow_private_network)
        request = Request(url, headers={"User-Agent": "AuditGraph/0.1"})
        with no_redirect_opener().open(request, timeout=self.timeout) as response:
            payload = response.read(self.max_bytes + 1)
            if len(payload) > self.max_bytes:
                raise ValueError(f"web source exceeds {self.max_bytes} bytes: {url}")
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
        return SourceDocument(
            source_id=f"web:{url}",
            source_type="web",
            content=payload.decode(charset, errors="replace"),
            content_type=content_type,
            metadata={"url": url, "status": 200},
        )
