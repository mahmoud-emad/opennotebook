"""Every error a client sees, as a sentence a person can act on.

Errors leave the server as an HTTP status with `{"detail": "<sentence>"}`.
The wording rules used to live in the web app (`ui/src/errors.rs`) and the
player, each with its own copy; they live here now, so every client, an
outside agent included, gets readable text and shows it as it is.

Raise `Problem` for a failure the code expects: it carries the status and the
sentence. Anything else that escapes a handler is logged and answered with a
fixed sentence, never with its own text. Text from elsewhere (a provider's
reply, an exception from a library) goes through `readable` first.
"""

import json
import logging
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

log = logging.getLogger(__name__)


class Problem(Exception):
    """An expected failure: what went wrong and what to do, for a person."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def not_found(what: str) -> Problem:
    return Problem(404, f"{what} is no longer there. Reload the page to see what is.")


# Said for an exhausted AI account; where to add credit depends on whose
# account it is (`credit_sentence`).
OUT_OF_CREDIT = "The AI account is out of credit, so nothing can be read or made right now."


def credit_sentence() -> str:
    """Out of credit, with where to add it: the account of the provider the
    studio mostly calls, OpenRouter's page when it is OpenRouter."""
    try:
        from opennotebook.ai import connections

        primary = next((c for c in connections.snapshot() if c.usable), None)
    except Exception:
        # Settings that cannot be read (no DATABASE_URL in a script, say)
        # must not turn one error into another.
        primary = None
    if primary is not None and primary.kind == "openrouter":
        return f"{OUT_OF_CREDIT} Add credit at openrouter.ai/settings/credits, then try again."
    if primary is not None:
        return f"{OUT_OF_CREDIT} Add credit to the {primary.label} account, then try again."
    return f"{OUT_OF_CREDIT} Add credit with the AI provider the studio uses, then try again."


# The failures with a fixed wording, and the words that give each away. No
# wording contains its own or an earlier kind's keys under a different kind,
# so cleaning twice gives the same sentence.
KNOWN: list[tuple[tuple[str, ...], str]] = [
    (("no sources",), "Add a source first: a link, a note, or a topic to research."),
    (
        (
            "out of credit",
            "insufficient credit",
            "insufficient_quota",
            "credit balance",
            "more credits",
            "quota exhausted",
            "exceeded your current quota",
            "http 402",
        ),
        OUT_OF_CREDIT,
    ),
    (
        (
            "refused the key",
            "invalid api key",
            "no auth credentials",
            "missing authentication",
            "http 401",
        ),
        "The AI provider refused the studio's API key. Check it in Settings › AI providers, "
        "then try again.",
    ),
    (
        ("rate limited", "too many requests", "http 429"),
        "The AI provider is busy right now. Wait a moment and try again.",
    ),
    (
        ("is the model id right", "model not found", "no endpoints found", "not a valid model"),
        "The AI model chosen in Settings is not available. Pick another one in Settings › Models.",
    ),
    (
        (
            "provider is unavailable",
            "upstream",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
        ),
        "The AI provider is not answering right now. Try again in a minute.",
    ),
]

# The longest message shown whole; past it, cut at a word with "…".
MAX_CHARS = 220

FALLBACK = "Something went wrong. Try again."
SERVER_FAULT = (
    "The studio ran into a problem. Try again; if it keeps happening, restart the studio."
)


def known(raw: str) -> str | None:
    """The fixed sentence for a failure people meet often, if `raw` is one."""
    low = raw.lower()
    for keys, say in KNOWN:
        if any(k in low for k in keys):
            return credit_sentence() if say == OUT_OF_CREDIT else say
    return None


def readable(raw: str) -> str:
    """`raw` as a person should read it."""
    if say := known(raw):
        return say
    if m := re.fullmatch(r"\s*HTTP (\d{3})\s*", raw):
        code = int(m.group(1))
        if code == 404:
            return "That is no longer there. Reload the page and try again."
        if code in (408, 504):
            return "The studio took too long to answer. Try again."
        if 500 <= code <= 599:
            return SERVER_FAULT
        return "The studio refused that request. Reload the page and try again."
    return tidy(raw)


def tidy(raw: str) -> str:
    """Any message made presentable without changing what it says: no JSON, no
    debug output, whitespace collapsed, a capital to start, a full stop to
    end, and cut short when it runs on."""
    msg = _without_json(raw)
    # `Os { code: 2, kind: NotFound, .. }` and the like: a type's debug form,
    # its name included.
    if (i := msg.find(" { ")) >= 0:
        head = msg[:i].rstrip()
        before, _, name = head.rpartition(" ")
        if before and name[:1].isupper():
            head = before
        msg = head.rstrip(": ")
    s = " ".join(msg.split())
    if not s:
        return FALLBACK
    if len(s) > MAX_CHARS:
        cut = s[:MAX_CHARS]
        cut = cut.rpartition(" ")[0] or cut
        s = cut.rstrip(",;: ") + "…"
    s = s[0].upper() + s[1:]
    if s[-1].isalnum() or s[-1] in ")`":
        s += "."
    return s


def _without_json(msg: str) -> str:
    """A message without the raw JSON a provider's reply can bring along: its
    `message` when one can be read out of it, else the words before it."""
    i = msg.find("{")
    if i < 0:
        return msg.strip()
    said: str | None = None
    try:
        v: Any = json.loads(msg[i:])
        if isinstance(v, dict):
            err: Any = v.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                said = err["message"]
            elif isinstance(v.get("message"), str):
                said = v["message"]
    except ValueError:
        pass
    before = msg[:i].strip().rstrip(":").strip()
    if said is not None:
        return f"{before}: {said}" if before else said
    # Not JSON: a type's debug form, `Os { code: 28, .. }`, whose name goes
    # with its braces.
    head, _, name = before.rpartition(" ")
    if head and name[:1].isupper() and name.isalnum():
        return head.rstrip(": ")
    return before


# ── HTTP ──────────────────────────────────────────────────────────────────────


def _field(loc: tuple[int | str, ...]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
    return " › ".join(parts).replace("_", " ") or "the request"


def validation_sentence(errors: list[dict[str, Any]]) -> str:
    """What a refused request got wrong, in one sentence: the first field and
    what it needs."""
    if not errors:
        return "Some of what was sent is not valid. Check it and try again."
    e = errors[0]
    field = _field(tuple(e.get("loc", ())))
    if e.get("type") == "missing":
        return f"Fill in {field}, then try again."
    if e.get("type") == "json_invalid":
        return "The request could not be read. Reload the page and try again."
    msg = str(e.get("msg", "is not valid")).removeprefix("Value error, ")
    return tidy(f"{field.capitalize()}: {msg[0].lower() + msg[1:] if msg else msg}")


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(_: Request, e: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        assert isinstance(e, Problem)
        return JSONResponse({"detail": e.detail}, status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        assert isinstance(e, RequestValidationError)
        return JSONResponse({"detail": validation_sentence(list(e.errors()))}, status_code=422)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, e: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        assert isinstance(e, HTTPException)
        # A sentence written for people is kept; Starlette's "Not Found" and
        # the like say less than the status does.
        detail = str(e.detail or "")
        said = detail if detail.endswith(".") else readable(f"HTTP {e.status_code}")
        return JSONResponse({"detail": said}, status_code=e.status_code, headers=e.headers)

    @app.exception_handler(Exception)
    async def _fault(request: Request, e: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=e)
        return JSONResponse({"detail": SERVER_FAULT}, status_code=500)
