"""Model calls: chat completions against an OpenAI-compatible endpoint,
OpenRouter by default, through the openai SDK.

Every call is priced and written to the spend ledger: by the cost the provider
reports (OpenRouter's `usage.cost`), else from the endpoint's price list, else
it counts as unpriced. Errors come out as `AiError`, sorted so the caller can
tell running out of credit from a busy provider, and worded for people by
`AiError.sentence`.

The SDK makes its requests with httpx2, its own fork of httpx, so a test
transport or a caught transport error must be httpx2's too.

The response is read from its raw JSON rather than the SDK's types, as the
Rust client did: providers put cost, audio transcripts and relayed errors in
places the SDK's models do not name.
"""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

import httpx2
import openai

from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError, Kind
from opennotebook.ai.prices import Catalogue
from opennotebook.config import settings

Finish = Literal["stop", "length", "tool_calls", "content_filter"] | str


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    # USD as the provider reported it; None when it does not say.
    cost_usd: float | None = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list[ToolCall])
    usage: Usage | None = None
    finish_reason: Finish | None = None
    model: str = ""
    # The response body as it came, for what a provider puts where the SDK's
    # models do not name it: Sonar's `citations`, say.
    raw: dict[str, Any] = field(default_factory=dict[str, Any])
    # Pictures an image model made, as their encoded bytes (PNG or JPEG).
    images: list[bytes] = field(default_factory=list[bytes])


@dataclass
class TextDelta:
    """More of the answer's text."""

    text: str


@dataclass
class Done:
    """The answer is complete."""

    completion: Completion


def _finish(v: Any) -> Finish | None:
    if not isinstance(v, str):
        return None
    return {
        "end_turn": "stop",
        "max_tokens": "length",
        "function_call": "tool_calls",
        "tool_use": "tool_calls",
    }.get(v, v)


def _usage(u: Any) -> Usage | None:
    if not isinstance(u, dict):
        return None

    def tokens(k: str) -> int | None:
        v: Any = u.get(k)
        return int(v) if isinstance(v, int | float) else None

    cost: Any = u.get("cost")
    return Usage(
        input_tokens=tokens("prompt_tokens"),
        output_tokens=tokens("completion_tokens"),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
    )


def _arguments(s: Any) -> dict[str, Any]:
    if not isinstance(s, str) or not s.strip():
        return {}
    try:
        v = json.loads(s)
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _images(msg: dict[str, Any]) -> list[bytes]:
    """An image model's pictures: OpenRouter puts them beside the text, in
    `message.images[].image_url.url`, as data URLs."""
    out: list[bytes] = []
    for item in msg.get("images") or []:
        url = str(((item or {}).get("image_url") or {}).get("url") or "")
        head, _, data = url.partition(",")
        if head.startswith("data:image/") and head.endswith(";base64") and data:
            try:
                out.append(base64.b64decode(data, validate=True))
            except binascii.Error:
                continue
    return out


def parse_response(raw: dict[str, Any]) -> Completion:
    """A finished completion from its JSON body."""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        if isinstance(raw.get("error"), dict):
            raise AiError.from_body(raw["error"])
        raise AiError(Kind.DECODE, "the response has no choices")
    choice: dict[str, Any] = choices[0]
    msg: dict[str, Any] = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Some servers return content as parts even when it is all text.
        text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    else:
        # An audio model: the words are in the transcript.
        text = str((msg.get("audio") or {}).get("transcript") or "")
    calls = [
        ToolCall(
            id=str(c.get("id", "")),
            name=str((c.get("function") or {}).get("name", "")),
            arguments=_arguments((c.get("function") or {}).get("arguments")),
        )
        for c in msg.get("tool_calls") or []
        if isinstance(c, dict)
    ]
    return Completion(
        images=_images(msg),
        text=text,
        tool_calls=calls,
        usage=_usage(raw.get("usage")),
        finish_reason=_finish(choice.get("finish_reason")),
        model=str(raw.get("model", "")),
        raw=raw,
    )


