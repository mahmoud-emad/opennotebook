"""Sources: what a collection's outputs are made from, kept verbatim.

Three ways in, each ported from the Rust server (`create.rs`):
- a note somebody typed or pasted, kept as it is;
- a file, converted to Markdown by `opennotebook.convert` (no model sees it);
- a web page, read over plain HTTP and reduced to its main text.

Adding is two steps. Preparing (`prepare_note`, `prepare_file`,
`prepare_page`) does everything slow — reading the page, converting the
file, splitting and embedding the text — outside any transaction. Keeping
(`keep`) is then a short write under the collection's lock.

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
# Files one upload may carry.
MAX_FILES = 10
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
# The longest a page may take to arrive, redirects and all, whatever pace
# the site sends it at.
PAGE_DEADLINE_S = 60

# Extensions whose bytes are already the document: no parser in the path.
TEXT_EXTENSIONS = {"md", "markdown", "txt", "text", "csv"}
# What a person is offered to upload, by extension and in words: the
# documents the converter reads and the plain text kinds. The file picker
# offers these; anything else is refused with `UPLOAD_KINDS`.
UPLOAD_EXTENSIONS = ("pdf", "docx", "pptx", "xlsx", "md", "markdown", "txt", "csv")
UPLOAD_KINDS = "PDF, Word, PowerPoint, Excel, Markdown, text or CSV"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 OpenNotebook"
)


def described(name: str, url: str, chars: int) -> tuple[int, str, str]:
    """How a source is shown under its name: about how many words it holds,
    the line saying where it came from and how long it is, and the icon to
    lead it with (its site's, read from the site itself; none for a note, a
    file or a report)."""
    words = chars // 6
    if url:
        parts = urlsplit(url)
        host = parts.netloc.split("@")[-1]
        while host.startswith("www."):
            host = host[4:]
        icon = f"{parts.scheme}://{parts.netloc}/favicon.ico" if parts.scheme else ""
        return words, f"{host} · {words} words", icon
    ext = PurePosixPath(name).suffix.lstrip(".").lower()
    kind = ext.upper() if ext and ext != "md" else "note"
    return words, f"{kind} · {words} words", ""


class Refused(Exception):
    """Why one thing was not added, as the sentence to show beside it."""


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


@dataclass
class Ready:
    """A source read, converted, split and embedded, waiting to be kept."""

    kind: str
    title: str
    text: str
    passages: memory.Passages
    url: str = ""
    # What its name is made from, when not its title.
    named: str | None = None
    # An uploaded file's extension and bytes, kept beside the text.
    upload: tuple[str, bytes] | None = None


async def prepare(
    kind: str,
    title: str,
    text: str,
    *,
    url: str = "",
    named: str | None = None,
    upload: tuple[str, bytes] | None = None,
) -> Ready:
    """A source ready to keep: its passages split and embedded now, outside
    any transaction, so keeping it is only a write."""
    return Ready(kind, title, text, await memory.passages(text), url, named, upload)


async def keep(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID, r: Ready) -> Source:
    """Keep a prepared source, searchable at once: a short write under the
    collection's lock, which also refuses a collection deleted while the
    source was being read. An upload's file is written first, before the
    lock, and removed again if the source is not kept."""
    path = None
    if r.upload is not None:
        suffix, data = r.upload
        path = await asyncio.to_thread(storage.put, f"uploads/{cid}/{uuid.uuid7()}{suffix}", data)
    try:
        await collections.lock(s, owner, cid)
        src = Source(
            owner_id=owner,
            collection_id=cid,
            name=await _free_name(s, cid, slug(r.named or r.title)),
            kind=r.kind,
            title=r.title,
            url=r.url,
            text=r.text,
            chars=len(r.text),
            file_path=path,
        )
        s.add(src)
        await s.flush()
        await s.refresh(src)
        await memory.store(s, src, r.passages)
        await collections.touch(s, cid)
    except BaseException:
        if path is not None:
            await asyncio.to_thread(storage.remove_file, path)
        raise
    return src


# ── notes ─────────────────────────────────────────────────────────────────────


async def prepare_note(text: str, title: str = "") -> Ready:
    """A note as it was written. A typed sentence is a source; an empty note
    is not."""
    text = text.strip()
    if not text:
        raise Refused("The note is empty. Write or paste something, then add it.")
    title = one_line(title)
    named = title or " ".join(text.split()[:NOTE_NAME_WORDS])
    # A title, when given, becomes the note's heading, which is what a build
    # and the source list read it by; unless the note already opens with it.
    first = text.splitlines()[0].strip()
    headed = first.startswith("# ") and first[2:].strip().casefold() == title.casefold()
    body = f"# {title}\n\n{text}" if title and not headed else text
    return await prepare("text", named, body)


# ── files ─────────────────────────────────────────────────────────────────────


def _extension(name: str) -> str:
    return PurePosixPath(name).suffix.lstrip(".").lower()


def convert_file(name: str, data: bytes, ext: str | None = None) -> str:
    """A file's text, verbatim, or `Refused` saying why there is none. Its
    kind is its name's extension, or `ext` for a file whose name is not a
    file name (a PDF from a link)."""
    ext = ext or _extension(name)
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
            f"{name} is not a kind of file the studio reads. Upload a {UPLOAD_KINDS} file."
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


async def prepare_file(name: str, data: bytes) -> Ready:
    """An uploaded file, read into text and titled by its name."""
    name = one_line(PurePosixPath(name).name) or "document"
    # Converting is CPU work on untrusted bytes; off the event loop.
    text = await asyncio.to_thread(convert_file, name, data)
    title = PurePosixPath(name).stem or name
    return await prepare("file", title, text, upload=(PurePosixPath(name).suffix, data))


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


NO_SUCH_SITE = "The site could not be reached: its address does not exist. Check the link."
INSIDE = "That link points inside the studio's own network, so it was not read."


def public_address(host: str) -> str:
    """The one address a page is read from: `host` resolved once, and
    refused when it is this machine or its network, or does not resolve. The
    connection goes to this address, so a name that answers differently a
    moment later cannot turn the studio onto a page only it can see."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError, UnicodeError:
        raise Refused(NO_SUCH_SITE) from None
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        # An IPv6 address can carry its interface (`fe80::1%eth0`).
        addr = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        found.append(addr)
    if not found:
        raise Refused(NO_SUCH_SITE)
    # Every address the name has, not only the first: one private address
    # among public ones is still a way in.
    if any(not a.is_global for a in found):
        raise Refused(INSIDE)
    return str(found[0])


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
        async with asyncio.timeout(PAGE_DEADLINE_S), http.stream("GET", url) as resp:
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
            encoding = resp.encoding
    except TimeoutError:
        raise Refused(
            f"The site took longer than {PAGE_DEADLINE_S} seconds to send the page. "
            "Try again later, or use another page."
        ) from None
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
        # Typed by what it is, not by its name: "the linked PDF" has no
        # extension, and was refused as a kind the studio does not read.
        md = await asyncio.to_thread(convert_file, "the linked PDF", data, "pdf")
        heading = next((ln[2:] for ln in md.splitlines() if ln.startswith("# ")), "")
        return Page(heading or fallback_title(final_url), md)
    if ctype and not ("html" in ctype or "xml" in ctype or ctype.startswith("text/")):
        raise Refused(
            f"The link is not a web page ({ctype.split(';')[0]}). "
            "Download it and upload it as a file instead."
        )
    html = data.decode(encoding or "utf-8", errors="replace")
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


async def prepare_page(http: httpx.AsyncClient, url: str) -> Ready:
    """A web page read and checked, ready to keep, or `Refused`."""
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
    return await prepare("url", title, body, url=url)


def network() -> httpx.AsyncBaseTransport:
    """The connections pages are read over. No connection is kept between
    requests: each one goes to the address checked for its own name, never
    reused for another name that shares the address. Tests replace it."""
    return httpx.AsyncHTTPTransport(limits=httpx.Limits(max_keepalive_connections=0))


class OutsideOnly(httpx.AsyncBaseTransport):
    """Every request, redirects included, resolved once and sent to the
    address that was checked (`public_address`), with the site's own name
    kept for its certificate and its Host header."""

    def __init__(self) -> None:
        self._inner = network()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        addr = await asyncio.to_thread(public_address, host)
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=addr),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


def http_client() -> httpx.AsyncClient:
    """The client pages are read with. Proxies set in the environment are
    ignored: a proxy would make its own connection, to an address nobody
    checked."""
    return httpx.AsyncClient(
        transport=OutsideOnly(),
        trust_env=False,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        max_redirects=10,
        timeout=httpx.Timeout(45, connect=15),
    )
