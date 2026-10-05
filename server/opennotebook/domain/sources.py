"""Sources: what a collection's outputs are made from, kept verbatim.

Three ways in, each ported from the Rust server (`create.rs`):
- a note somebody typed or pasted, kept as it is;
- a file, converted to Markdown by `opennotebook.convert` (no model sees it);
- a web page, read over plain HTTP and reduced to its main text.

Every refusal is a sentence that says what happened and what to do, because
the source panel shows it next to the thing that was not added.
"""

import asyncio
import ipaddress
import re
import socket
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
import trafilatura
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import memory, storage
from opennotebook.convert import ConvertError, InputKind, to_markdown
from opennotebook.db.models import Source
from opennotebook.domain import collections

MAX_URLS = 8
# The largest file a person may hand over, and the largest page read.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_PAGE_BYTES = 25 * 1024 * 1024
# A converted document with fewer visible characters than this is a scan or
# an image-only export: there is nothing in it to ground anything on.
MIN_CONVERTED_CHARS = 80
# A page with less readable text than this is a shell, a login or a bot check.
MIN_PAGE_CHARS = 200
# A note is named by this many of its first words.
NOTE_NAME_WORDS = 6

# Extensions whose bytes are already the document: no parser in the path.
TEXT_EXTENSIONS = {"md", "markdown", "txt", "text", "csv"}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 OpenNotebook"
)


class Refused(Exception):
    """Why one thing was not added, as the sentence to show beside it."""


@dataclass
class Added:
    source: Source | None
    error: str = ""


# ── names ─────────────────────────────────────────────────────────────────────


def slug(s: str) -> str:
    out: list[str] = []
    dash = False
    for c in s:
        if c.isascii() and c.isalnum():
            out.append(c.lower())
            dash = False
        elif not dash and out:
            out.append("_")
            dash = True
        if len(out) >= 48:
            break
    return "".join(out).strip("_") or "source"


def one_line(s: str) -> str:
    return " ".join("".join(c for c in s if c.isprintable() or c.isspace()).split())


async def _free_name(s: AsyncSession, cid: uuid.UUID, base: str) -> str:
    taken = set((await s.scalars(select(Source.name).where(Source.collection_id == cid))).all())
    name, n = f"{base}.md", 2
    while name in taken:
        name, n = f"{base}_{n}.md", n + 1
    return name


async def _keep(
    s: AsyncSession,
    owner: uuid.UUID,
    cid: uuid.UUID,
    *,
    kind: str,
    title: str,
    text: str,
    url: str = "",
    named: str | None = None,
) -> Source:
    src = Source(
        owner_id=owner,
        collection_id=cid,
        name=await _free_name(s, cid, slug(named or title)),
        kind=kind,
        title=title,
        url=url,
        text=text,
        chars=len(text),
    )
    s.add(src)
    await s.flush()
    await s.refresh(src)
    # Searchable from the moment it is added, in the same transaction.
    await memory.index_source(s, src)
    return src


# ── notes ─────────────────────────────────────────────────────────────────────


async def add_note(
    s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, text: str, title: str = ""
) -> Source:
    """Keep a note as it was written. A typed sentence is a source; an empty
    note is not."""
    text = text.strip()
    if not text:
        raise Refused("The note is empty. Write or paste something, then add it.")
    await collections.lock(s, owner, cid)
    title = one_line(title)
    named = title or " ".join(text.split()[:NOTE_NAME_WORDS])
    # A title, when given, becomes the note's heading, which is what a build
    # and the source list read it by.
    body = f"# {title}\n\n{text}" if title else text
    src = await _keep(s, owner, cid, kind="text", title=named, text=body)
    await collections.touch(s, cid)
    return src


# ── files ─────────────────────────────────────────────────────────────────────


def _extension(name: str) -> str:
    return PurePosixPath(name).suffix.lstrip(".").lower()


def convert_file(name: str, data: bytes) -> str:
    """A file's text, verbatim, or `Refused` saying why there is none."""
    ext = _extension(name)
    if len(data) > MAX_UPLOAD_BYTES:
        raise Refused(f"{name} is larger than 25 MB. Split it, or upload the part you need.")
    if ext in TEXT_EXTENSIONS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(
                f"{name} is not plain text in UTF-8. Save it as UTF-8 text, then upload it again."
            ) from None
        if not text.strip():
            raise Refused(f"{name} is empty.")
        return text
    kind = InputKind.from_extension(ext)
    if kind is None:
        raise Refused(
            f"{name} is not a kind of file the studio reads. "
            "Upload a PDF, Word, PowerPoint, Excel, Markdown, text or CSV file."
        )
    try:
        md = to_markdown(data, kind)
    except ConvertError as e:
        raise Refused(str(e)) from e
    if sum(1 for c in md if not c.isspace()) < MIN_CONVERTED_CHARS:
        raise Refused(
            f"{name} has almost no text in it, so it is probably a scan or an image-only "
            "export. Upload a version with real text, or paste the text as a note."
        )
    return md


async def add_file(
    s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, name: str, data: bytes
) -> Source:
    name = one_line(PurePosixPath(name).name) or "document"
    # Converting is CPU work on untrusted bytes; off the event loop.
    text = await asyncio.to_thread(convert_file, name, data)
    await collections.lock(s, owner, cid)
    title = PurePosixPath(name).stem or name
    src = await _keep(s, owner, cid, kind="file", title=title, text=text)
    src.file_path = storage.put(f"uploads/{cid}/{src.id}{PurePosixPath(name).suffix}", data)
    await collections.touch(s, cid)
    return src


# ── web pages ─────────────────────────────────────────────────────────────────

