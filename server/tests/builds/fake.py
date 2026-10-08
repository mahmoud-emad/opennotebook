"""Stand-ins for a build's services: a model that answers every step of the
prep from what it is asked, and a speech server that answers every line with
a WAV whose length follows the text. Nothing leaves the process.

The model answers by the kind of request, not by a queue of replies, because
a build asks in an order that depends on timing: the Q&A calls and the slide
batches run side by side.
"""

import asyncio
import base64
import functools
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


@functools.cache
def _picture() -> bytes:
    """A small painted picture, as an image model sends one: two soft bands."""
    import skia

    surface = skia.Surface(320, 180)
    c = surface.getCanvas()
    c.clear(skia.ColorSetRGB(0xE8, 0xEE, 0xF4))
    c.drawRect(
        skia.Rect.MakeXYWH(0, 90, 320, 90), skia.Paint(Color=skia.ColorSetRGB(0xB7, 0xCF, 0xE3))
    )
    data = surface.makeImageSnapshot().encodeToData()
    assert data is not None
    return bytes(data)


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
        # A whiteboard: whether the plan is usable, and how many answers a
        # scene gets wrong before a right one (a large number: never right).
        self.plan_ok = True
        self.scene_wrong = 0
        # The scene check: how many of a scene's checks fail before one
        # passes, and whether the checker answers at all.
        self.check_fail = 0
        self.check_down = False
        # An illustrated theme: whether the picture check finds lettering.
        self.picture_lettering = False
        # How many times each scene was checked and written, by its first
        # line; a test clears the tries to start a render's scenes afresh.
        self._checks: dict[str, int] = {}
        self.scene_tries: dict[str, int] = {}

    def kind_of(self, body: dict[str, Any]) -> str:
        msgs = body.get("messages") or []
        # An illustrated theme's picture, and its check (a picture, no system).
        if "image" in (body.get("modalities") or []):
            return "video_picture"
        first = next((m["content"] for m in msgs if m["role"] == "user"), "")
        if isinstance(first, list) and any(
            str(p.get("text", "")).startswith("You check a picture made") for p in first
        ):
            return "video_picture_check"
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
        if system.startswith("You plan a whiteboard explainer video."):
            return "video_plan"
        if system.startswith("You draw one scene of a whiteboard explainer video."):
            return "video_scene"
        if system.startswith("You check one scene of a whiteboard explainer video"):
            return "video_check"
        if system.startswith("You are the teacher who will say this lesson on camera"):
            return "video_presenter"
        if system.startswith("You are the tutor beside a video lesson"):
            return "video_explain"
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
        elif kind == "video_plan":
            line_ids = re.findall(r"^(\S+) \(\d+\.\d s\):", user, re.M)
            scenes = [line_ids[i : i + 2] for i in range(0, len(line_ids), 2)]
            if not self.plan_ok:
                scenes = [line_ids[1:]]
            text = json.dumps(
                {
                    "scenes": [
                        {"lines": sc, "layout": "flow", "title": f"Scene {i}",
                         "brief": "a server and a box", "concepts": ["server", "kernel"]}
                        for i, sc in enumerate(scenes)
                    ]
                }
            )  # fmt: skip
        elif kind == "video_scene":
            text = self._scene(user)
        elif kind == "video_check":
            text = self._check(body)
        elif kind == "video_presenter":
            # Each part's lines as one paragraph: the same words, said whole.
            blocks = re.split(r"^## Part (\d+):.*$", user, flags=re.M)[1:]
            titles = re.findall(r"^## Part \d+: (.*)$", user, flags=re.M)
            text = json.dumps(
                {
                    "opening": ["Here is what this covers, part by part."],
                    "parts": [
                        {"ordinal": int(n), "paragraphs": [" ".join(b.strip().splitlines())]}
                        for n, b in zip(blocks[::2], blocks[1::2], strict=True)
                    ],
                    "closing": ["That is the whole idea. Thanks for watching."],
                    "about": "What the kernel does.",
                    "agenda": titles,
                    "takeaways": ["The kernel manages memory."],
                }
            )
        elif kind == "video_explain":
            # A citation of a passage and one of none; a moment in the video
            # and one past its end.
            text = "The kernel manages memory [1][9]. It was said at [0:01], not at [99:00]."
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

    def _check(self, body: dict[str, Any]) -> str:
        if self.check_down:
            return "I cannot look at pictures today."
        msgs = body.get("messages") or []
        user = next(m["content"] for m in msgs if m["role"] == "user")
        text = next(p["text"] for p in user if p.get("type") == "text")
        assert any(p.get("type") == "image_url" for p in user), "the board is sent as a picture"
        claims = re.findall(r"^- (.+)$", text, re.M)
        key = text.split("\n")[1]
        n = self._checks[key] = self._checks.get(key, 0) + 1
        ok = n > self.check_fail
        return json.dumps(
            {
                "claims": [{"claim": c, "supported": ok, "why": "" if ok else "not said"}
                           for c in claims],
                "missing": [], "unreadable": [],
                "matches_narration": True, "fixes": [] if ok else ["say only what is said"],
            }
        )  # fmt: skip

    def _scene(self, user: str) -> str:
        line = re.findall(r"^(\S+): \[0\]", user, re.M)[0]
        tries = self.scene_tries[line] = self.scene_tries.get(line, 0) + 1
        target = "box" if tries > self.scene_wrong else "nowhere"
        icon = (re.findall(r"^- server: (\S+?)[,\n]", user + "\n", re.M) or ["server"])[0]
        # Labels from the narration's own words, as the rules ask.
        numbered = re.findall(rf"^{re.escape(line)}: (.*)$", user, re.M)[0].split()
        said = [re.sub(r"^\[\d+\]", "", w.strip(".,;:!?\"'")) for w in numbered]
        words = [w for w in said if len(w) > 3] or ["thing", "other"]
        first, second = words[0], words[min(1, len(words) - 1)]
        return json.dumps(
            {
                "title": "",
                "layout": "flow",
                "elements": [
                    {"id": "srv", "kind": "icon", "icon": icon, "at": "A2", "span": [2, 3],
                     "label": first, "tone": "blue", "beat": {"line": line, "word": 0}},
                    {"id": "box", "kind": "box", "at": "E2", "span": [2, 3], "label": second,
                     "beat": {"line": line, "word": 1}},
                    {"id": "a", "kind": "arrow", "from": "srv", "to": target,
                     "beat": {"line": line, "word": 1}},
                ],
                "highlight": [{"target": "box", "beat": {"line": line, "word": 0}}],
            }
        )  # fmt: skip

    async def answer(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json=CATALOGUE)
        body = json.loads(request.content)
        kind = self.kind_of(body)
        self.asked.append(kind)
        self.bodies.append(body)
        if kind in self.hold:
            await self.hold[kind].wait()
        if kind in self.fail:
            return httpx2.Response(self.fail[kind], json={"error": {"message": "down"}})
        if kind == "video_picture":
            url = "data:image/png;base64," + base64.b64encode(_picture()).decode()
            message = {"content": "", "images": [{"type": "image_url", "image_url": {"url": url}}]}
            return _answered(body, message, completion_tokens=1290, cost=0.04)
        if kind == "video_picture_check":
            verdict = {"lettering": self.picture_lettering, "contradicts": False, "why": ""}
            return _answered(body, {"content": json.dumps(verdict)}, completion_tokens=20)
        _, r = self.reply(body)
        return _answered(body, {"content": r["text"]}, **r["extra"])