class Ai:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        http: httpx2.AsyncClient | None = None,
        retries: int = 2,
        timeout: float = 300.0,
        backoff: float = 0.75,
    ) -> None:
        self.base_url = base_url
        self.has_key = bool(api_key)
        # Retries are ours, not the SDK's: out of credit must not be repeated,
        # however it is dressed up.
        self._client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "none",
            max_retries=0,
            timeout=timeout,
            http_client=http,
        )
        self._retries = retries
        self._timeout = timeout
        self._backoff = backoff
        self.catalogue = Catalogue(base_url, api_key, http)

    def _body(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        temperature: float | None,
        json_schema: tuple[str, dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not model:
            raise AiError(Kind.INVALID, "no model named")
        body: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if json_schema is not None:
            name, schema = json_schema
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            }
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        return body

    async def _post(
        self, body: dict[str, Any], *, stream: bool, more: dict[str, Any] | None = None
    ) -> Any:
        """One request through the SDK, with the retries a busy or unreachable
        provider is worth. Returns the raw HTTP response. `more` goes in the
        body as it is, for what the SDK has no parameter for."""
        # OpenRouter: report cost in `usage.cost`. Ignored elsewhere.
        extra: dict[str, Any] = {"usage": {"include": True}, **(more or {})}
        if stream:
            extra["stream_options"] = {"include_usage": True}
        attempt = 0
        while True:
            try:
                return await self._client.chat.completions.with_raw_response.create(
                    **body, stream=stream, extra_body=extra
                )
            except openai.APIStatusError as e:
                err = AiError.from_status(e.status_code, e.response.text)
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                err = AiError(Kind.UNAVAILABLE, str(e) or type(e).__name__)
            if not err.retryable or attempt >= self._retries:
                raise err
            attempt += 1
            await asyncio.sleep(self._backoff * 2 ** (attempt - 1))

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: tuple[str, dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        image_aspect: str | None = None,
    ) -> Completion:
        """Send and wait for the whole answer. With `image_aspect` ("16:9"),
        ask an image model for a picture of that shape (`Completion.images`)."""
        more: dict[str, Any] = {}
        if image_aspect is not None:
            more = {"modalities": ["image", "text"], "image_config": {"aspect_ratio": image_aspect}}
        body = self._body(
            model,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=json_schema,
            tools=tools,
            tool_choice=tool_choice,
        )
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self._post(body, stream=False, more=more)
                try:
                    raw = json.loads(resp.http_response.text)
                except ValueError as e:
                    raise AiError(Kind.DECODE, str(e)) from e
        except TimeoutError as e:
            raise AiError(Kind.UNAVAILABLE, f"no answer within {self._timeout:.0f}s") from e
        done = parse_response(raw if isinstance(raw, dict) else {})
        await self._charge(model, done.usage)
        return done

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[TextDelta | Done]:
        """Send and read the answer as it is written: text as it comes, then
        the whole completion, tool calls included."""
        body = self._body(
            model,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema=None,
            tools=tools,
            tool_choice=tool_choice,
        )
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self._post(body, stream=True)
        except TimeoutError as e:
            raise AiError(Kind.UNAVAILABLE, f"no answer within {self._timeout:.0f}s") from e
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        finish: Finish | None = None
        answered_by = ""
        finished = False
        try:
            async for line in resp.http_response.aiter_lines():
                data = line.removeprefix("data:").strip() if line.startswith("data:") else None
                if data is None:
                    # Comments (`: keep-alive`), `event:` lines, blanks.
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk: Any = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if isinstance(chunk.get("error"), dict):
                    raise AiError.from_body(chunk["error"])
                answered_by = str(chunk.get("model") or answered_by)
                choice: dict[str, Any] = (chunk.get("choices") or [{}])[0] or {}
                delta: dict[str, Any] = choice.get("delta") or {}
                # An audio model answers with `content: null` and its words in
                # `audio.transcript`; either is the answer's text.
                t = delta.get("content") or (delta.get("audio") or {}).get("transcript")
                if isinstance(t, str) and t:
                    text.append(t)
                    yield TextDelta(t)
                for c in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        int(c.get("index", 0)), {"id": "", "name": "", "args": ""}
                    )
                    slot["id"] = c.get("id") or slot["id"]
                    fn = c.get("function") or {}
                    slot["name"] = fn.get("name") or slot["name"]
                    slot["args"] += fn.get("arguments") or ""
                finish = _finish(choice.get("finish_reason")) or finish
                usage = _usage(chunk.get("usage")) or usage
            finished = True
        except httpx2.HTTPError as e:
            raise AiError(Kind.UNAVAILABLE, f"the stream broke: {e}") from e
        finally:
            await resp.http_response.aclose()
            if not finished and (text or calls or usage is not None):
                # Broken off part way, by the provider, the network or a
                # reader that stopped listening: what was written was still
                # paid for, so it goes on the ledger, unpriced unless the
                # provider already said what it cost.
                await asyncio.shield(self._charge(model, usage))
        await self._charge(model, usage)
        yield Done(
            Completion(
                text="".join(text),
                tool_calls=[
                    ToolCall(c["id"], c["name"], _arguments(c["args"]))
                    for _, c in sorted(calls.items())
                ],
                usage=usage,
                finish_reason=finish,
                model=answered_by,
            )
        )

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """One vector per text, in input order, from the endpoint's
        `/embeddings`. Charged like any other call."""
        if not texts:
            return []
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self._embed_tries(model, texts)
        except TimeoutError as e:
            raise AiError(Kind.UNAVAILABLE, f"no vectors within {self._timeout:.0f}s") from e
        data = sorted(resp.data, key=lambda d: d.index)
        if len(data) != len(texts):
            raise AiError(Kind.DECODE, f"asked for {len(texts)} vectors, got {len(data)}")
        u: Any = resp.usage.model_dump() if resp.usage else None
        await self._charge(model, _usage(u))
        return [list(d.embedding) for d in data]

    async def _embed_tries(self, model: str, texts: list[str]) -> Any:
        attempt = 0
        while True:
            try:
                return await self._client.embeddings.create(model=model, input=texts)
            except openai.APIStatusError as e:
                err = AiError.from_status(e.status_code, e.response.text)
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                err = AiError(Kind.UNAVAILABLE, str(e) or type(e).__name__)
            if not err.retryable or attempt >= self._retries:
                raise err
            attempt += 1
            await asyncio.sleep(self._backoff * 2 ** (attempt - 1))

    async def aclose(self) -> None:
        """Close the connections the client keeps open, at shutdown."""
        await self._client.close()

    async def _charge(self, model: str, usage: Usage | None) -> None:
        u = usage or Usage()
        tokens_in, tokens_out = u.input_tokens or 0, u.output_tokens or 0
        if u.cost_usd is not None:
            cost, by = u.cost_usd, "provider"
        elif usage is not None and (price := await self.catalogue.price(model)):
            cost, by = price.cost(tokens_in, tokens_out), "catalogue"
        else:
            cost, by = None, "unpriced"
        await ledger.record(
            model, input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost, priced_by=by
        )


@lru_cache
def ai() -> Ai:
    """The studio's client, configured from the environment."""
    c = settings()
    return Ai(c.ai_base_url, c.ai_key.get_secret_value())
