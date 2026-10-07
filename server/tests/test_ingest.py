"""Files and web pages becoming sources."""

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient

from opennotebook.domain import sources
from tests.web import fake_web

FIXTURES = Path(__file__).parent / "convert" / "fixtures"


def _uploads(root: Path) -> list[Path]:
    return list(root.rglob("*.md"))


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def test_files_are_read_verbatim_and_refusals_are_sentences(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENNOTEBOOK_FILES_DIR", str(tmp_path))
    from opennotebook.config import settings

    settings.cache_clear()
    cid = await _collection(client)
    files = [
        ("files", ("notes.md", b"# Reefs\n\nPolyps build reefs_slowly * 3.", "text/markdown")),
        (
            "files",
            (
                "verbatim.docx",
                (FIXTURES / "verbatim.docx").read_bytes(),
                "application/octet-stream",
            ),
        ),
        ("files", ("scanned.pdf", (FIXTURES / "scanned.pdf").read_bytes(), "application/pdf")),
        ("files", ("old.doc", b"\xd0\xcf\x11\xe0", "application/msword")),
    ]
    r = await client.post(f"/api/collections/{cid}/sources/files", files=files)
    assert r.status_code == 201, r.text
    md, docx, scan, doc = r.json()
    assert md["ok"] and md["source"]["title"] == "notes" and md["source"]["kind"] == "file"
    assert docx["ok"], docx
    assert not scan["ok"] and "probably a scan" in scan["error"]
    assert not doc["ok"] and doc["error"].startswith("old.doc is not a kind of file")

    text = (await client.get(f"/api/collections/{cid}/sources/{md['source']['name']}")).json()
    # Kept as written: no escaping of _ or *.
    assert text["text"] == "# Reefs\n\nPolyps build reefs_slowly * 3."
    # The original upload is on the files volume, and goes with its source.
    assert len(_uploads(tmp_path)) == 1
    assert (
        await client.delete(f"/api/collections/{cid}/sources/{md['source']['name']}")
    ).status_code == 204
    assert _uploads(tmp_path) == []
    settings.cache_clear()


PAGE = """<html><head><title>Coral reefs | Ocean facts</title></head><body>
<nav>Home About</nav><article><h1>Coral reefs</h1>
<p>Coral reefs are built by colonies of tiny animals called polyps, which secrete calcium
carbonate skeletons. Over thousands of years these skeletons accumulate into vast structures.</p>
<p>Reefs cover less than one percent of the ocean floor, yet they shelter roughly a quarter of all
marine species, which makes them among the most diverse ecosystems on the planet.</p>
</article></body></html>"""

DENIED = (
    "<html><head><title>Access Denied</title></head><body>" + "<p>no</p>" * 300 + "</body></html>"
)


def _pages(request: httpx.Request) -> httpx.Response:
    match request.url.path:
        case "/reefs":
            return httpx.Response(200, html=PAGE)
        case "/denied":
            return httpx.Response(403, html=DENIED)
        case "/thin":
            return httpx.Response(200, html="<html><body><div id=app></div></body></html>")
        case "/file.zip":
            return httpx.Response(200, content=b"PK", headers={"content-type": "application/zip"})
        case _:
            return httpx.Response(404, text="nope")


async def test_pages_are_read_and_refusals_say_why(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_web(monkeypatch, _pages)
    cid = await _collection(client)
    urls = [
        "https://example.com/reefs",
        "https://example.com/denied",
        "https://example.com/thin",
        "https://example.com/gone",
        "https://example.com/file.zip",
        "http://127.0.0.1/admin",
    ]
    r = await client.post(f"/api/collections/{cid}/sources", json={"kind": "urls", "urls": urls})
    assert r.status_code == 201, r.text
    reefs, denied, thin, gone, zipped, inside = r.json()
    assert reefs["ok"] and reefs["source"]["title"] == "Coral reefs"
    assert reefs["source"]["url"] == "https://example.com/reefs"
    assert denied["error"].startswith("The site refused an automated visit.")
    assert thin["error"].startswith("The page has almost no readable text")
    assert gone["error"] == "The page does not exist (404). Check the link."
    assert zipped["error"].startswith("The link is not a web page (application/zip).")
    assert inside["error"].startswith("That link points inside the studio's own network")

    body = (await client.get(f"/api/collections/{cid}/sources/{reefs['source']['name']}")).json()
    assert "polyps" in body["text"] and "Home About" not in body["text"]


def test_a_page_without_a_title_is_named_by_its_address() -> None:
    assert sources.fallback_title("https://tldp.org/LDP/tlk/kernel/processes.html") == (
        "tldp.org · processes"
    )
    assert sources.fallback_title("https://www.example.com/") == "example.com"


def test_refusal_pages_are_known_by_their_title_or_words() -> None:
    assert sources.is_refusal("Attention Required! | Cloudflare", "")
    assert sources.is_refusal("Reefs", "Please verify you are human to continue.")
    assert not sources.is_refusal("Blocked I/O in Linux", "long text " * 50)


def test_addresses_of_this_machine_and_its_network_are_refused() -> None:
    for host in ("127.0.0.1", "localhost", "10.0.0.8", "169.254.169.254", "::1"):
        with pytest.raises(sources.Refused, match="inside the studio's own network"):
            sources.public_address(host)
    # Dressed as IPv6, the loopback address is still this machine.
    with pytest.raises(sources.Refused, match="inside the studio's own network"):
        sources.public_address("::ffff:127.0.0.1")
    assert sources.public_address("8.8.8.8") == "8.8.8.8"


def test_a_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def nowhere(*_: object, **__: object) -> list[object]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", nowhere)
    with pytest.raises(sources.Refused, match="its address does not exist"):
        sources.public_address("no-such-site.example")


def test_one_private_address_among_public_ones_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def both(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", both)
    with pytest.raises(sources.Refused, match="inside the studio's own network"):
        sources.public_address("rebinding.example")


async def test_a_page_is_fetched_from_the_address_that_was_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolved once: the connection goes to the checked address, with the
    site's name kept for its Host header and its certificate, and a name
    that later answers with a private address cannot be read."""
    answers = iter(["203.0.113.9", "127.0.0.1"])

    def resolve(host: str) -> str:
        addr = next(answers)
        if addr.startswith("127."):
            raise sources.Refused(sources.INSIDE)
        return addr

    seen: list[httpx.Request] = []

    def site(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, html=PAGE)

    monkeypatch.setattr(sources, "public_address", resolve)
    monkeypatch.setattr(sources, "network", lambda: httpx.MockTransport(site))
    async with sources.http_client() as http:
        page = await sources.read_page(http, "https://news.example/reefs")
        assert page.title.startswith("Coral reefs")
        (sent,) = seen
        assert sent.url.host == "203.0.113.9" and sent.url.path == "/reefs"
        assert sent.headers["host"] == "news.example"
        assert sent.extensions["sni_hostname"] == "news.example"
        with pytest.raises(sources.Refused, match="inside the studio's own network"):
            await sources.read_page(http, "https://news.example/again")
    assert len(seen) == 1


async def test_a_redirect_inside_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def bounce(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    fake_web(monkeypatch, bounce)
    async with sources.http_client() as http:
        with pytest.raises(sources.Refused, match="inside the studio's own network"):
            await sources.read_page(http, "https://public.example/")


def test_proxies_in_the_environment_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:3128")
    c = sources.http_client()
    assert c._mounts == {}  # pyright: ignore[reportPrivateUsage]
    assert isinstance(c._transport, sources.OutsideOnly)  # pyright: ignore[reportPrivateUsage]


async def test_a_page_that_never_finishes_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"<html>"
            await asyncio.sleep(10)
            yield b"</html>"

    def slow(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Slow())

    fake_web(monkeypatch, slow)
    monkeypatch.setattr(sources, "PAGE_DEADLINE_S", 0.2)
    async with sources.http_client() as http:
        with pytest.raises(sources.Refused, match="took longer than"):
            await sources.read_page(http, "https://slow.example/")


async def test_too_many_files_at_once_are_refused(client: AsyncClient) -> None:
    cid = await _collection(client)
    files = [("files", (f"n{i}.md", b"# Note\n\nSome text.", "text/markdown")) for i in range(11)]
    r = await client.post(f"/api/collections/{cid}/sources/files", files=files)
    assert r.status_code == 422
    assert r.json()["detail"].startswith("That is 11 files; at most 10 can be added at once.")


async def test_a_linked_pdf_is_read_whatever_its_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """arXiv serves a paper at /pdf/1706.03762: no extension, only its type."""
    pdf = (FIXTURES / "verbatim.pdf").read_bytes()

    def serve(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pdf, headers={"Content-Type": "application/pdf"})

    fake_web(monkeypatch, serve)
    async with sources.http_client() as http:
        page = await sources.read_page(http, "https://arxiv.example/pdf/1706.03762")
    assert len(page.text.split()) > 20
