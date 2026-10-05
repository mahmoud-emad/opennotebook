"""Web search and deep research. Ports of `web_search` in
`opennotebook_server/src/agent.rs` and of `research.rs`.

A person who asks to understand "the Mochi paper" should not have to go and
find it first. Research searches and reads the web on the person's own
words, and the report is added as one more source beside whatever the person
added. The script and the deck then treat it like any other document.

Four steps, each a plain call:

1. a model turns the topic into a few focused search queries;
2. each query goes through `web_search`;
3. the best distinct pages are read with the same fetcher a pasted link uses;
4. a model writes a report from those pages, citing each claim by number.

The depth setting decides how wide it goes: `quick` reads a handful of
pages, `standard` about a dozen. A run is minutes, so it never runs inside a
request's transaction: it runs as a job (asked for on its own, or as the
first step of a build), or in a chat turn, which streams its progress and
holds no transaction while it does.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.domain import settings as st
from opennotebook.domain import sources

log = logging.getLogger(__name__)

# Results one search hands back.
MAX_RESULTS = 8

# How long a research run may take before it gives up. Past this the work
# carries on without it rather than be held hostage to one search.
DEADLINE_SECONDS = 15 * 60

# Characters of each page the report is written from. A dozen pages at this
# size is well inside a small model's window.
PAGE_CHARS = 8_000

# Readable characters below which a page is not worth a place in the report.
MIN_PAGE_CHARS = 200


@dataclass(frozen=True)
class Hit:
    """One web search result."""

    title: str
    url: str
    snippet: str


class ResearchError(Exception):
    """A research run that found nothing to report, in a sentence."""

    def __init__(self, detail: str, sentence: str) -> None:
        super().__init__(detail)
        self.sentence = sentence


def short_host(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    return rest.split("/", 1)[0].removeprefix("www.")


def citations(raw: dict[str, Any], answer: str) -> list[Hit]:
    """The cited URLs out of a Sonar response: the top-level `citations`
    array, or the message's `url_citation` annotations, whichever it used.
    The answer's text rides along on the first hit as its snippet."""
    hits: list[Hit] = []
    cited: Any = raw.get("citations")
    for u in cited if isinstance(cited, list) else []:
        if isinstance(u, str) and u.startswith("http"):
            hits.append(Hit(short_host(u), u, ""))
    if not hits:
        choices: Any = raw.get("choices")
        msg: Any = (
            (choices[0] or {}).get("message") if isinstance(choices, list) and choices else {}
        )
        notes: Any = msg.get("annotations") if isinstance(msg, dict) else None
        for a in notes if isinstance(notes, list) else []:
            c: Any = a.get("url_citation") if isinstance(a, dict) else None
            if isinstance(c, dict) and isinstance(c.get("url"), str):
                title = c.get("title")
                hits.append(
                    Hit(title if isinstance(title, str) else short_host(c["url"]), c["url"], "")
                )
    seen: set[str] = set()
    out: list[Hit] = []
    for h in hits:
        if h.url not in seen:
            seen.add(h.url)
            out.append(h)
    out = out[:MAX_RESULTS]
    if out:
        out[0] = Hit(out[0].title, out[0].url, answer[:600])
    return out


async def web_search(model: str, query: str) -> list[Hit]:
    """Search the web through Perplexity Sonar on the AI endpoint.

    Not DuckDuckGo: its HTML endpoint answered for a handful of queries and
    then returned nothing to anyone, the bot wall other scrapers have
    documented too. Sonar goes through the same key and the same client as
    every other model call, at about half a cent a search, and its answer
    text comes back too.
    """
    query = query.strip()
    if not query:
        return []
    done = await client.ai().complete(
        model,
        [
            {
                "role": "user",
                "content": f"Search the web for the most useful, authoritative sources on: "
                f"{query}\nAnswer in two sentences and cite your sources.",
            }
        ],
    )
    return citations(done.raw, done.text)


def breadth(depth: str) -> tuple[int, int]:
    """How wide a run goes: queries planned, pages read."""
    return (3, 5) if depth == "quick" else (5, 12)


def parse_queries(text: str, n: int) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        q = line.strip().lstrip("0123456789.)-*").strip().strip('"').strip()
        if len(q) >= 3 and not any(o.lower() == q.lower() for o in out):
            out.append(q)
    return out[:n]


async def plan(model: str, topic: str, n: int) -> list[str]:
    """Search queries for the topic. Falls back to the topic itself, so a
    planner that answers badly costs breadth, not the run."""
    try:
        done = await client.ai().complete(
            model,
            [
                {
                    "role": "system",
                    "content": f"You plan web research. Given what a person wants to understand, "
                    f"reply with {n} distinct, focused web search queries that together cover it: "
                    "the primary source first (the paper, the official docs, the project page), "
                    "then explanations and context. One query per line, no numbering, no quotes, "
                    "nothing else.",
                },
                {"role": "user", "content": topic},
            ],
            max_tokens=300,
        )
        queries = parse_queries(done.text, n)
    except AiError as e:
        log.info("research planning failed: %s", e)
        queries = []
    return queries or [topic[:200]]


