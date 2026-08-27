import secrets

import pytest

from short_term_memory.service.auth import (
    AuthenticationError,
    BearerTokenAuthenticator,
)


def test_production_rejects_a_blank_auth_token() -> None:
    with pytest.raises(ValueError, match="MEMORY_API_AUTH_TOKEN"):
        BearerTokenAuthenticator(auth_token="  ", environment="production")


def test_only_explicit_development_allows_a_blank_auth_token() -> None:
    authenticator = BearerTokenAuthenticator(auth_token="", environment="development")

    authenticator.verify(None)
    authenticator.verify("anything is ignored in explicit development")


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic dXNlcjpwYXNz", "Bearer", "Bearer wrong-token"],
)
def test_missing_malformed_and_wrong_credentials_are_equivalent(
    authorization: str | None,
) -> None:
    authenticator = BearerTokenAuthenticator(
        auth_token="correct-token", environment="production"
    )

    with pytest.raises(AuthenticationError) as raised:
        authenticator.verify(authorization)

    assert str(raised.value) == "unauthorized"


def test_bearer_token_is_compared_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[str, str]] = []
    original_compare = secrets.compare_digest

    def record_compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(
        "short_term_memory.service.auth.secrets.compare_digest", record_compare
    )
    authenticator = BearerTokenAuthenticator(
        auth_token="correct-token", environment="production"
    )

    authenticator.verify("Bearer correct-token")

    assert compared == [("correct-token", "correct-token")]
