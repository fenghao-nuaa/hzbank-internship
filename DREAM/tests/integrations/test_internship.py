import httpx
import pytest

from dream.integrations.internship.client import (
    InternshipSourceClient,
    SourceFetchError,
    parse_ndjson,
)
from tests.integrations.source_helpers import source_line, source_settings


def test_parse_ndjson_accepts_records_and_empty_lines() -> None:
    records = parse_ndjson("\n" + source_line() + "\n")
    assert len(records) == 1
    assert records[0].cursor == "101"
    assert records[0].messages[0].role == "user"


def test_parse_ndjson_empty_body_means_no_new_records() -> None:
    assert parse_ndjson("") == ()


def test_parse_ndjson_rejects_missing_required_field() -> None:
    with pytest.raises(SourceFetchError, match="line 1"):
        parse_ndjson('{"cursor":"101"}\n')


def test_parse_ndjson_rejects_completed_at_without_timezone() -> None:
    with pytest.raises(SourceFetchError, match="line 1"):
        parse_ndjson(source_line(completed_at="2026-07-15T10:00:00"))


def test_client_sends_cursor_limit_accept_and_bearer_token() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["accept"] = request.headers.get("accept")
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, text=source_line() + "\n")

    settings = source_settings(batch_size=20, interval_seconds=60)
    client = InternshipSourceClient(
        settings, transport=httpx.MockTransport(handler)
    )
    records = client.fetch(after="100", limit=20)
    assert len(records) == 1
    assert captured == {
        "url": "https://memory.example/v1/memory/dream-export?after=100&limit=20",
        "accept": "application/x-ndjson",
        "authorization": "Bearer secret",
    }


def test_client_rejects_non_success_without_leaking_response_body() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(500, text="secret upstream body")
    )
    settings = source_settings(url="https://memory.example/export", api_key="")
    with pytest.raises(SourceFetchError) as error:
        InternshipSourceClient(settings, transport=transport).fetch("", 100)
    assert "secret upstream body" not in str(error.value)


def test_client_wraps_timeout_without_leaking_request_headers() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = InternshipSourceClient(
        source_settings(), transport=httpx.MockTransport(timeout)
    )
    with pytest.raises(SourceFetchError, match="request failed") as error:
        client.fetch("100", 20)
    assert "Bearer secret" not in str(error.value)
