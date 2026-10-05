"""Files and web pages becoming sources."""

from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient

from opennotebook.domain import sources

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
    def loopback_only(host: str) -> bool:
        return host == "127.0.0.1"

    monkeypatch.setattr(sources, "_private", loopback_only)
    real = sources.http_client

    def mocked() -> httpx.AsyncClient:
        c = real()
        c._transport = httpx.MockTransport(_pages)  # pyright: ignore[reportPrivateUsage]
        return c

    monkeypatch.setattr(sources, "http_client", mocked)
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


def test_private_addresses_are_this_network() -> None:
    assert sources._private("127.0.0.1")  # pyright: ignore[reportPrivateUsage]
    assert sources._private("localhost")  # pyright: ignore[reportPrivateUsage]
