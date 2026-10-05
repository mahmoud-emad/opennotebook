"""The web, faked for tests: names resolve without DNS and pages come from a
handler, through the same checked transport the studio reads pages with."""

import ipaddress
from collections.abc import Callable

import httpx
import pytest

from opennotebook.domain import sources

# Where every outside name resolves to in a test: a documentation address.
OUTSIDE = "203.0.113.7"


def fake_address(host: str) -> str:
    """`public_address` without DNS: an address literal is checked as it is,
    `localhost` is this machine, and every other name is outside."""
    if host == "localhost":
        raise sources.Refused(sources.INSIDE)
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return OUTSIDE
    if not addr.is_global:
        raise sources.Refused(sources.INSIDE)
    return host


def fake_web(
    monkeypatch: pytest.MonkeyPatch, pages: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Pages answered by `pages`, which reads the site's name from the Host
    header: the URL it is handed carries the address the name resolved to."""
    monkeypatch.setattr(sources, "public_address", fake_address)
    monkeypatch.setattr(sources, "network", lambda: httpx.MockTransport(pages))