def _answered(
    body: dict[str, Any],
    message: dict[str, Any],
    completion_tokens: int = 50,
    cost: float = 0.001,
    **extra: Any,
) -> httpx2.Response:
    """A chat completion that answers `body` with `message`, and its usage."""
    return httpx2.Response(
        200,
        json={
            "model": body.get("model", ""),
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens, "cost": cost},
            **extra,
        },
    )


class Voice:
    """The speech server: every line comes back as 24 kHz mono, about 60 ms
    a word."""

    def __init__(self) -> None:
        self.said: list[dict[str, Any]] = []
        self.status = 200
        # Like Speaches by default: no captioned route. True answers it as
        # Kokoro-FastAPI does, every word timed.
        self.timestamps = False

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captioned = request.url.path.endswith("/dev/captioned_speech")
        if captioned and not self.timestamps:
            return httpx.Response(404, json={"detail": "Not Found"})
        self.said.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "Voice not found"})
        words = body["input"].split()
        frames = 24_000 * 60 * max(len(words), 1) // 1000
        audio = ramp_wav(24_000, 1, frames)
        if not captioned:
            return httpx.Response(200, content=audio)
        stamps = [
            {"word": w, "start_time": i * 0.06, "end_time": (i + 1) * 0.06}
            for i, w in enumerate(words)
        ]
        return httpx.Response(
            200,
            json={"audio": base64.b64encode(audio).decode(), "timestamps": stamps},
        )


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
    # Which servers lack timings is remembered per process; each test starts
    # from not knowing.
    monkeypatch.setattr(speech, "_UNTIMED", set[str]())
    return studio, voice


def tools_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take ffmpeg and ffprobe as installed, for a render that is asked for
    but never run."""
    from opennotebook.build import video

    def found(key: str, name: str) -> str:
        return name

    monkeypatch.setattr(video, "tool", found)
