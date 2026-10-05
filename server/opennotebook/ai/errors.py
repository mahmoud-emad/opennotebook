"""Why a model call did not come back, sorted into the failures that need
different handling: out of credit, a refused key, a rate limit, a wrong model,
an unavailable provider, an answer that could not be read, a refused request.

Ported from `opennotebook_ai/src/error.rs`, including the unwrapping of a
provider's error that OpenRouter relays inside its own.
"""

import json
from enum import StrEnum
from typing import Any

from opennotebook.errors import readable


class Kind(StrEnum):
    QUOTA = "quota"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    DECODE = "decode"
    INVALID = "invalid"


# The words each kind is said with. `errors.readable` recognises them and
# turns them into the sentence a person sees.
_SAID = {
    Kind.QUOTA: "the AI account is out of credit",
    Kind.RATE_LIMITED: "rate limited",
    Kind.AUTH: "the AI provider refused the key",
    Kind.NOT_FOUND: "not found (is the model id right?)",
    Kind.UNAVAILABLE: "the AI provider is unavailable",
    Kind.DECODE: "the AI provider's answer could not be read",
    Kind.INVALID: "the request was refused",
}


class AiError(Exception):
    def __init__(self, kind: Kind, detail: str) -> None:
        super().__init__(f"{_SAID[kind]}: {detail}")
        self.kind = kind
        self.detail = detail

    @property
    def retryable(self) -> bool:
        """Worth repeating unchanged."""
        return self.kind in (Kind.RATE_LIMITED, Kind.UNAVAILABLE)

    @property
    def sentence(self) -> str:
        """What to tell a person."""
        return readable(str(self))

    @classmethod
    def from_status(cls, status: int, body: str) -> AiError:
        detail = _detail(status, body)
        # Out of credit wins over the status it came with: relayed through a
        # gateway it can arrive as a 5xx, or inside a 200 with a string code.
        # Not over 401 (a bad key) or 429: a per-minute rate limit is often
        # worded "quota exceeded" and clears by itself.
        if status not in (401, 429) and (_looks_like_credit(body) or _looks_like_credit(detail)):
            return cls(Kind.QUOTA, detail)
        if status == 402:
            return cls(Kind.QUOTA, detail)
        if status in (401, 403):
            # Some providers answer an exhausted balance with 403.
            return cls(Kind.QUOTA if _looks_like_credit(detail) else Kind.AUTH, detail)
        if status == 404:
            return cls(Kind.NOT_FOUND, detail)
        if status in (408, 429):
            return cls(Kind.RATE_LIMITED, detail)
        if 500 <= status <= 599:
            return cls(Kind.UNAVAILABLE, detail)
        return cls(Kind.INVALID, detail)

    @classmethod
    def from_body(cls, error: dict[str, Any]) -> AiError:
        """The error a 200 whose body is `{"error": {...}}` comes to. OpenRouter
        answers some upstream failures this way. `code` may be a number or a
        string; a string one says nothing about the status."""
        code = error.get("code")
        status = code if isinstance(code, int) and 100 <= code < 600 else 502
        return cls.from_status(status, json.dumps({"error": error}))


def _detail(status: int, body: str) -> str:
    """The provider's own message when the body has one, else the body itself."""
    msg = (message_of(body) or body[:300]).strip()
    return f"HTTP {status}: {msg}" if msg else f"HTTP {status}"


def message_of(body: str) -> str | None:
    """The innermost human message in an error body, if it has one.

    A relayed upstream error nests: OpenRouter's `error.message` can itself be
    the upstream's JSON body, so the message is unwrapped until it is prose.
    """
    text, found = body.strip(), None
    for _ in range(4):
        try:
            v: Any = json.loads(text)
        except ValueError:
            break
        if not isinstance(v, dict):
            break
        err: Any = v.get("error")
        inner: Any = None
        if isinstance(err, dict):
            inner = err.get("message")
            if not isinstance(inner, str):
                meta: Any = err.get("metadata")
                inner = meta.get("raw") if isinstance(meta, dict) else None
        elif isinstance(err, str):
            inner = err
        if not isinstance(inner, str):
            inner = v.get("message")
        if not isinstance(inner, str):
            break
        text = found = inner.strip()
    return found if found is not None and not found.startswith("{") else None


def _looks_like_credit(detail: str) -> bool:
    d = detail.lower()
    return any(
        k in d
        for k in (
            "insufficient credit",
            "insufficient_quota",
            "out of credit",
            "credit balance",
            "quota",
        )
    )
