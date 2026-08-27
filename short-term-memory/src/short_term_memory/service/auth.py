"""Bearer authentication for the memory HTTP service."""

import secrets


class AuthenticationError(RuntimeError):
    """Credentials are absent or do not match the configured bearer token."""


class BearerTokenAuthenticator:
    """Validate bearer credentials without exposing why authentication failed."""

    def __init__(self, *, auth_token: str, environment: str) -> None:
        normalized_environment = environment.strip().casefold()
        if not auth_token.strip() and normalized_environment != "development":
            raise ValueError(
                "MEMORY_API_AUTH_TOKEN must not be blank outside development"
            )
        self._auth_token = auth_token
        self._disabled = not auth_token.strip()

    def verify(self, authorization: str | None) -> None:
        """Accept one exact bearer token or raise the same sanitized error."""

        if self._disabled:
            return
        supplied = self._extract_bearer_token(authorization)
        if not secrets.compare_digest(supplied, self._auth_token):
            raise AuthenticationError("unauthorized")

    @staticmethod
    def _extract_bearer_token(authorization: str | None) -> str:
        if authorization is None:
            return ""
        scheme, separator, credentials = authorization.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not credentials
            or credentials.strip() != credentials
            or " " in credentials
        ):
            return ""
        return credentials