_REFUSAL_TITLES = {
    "access denied",
    "403 forbidden",
    "forbidden",
    "just a moment",
    "attention required",
    "are you a robot",
    "request rejected",
    "security check",
    "captcha",
    "blocked",
}
_REFUSAL_TEXT = (
    "you don't have permission to access",
    "you do not have permission to access",
    "access to this page has been denied",
    "verify you are human",
    "enable javascript and cookies to continue",
    "request unsuccessful. incapsula",
)


def is_refusal(title: str, text: str) -> bool:
    """A bot check or an access-denied page. A refusal can carry enough words
    to pass the length check: an "Access Denied" page was once added as a
    40-word source and then presented as the book it refused to show."""
    main = re.split(r"[|–—]", title.lower())[0].split(" - ")[0]
    main = re.sub(r"^\W+|\W+$", "", main)
    if main in _REFUSAL_TITLES:
        return True
    short = sum(1 for c in text if not c.isspace()) < 2000
    low = text.lower()
    return short and any(b in low for b in _REFUSAL_TEXT)


def fallback_title(url: str) -> str:
    """A name for a page that declares none: its address made readable,
    `tldp.org/LDP/tlk/kernel/processes.html` → `tldp.org · processes`."""
    parts = urlsplit(url)
    host = (parts.hostname or url).removeprefix("www.")
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    last = re.split(r"[.?#]", last)[0].replace("-", " ").replace("_", " ").strip()
    return f"{host} · {last}" if last else host


def _private(host: str) -> bool:
    """Whether a host is this machine or its network: the studio must not be
    a way to read pages only it can see."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # Unresolvable: the fetch fails with its own sentence.
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            return True
    return False


def _open_error(e: Exception) -> str:
    r = f"{type(e).__name__} {e}".lower()
    if any(k in r for k in ("name or service", "nodename", "resolve", "getaddrinfo", "dns")):
        return "The site could not be reached: its address does not exist. Check the link."
    if "timeout" in r or "timed out" in r:
        return "The site took too long to answer. Try again later, or use another page."
    if "refused" in r:
        return "The site refused the connection. Try again later, or use another page."
    if any(k in r for k in ("certificate", "ssl", "tls")):
        return "The site's security certificate is not valid, so the page was not read."
    return "The page could not be opened. Check the link, or use another page."


@dataclass
class Page:
    title: str
    text: str


async def read_page(http: httpx.AsyncClient, url: str) -> Page:
    """One page's title and main text, or `Refused`."""
    try:
        async with http.stream("GET", url) as resp:
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_PAGE_BYTES:
                    raise Refused("The page is too large to read. Use a shorter page.")
                chunks.append(chunk)
            data = b"".join(chunks)
            status = resp.status_code
            ctype = resp.headers.get("content-type", "").lower()
            final_url = str(resp.url)
    except httpx.HTTPError as e:
        raise Refused(_open_error(e)) from e
    # A bot check is often a 403 or 503 with a page of its own; it is read
    # anyway so the refusal check can name it.
    if not 200 <= status < 300 and status not in (403, 429, 503):
        raise Refused(
            {
                404: "The page does not exist (404). Check the link.",
                410: "The page does not exist any more (410). Use another page.",
                401: "The page needs a login, so the studio cannot read it. "
                "Paste its text as a note instead.",
            }.get(status, f"The site answered with an error ({status}). Try another page.")
        )
    # A linked PDF is a document, read like an uploaded one.
    if "application/pdf" in ctype or data.startswith(b"%PDF-"):
        md = convert_file("the linked PDF", data)
        heading = next((ln[2:] for ln in md.splitlines() if ln.startswith("# ")), "")
        return Page(heading or fallback_title(final_url), md)
    if ctype and not ("html" in ctype or "xml" in ctype or ctype.startswith("text/")):
        raise Refused(
            f"The link is not a web page ({ctype.split(';')[0]}). "
            "Download it and upload it as a file instead."
        )
    html = data.decode(resp.encoding or "utf-8", errors="replace")
    text = await asyncio.to_thread(
        trafilatura.extract,
        html,
        url=final_url,
        output_format="markdown",
        include_links=False,
        include_comments=False,
        favor_recall=True,
    )
    meta = await asyncio.to_thread(trafilatura.extract_metadata, html, default_url=final_url)
    title = one_line((meta.title if meta else None) or "")
    return Page(title, text or "")


async def fetch_and_keep(
    s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, http: httpx.AsyncClient, url: str
) -> Source:
    page = await read_page(http, url)
    if is_refusal(page.title, page.text):
        raise Refused(
            "The site refused an automated visit. Open it in your browser and paste the text "
            "as a note."
        )
    if sum(1 for c in page.text if not c.isspace()) < MIN_PAGE_CHARS:
        raise Refused(
            "The page has almost no readable text: it may need a login or JavaScript. "
            "Paste its text as a note instead."
        )
    title = page.title or fallback_title(url)
    body = f"# {title}\n\nSource: {url}\n\n{page.text}"
    await collections.lock(s, owner, cid)
    src = await _keep(s, owner, cid, kind="url", title=title, text=body, url=url)
    await collections.touch(s, cid)
    return src


async def _outside_only(request: httpx.Request) -> None:
    """Checked on every request, redirects included, so a public link cannot
    bounce the studio onto its own network."""
    if await asyncio.to_thread(_private, request.url.host):
        raise Refused("That link points inside the studio's own network, so it was not read.")


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        event_hooks={"request": [_outside_only]},
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        max_redirects=10,
        timeout=httpx.Timeout(45, connect=15),
    )