def document(topic: str, report: str, found: list[tuple[str, str]]) -> str:
    """The report as a source: what it is, the report, and where it came
    from."""
    out = (
        f"# Web research: {topic}\n\nSource: web research on the topic above, gathered for this "
        f"collection.\n\n{report}\n"
    )
    if found:
        out += "\n## Sources read\n\n" + "".join(
            f"{i + 1}. [{title}]({url})\n" for i, (title, url) in enumerate(found)
        )
    return out


async def write_report(
    model: str, topic: str, pages: list[tuple[str, str, str]], language_rule: str
) -> str:
    """The report, written from the pages and citing them as `[n]`."""
    material = "".join(
        f"[{i + 1}] {title} ({url})\n{text}\n\n" for i, (title, url, text) in enumerate(pages)
    )
    done = await client.ai().complete(
        model,
        [
            {
                "role": "system",
                "content": "You write a research report for someone studying a topic, from "
                "numbered web pages. Use only what the pages say. Cite every claim with the "
                "number of the page it comes from, like [2]. Organise it under Markdown "
                "headings: what it is, how it works, why it matters, and open questions or "
                "disagreements between sources. Be thorough and specific: names, numbers, "
                "mechanisms. No preamble, and no list of sources at the end; that is added for "
                f"you. {language_rule}",
            },
            {"role": "user", "content": f"Topic: {topic}\n\nPages:\n\n{material}"},
        ],
        max_tokens=6000,
    )
    if not done.text.strip():
        raise ResearchError(
            "the report came back empty",
            "The research report came back empty. Try again, or pick another notes model in "
            "Settings › Models.",
        )
    return done.text


@dataclass(frozen=True)
class Found:
    title: str
    text: str
    sources: int


Say = Callable[[str], Awaitable[None]]


async def _quiet(_: str) -> None:
    return None


async def gather(values: dict[str, str], topic: str, say: Say = _quiet) -> Found:
    """Research `topic` on the web and return the report as a document.

    `values` are the person's settings in force (`settings.values`). An
    error is raised rather than swallowed, and the caller decides whether it
    matters."""
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            return await _run(values, topic, say)
    except TimeoutError as e:
        raise ResearchError(
            f"web research did not finish in {DEADLINE_SECONDS // 60} minutes",
            "Web research took too long and was stopped. Try a narrower topic.",
        ) from e


async def _run(values: dict[str, str], topic: str, say: Say) -> Found:
    n_queries, n_pages = breadth(st.research_tier(values[st.RESEARCH_DEPTH_KEY]))
    await say("Planning searches")
    queries = await plan(values[st.CHAT_MODEL_KEY], topic, n_queries)
    # Every distinct url, in the order the searches ranked them, so the first
    # query's best results are read first.
    urls: list[str] = []
    for q in queries:
        await say(f"Searching: {q}")
        try:
            hits = await web_search(values[st.SEARCH_MODEL_KEY], q)
        except AiError as e:
            log.info("research search %r: %s", q, e)
            continue
        urls.extend(h.url for h in hits if h.url not in urls)
    if not urls:
        raise ResearchError(
            f"the web searches found nothing on {topic!r}",
            "The web searches found nothing on that topic. Try wording it differently.",
        )
    pages: list[tuple[str, str, str]] = []
    async with sources.http_client() as http:
        for url in urls:
            if len(pages) >= n_pages:
                break
            await say(f"Reading {short_host(url)}")
            try:
                page = await sources.read_page(http, url)
            except sources.Refused as e:
                log.info("research %s: %s", url, e)
                continue
            if sum(1 for c in page.text if not c.isspace()) < MIN_PAGE_CHARS:
                continue
            if sources.is_refusal(page.title, page.text):
                continue
            title = sources.one_line(page.title) or short_host(url)
            pages.append((title, url, page.text[:PAGE_CHARS]))
    if not pages:
        raise ResearchError(
            f"none of the pages found on {topic!r} could be read",
            "None of the pages found on that topic could be read. Try wording it differently, "
            "or add the pages you know as links.",
        )
    await say(f"Writing the report from {len(pages)} pages")
    report = await write_report(
        values[st.NOTES_MODEL_KEY], topic, pages, st.language_rule(values[st.LANGUAGE_KEY])
    )
    body = document(topic, report.strip(), [(t, u) for t, u, _ in pages])
    return Found(f"Web research: {sources.one_line(topic)}"[:200], body, len(pages))
