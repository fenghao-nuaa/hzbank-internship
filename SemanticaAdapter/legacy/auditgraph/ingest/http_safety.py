"""Shared URL validation for HTTP ingestors."""

import ipaddress
import socket
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise HTTPError(req.full_url, code, "redirects are disabled", headers, fp)


def validate_http_url(url: str, *, allow_private_network: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid HTTP source URL: {url}")
    if parsed.username or parsed.password:
        raise ValueError("credentials in source URLs are not allowed")
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except socket.gaierror as error:
        raise ValueError(f"cannot resolve source host: {parsed.hostname}") from error
    if not allow_private_network and any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    ):
        raise ValueError(f"private network source is blocked: {url}")


def no_redirect_opener():
    return build_opener(_NoRedirect())
