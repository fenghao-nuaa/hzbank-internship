"""Application-driven CCR recall client.

Headroom's transparent auto-continuation of retrieve tool calls is not reliable
on the OpenAI-compatible proxy path (0.33/0.34). Instead, we drive recall from
the memory service: markers embedded in the compressed context point at originals
stored in Headroom's CCR store, and we call the public ``POST /v1/retrieve``
endpoint ourselves to pull the original content back when the caller decides it
is needed.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

import httpx

from short_term_memory.compression.headroom_client import _SCOPE_HEADER_NAMES

# Matches Headroom's compression markers:
#   "[1000 items compressed to 20. Retrieve more: hash=abc123]"
#   "Retrieve original: hash=<hash>"
#   "<<ccr:abc123"
HASH_AFTER_KEY = re.compile(r"hash=([0-9a-fA-F]{12,128})")
HASH_INLINE = re.compile(r"<<ccr:([A-Za-z0-9_-]{1,128})")


class CcrRecallError(RuntimeError):
    """Base error for CCR recall failures."""


class CcrRecallUnavailableError(CcrRecallError):
    """The Headroom service could not be reached."""


class CcrRecallNotFoundError(CcrRecallError):
    """The requested hash was not present in the CCR store."""


class CcrRecallInvalidError(CcrRecallError):
    """The response from the Headroom retrieve endpoint was malformed."""


def extract_marker_hashes(messages: Any) -> tuple[str, ...]:
    """Return the unique marker hashes embedded in a message list.

    Messages may be opaque ``SessionCompressionMessage`` objects, dicts, or a
    JSON string; we scan their textual content so the caller can decide which
    originals to pull back.
    """
    found: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        text: str
        if isinstance(content, str):
            text = content
        elif content is not None:
            text = json.dumps(content, ensure_ascii=False)
        else:
            continue
        for match in HASH_AFTER_KEY.finditer(text):
            found.append(match.group(1))
        for match in HASH_INLINE.finditer(text):
            found.append(match.group(1))
    # Preserve first-seen order, deduplicate.
    return tuple(dict.fromkeys(found))


class CcrRecallClient:
    """Async client for ``POST /v1/retrieve`` (application-driven CCR recall)."""

    def __init__(
        self,
        service_url: str,
        *,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not service_url.strip():
            raise ValueError("service_url must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.service_url = service_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._owns_http_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def recall(
        self,
        hash_value: str,
        *,
        scope_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Return the original content for a marker hash.

        Raises :class:`CcrRecallError` subclasses on failure so callers can
        decide how to degrade (e.g. fall back to the compressed context).
        """
        hash_value = hash_value.strip()
        if not hash_value:
            raise CcrRecallInvalidError("hash must not be blank")
        headers = _scope_headers(scope_headers)
        try:
            response = await self.http.post(
                f"{self.service_url}/v1/retrieve",
                json={"hash": hash_value},
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise CcrRecallUnavailableError("recall timed out") from exc
        except httpx.RequestError as exc:
            raise CcrRecallUnavailableError(
                f"recall request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 404:
            raise CcrRecallNotFoundError(f"hash not found in CCR store: {hash_value}")
        if response.status_code != 200:
            raise CcrRecallUnavailableError(
                f"recall returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
            content = payload["original_content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CcrRecallInvalidError("recall response was malformed") from exc
        if not isinstance(content, str) or not content:
            raise CcrRecallInvalidError("recall returned empty original content")
        return content

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self.http.aclose()

    async def recall_recursive(
        self,
        hash_value: str,
        *,
        scope_headers: Mapping[str, str] | None = None,
        max_depth: int = 5,
    ) -> str:
        """Follow the CCR hash chain down to the true original content.

        Re-compression can produce a marker (hash_B) whose content still embeds an
        earlier marker (hash_A).  This method recursively resolves: it recalls
        ``hash_B``, scans the returned content for embedded markers, and if found,
        recalls those in turn, until the returned content contains no markers —
        i.e. the original text.
        """
        visited: set[str] = set()

        async def _resolve(current: str, depth: int) -> str:
            if depth > max_depth or current in visited:
                return ""
            visited.add(current)
            content = await self.recall(current, scope_headers=scope_headers)
            embedded = extract_marker_hashes(
                [{"role": "assistant", "content": content}]
            )
            if not embedded:
                return content
            parts: list[str] = []
            for h in embedded:
                sub = await _resolve(h, depth + 1)
                if sub:
                    parts.append(sub)
            return "\n---\n".join(parts) if parts else content

        return await _resolve(hash_value, 0)


def _scope_headers(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    headers = dict(value)
    if set(headers) != _SCOPE_HEADER_NAMES:
        raise ValueError("invalid Headroom scope headers")
    if any(not isinstance(item, str) or not item.strip() for item in headers.values()):
        raise ValueError("invalid Headroom scope header value")
    return headers
