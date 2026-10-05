"""Stand-ins for a build's services: a model that answers every step of the
prep from what it is asked, and a speech server that answers every line with
a WAV whose length follows the text. Nothing leaves the process.

The model answers by the kind of request, not by a queue of replies, because
a build asks in an order that depends on timing: the Q&A calls and the slide
batches run side by side.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from opennotebook import speech, storage
from opennotebook.ai import client
from opennotebook.ai.client import Ai
from opennotebook.speech.wav import ramp_wav

# Every model the build defaults to, priced, so a build's spend is known.
PRICED = [
    "anthropic/claude-haiku-4.5",
    "openai/gpt-4o-mini",
    "google/gemini-2.5-flash-lite",
    "perplexity/sonar",
    "anthropic/claude-sonnet-5.5",
    "amazon/nova-micro-v1",
]
CATALOGUE = {
    "data": [{"id": m, "pricing": {"prompt": "0.000001", "completion": "0.000005"}} for m in PRICED]
}


def _speaker_ids(system: str) -> list[str]:
    """The ids a prompt lists under "Speakers, by id:"."""
    at = system.find("Speakers, by id:\n")
    if at < 0:
        return ["host"]
    ids: list[str] = []
    for line in system[at + len("Speakers, by id:\n") :].splitlines():
        m = re.match(r"\s+(\S+) \(", line)
        if not m:
            break
        ids.append(m.group(1))
    return ids or ["host"]


def _lines(ids: list[str], n: int, about: str) -> str:
    said = [
        f"Here is a full sentence about {about}, with the detail the material gives.",
        f"And that detail matters because it explains {about} in plain terms.",
        "The material puts a number on it, and the number is what to remember.",
    ]
    return "\n".join(f"{ids[i % len(ids)]}: {said[i % len(said)]}" for i in range(n))


class Studio:
    """The model. Counts what it was asked, by kind, and can be told to
    wait or fail at a step."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        # A step that waits for this before it answers, by kind.
        self.hold: dict[str, asyncio.Event] = {}
        # Steps that answer with this HTTP status instead.
        self.fail: dict[str, int] = {}
        self.slide_docs = True
        self.running = 0
        self.most_at_once = 0

    def kind_of(self, body: dict[str, Any]) -> str:
        msgs = body.get("messages") or []
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        user = next((m["content"] for m in msgs if m["role"] == "user"), "")
        # Markers that survive an audio overview's rewording of the prompts.
        if "question-and-answer pairs" in system:
            return "qa"
        if "Return exactly" in system:
            return "outline"
        if "=== PART <number> ===" in system:
            return "session"
        if "Carry it on from its last line" in system:
            return "extend"
        if "explaining THIS" in system:
            return "slide_script"
        if "Write the OPENING" in system or "Write the CLOSING" in system:
            return "bookend"
        if system.startswith("You are the editor"):
            return "edit"
        if "design presentation slides as HTML" in system:
            return "slides"
        if system.startswith("You plan web research"):
            return "research_plan"
        if system.startswith("You write a research report"):
            return "report"
        if user.startswith("Search the web"):
            return "search"
        return "other"

    def reply(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = self.kind_of(body)
        msgs = body.get("messages") or []
        system = next((m["content"] for m in msgs if m["role"] == "system"), "")
        user = next((m["content"] for m in msgs if m["role"] == "user"), "")
        ids = _speaker_ids(system)
        extra: dict[str, Any] = {}
        if kind == "qa":
            doc = user[user.find("<document>\n") + len("<document>\n") :]
            first = doc.split(".")[0].strip()
            text = json.dumps(
                {
                    "pairs": [
                        {
                            "question": f"What does the source say first? {first[:40]}",
                            "answer": first + ".",
                            "evidence": first,
                        }
                    ]
                }
            )
        elif kind == "outline":
            m = re.search(r"Return exactly (\d+) parts", system)
            assert m is not None
            n = int(m.group(1))
            text = "\n".join(
                f"Part title number {i + 1}\n- point one of {i + 1}\n- point two of {i + 1}"
                for i in range(n)
            )
        elif kind == "session":
            m = re.search(r"cut into the same (\d+) parts", system)
            assert m is not None
            n = int(m.group(1))
            text = "\n".join(
                f"=== PART {i + 1} ===\n{_lines(ids, 4, f'part {i + 1}')}\nSLIDE:\n"
                f"Layout: two columns\nPoint: point {i + 1}\nStat: {i + 10} — a figure"
                for i in range(n)
            )
        elif kind in ("extend", "slide_script"):
            text = _lines(ids, 3, "this slide") + "\nSLIDE:\nLayout: text only\nPoint: a point"
        elif kind == "bookend":
            text = _lines(ids, 2, "the session")
        elif kind == "edit":
            text = user[user.find("\n", user.rfind("DRAFT OF PART")) + 1 :]
        elif kind == "slides":
            n = len(re.findall(r"=== SLIDE \d+ ===", user))
            titles = re.findall(r"Title: (.*)", user)
            text = (
                "".join(
                    f"<!-- SLIDE {i + 1} -->\n<!doctype html><html><head></head><body>"
                    f'<h1 class="k-title">{titles[i]}</h1><ul class="k-points"><li>p</li></ul>'
                    "</body></html>\n"
                    for i in range(n)
                )
                if self.slide_docs
                else "no slides today"
            )
        elif kind == "research_plan":
            text = "first query\nsecond query"
        elif kind == "search":
            text = "An answer."
            extra = {"citations": ["https://example.org/a", "https://example.org/b"]}
        elif kind == "report":
            text = "## What it is\n\nIt is a thing [1]."
        else:
            text = "ok"
        return kind, {"text": text, "extra": extra}

    async def answer(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json=CATALOGUE)
        body = json.loads(request.content)
        kind = self.kind_of(body)
        self.asked.append(kind)
        self.bodies.append(body)
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            if kind in self.hold:
                await self.hold[kind].wait()
            if kind in self.fail:
                return httpx2.Response(self.fail[kind], json={"error": {"message": "down"}})
            _, r = self.reply(body)
            return httpx2.Response(
                200,
                json={
                    "model": body.get("model", ""),
                    "choices": [{"message": {"content": r["text"]}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
                    **r["extra"],
                },
            )
        finally:
            self.running -= 1


class Voice:
    """The speech server: every line comes back as 24 kHz mono, about 60 ms
    a word."""

    def __init__(self) -> None:
        self.said: list[dict[str, Any]] = []
        self.status = 200

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.said.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "Voice not found"})
        frames = 24_000 * 60 * max(len(body["input"].split()), 1) // 1000
        return httpx.Response(200, content=ramp_wav(24_000, 1, frames))


def install(monkeypatch: pytest.MonkeyPatch, files: Path) -> tuple[Studio, Voice]:
    """Point the studio's model client, speech client and files volume at
    stand-ins."""
    studio, voice = Studio(), Voice()
    ai = Ai(
        "http://ai.test/v1",
        "k",
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(studio.answer)),
        backoff=0,
        retries=0,
    )
    monkeypatch.setattr(client, "ai", lambda: ai)
    http = httpx.AsyncClient(transport=httpx.MockTransport(voice.answer))
    monkeypatch.setattr(
        speech, "speech", lambda: speech.Speech("http://tts.test/v1", "", "kokoro", "", http=http)
    )
    monkeypatch.setattr(storage, "_root", lambda: files)
    return studio, voice
