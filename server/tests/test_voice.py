"""Asking aloud: the voice turn. Ported from the tests of
`opennotebook_server/src/ask.rs`, with the route's own: the silence gate in
front of the model, the events the player reads, and who may ask."""

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any

import httpx2
import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import speech, storage
from opennotebook.ai import client as ai_client
from opennotebook.ai.client import Ai
from opennotebook.db.models import Collection, Session
from opennotebook.db.session import engine, sessionmaker
from opennotebook.domain import settings as st
from opennotebook.domain import voice
from opennotebook.domain.sessions import Line, Part, Speaker
from opennotebook.domain.voice import At, Narration
from opennotebook.speech import banter
from opennotebook.speech.wav import ramp_wav
from tests.conftest import other_person
from tests.test_vad import silence, voiced, wav16

# ── reading the answer as it streams ──────────────────────────────────────────


def test_reply_audio_is_pcm16_at_24k() -> None:
    """The rate is a contract with the player: the reply arrives as
    headerless PCM and the page builds an AudioBuffer at exactly this rate.
    Wrong by a factor and the answer plays chipmunked or slurred, which is a
    bug that sounds like a model problem."""
    assert voice.REPLY_RATE == 24_000
    chunks = voice.pcm16_chunks(ramp_wav(24_000, 1, 10_000))
    assert [len(c) for c in chunks] == [9_600, 9_600, 800], "200 ms frames of PCM16"
    assert voice.pcm16_chunks(ramp_wav(22_050, 1, 1_000)) == [], "another rate is not played"


def test_a_streamed_answer_comes_out_as_typed_parts_in_order() -> None:
    """The three parts come out in speaking order, whatever the deltas look
    like: the thinking line whole as soon as it closes, the answer and the
    handback by sentence, and a tag split across deltas still found."""
    p = voice.AnswerStream()
    reply = (
        "<think>Hmm, so you want to know why attention helps.</think>\n"
        "<answer>It lets every word look at every other word. That is why it handles long "
        "sentences well.</answer>\n<back>Anyway, I was saying the model is simple.</back>"
    )
    got: list[str] = []
    # Deliberately awkward chunks: tags cut in half.
    for i in range(0, len(reply), 7):
        got.extend(p.push(reply[i : i + 7]))
    got.extend(p.finish())
    assert got == [
        "Hmm, so you want to know why attention helps.",
        "It lets every word look at every other word.",
        "That is why it handles long sentences well.",
        "Anyway, I was saying the model is simple.",
    ]
    a = p.answer
    assert a.think.strip() == "Hmm, so you want to know why attention helps."
    assert "every other word" in a.answer
    assert a.back.startswith("Anyway")


def test_an_untagged_reply_is_all_answer() -> None:
    """A model that ignores the format still gets its answer said."""
    p = voice.AnswerStream()
    got = p.push("It is 2.5 times faster. Mostly because ")
    got.extend(p.push("of caching"))
    got.extend(p.finish())
    assert got == ["It is 2.5 times faster.", "Mostly because of caching"]
    assert p.answer.answer == "It is 2.5 times faster. Mostly because of caching"


def test_an_answer_is_voiced_in_whole_sentences() -> None:
    """A comma never splits one, a decimal point is not a sentence end, and
    an unpunctuated tail still gets said."""
    assert voice.speakable_parts("It is 2.5 times faster, roughly. Why? Attention") == [
        "It is 2.5 times faster, roughly.",
        "Why?",
        "Attention",
    ]
    assert voice.speakable_parts("  ") == []


def test_a_finished_sentence_is_taken_and_the_rest_is_kept() -> None:
    """A clause leaves as soon as there is one, or the answer starts late."""
    clause, rest = voice.take_speakable("Letterboxing keeps the frame. It pads the sides.")
    assert clause == "Letterboxing keeps the frame."
    assert rest == " It pads the sides."
    # The tail has no boundary yet: it waits for the next delta rather than
    # going out as a fragment.
    assert voice.take_speakable(rest) == (None, rest)


def test_a_dot_inside_a_token_is_not_a_boundary() -> None:
    """The bug this rule exists for: `gpt-4.1` spoken as two clauses is heard
    as a stutter."""
    buf = "The model is gpt-4.1 and it"
    assert voice.take_speakable(buf) == (None, buf)


def test_a_comma_cuts_only_a_long_enough_clause() -> None:
    # Two rules hold here at once. The comma is in the second word, well under
    # MIN_CLAUSE, so it does not cut; and the closing dot is the last character
    # in the buffer, so it is not a boundary either — mid-stream that dot is
    # indistinguishable from `gpt-4.` still waiting for its `1`. The whole
    # short answer therefore stays put and goes out as the tail, in one piece,
    # which is what it should sound like.
    assert voice.take_speakable("Yes, it does.") == (None, "Yes, it does.")
    clause, _ = voice.take_speakable(
        "Letterboxing preserves the original aspect ratio of the frame, which matters."
    )
    assert clause == "Letterboxing preserves the original aspect ratio of the frame,"


def test_an_unpunctuated_answer_is_left_in_the_buffer() -> None:
    """Nothing is lost when the model stops without punctuation: the caller
    speaks whatever is left, so an un-cut buffer must come back whole."""
    assert voice.take_speakable("about two thirds") == (None, "about two thirds")


def test_keep_going_is_not_a_question() -> None:
    """Only a request to carry on skips the model; a question that happens to
    contain "go" or "continue" does not."""
    for t in [
        "Keep going, please.",
        "keep going",
        "Go on.",
        "Okay, continue.",
        "Please continue",
        "Carry on",
        "Never mind.",
        "No, nothing, sorry.",
        "Go ahead.",
    ]:
        assert voice.is_resume(t), t
    for t in [
        "Why?",
        "So, what's Mochi at all",
        "Keep going with the latency part",
        "How does it continue speaking while I talk?",
        "",
    ]:
        assert not voice.is_resume(t), t


# ── who answers, and what they are told ───────────────────────────────────────


def _line(lid: str, who: str, ordinal: int, said: str) -> Line:
    return Line(lid, who, ordinal, said)


SPEAKERS = [
    Speaker("host", "af_bella", "Bella", "asks the questions"),
    Speaker("expert", "am_adam", "Adam", "explains, with a joke or two"),
]
LINES = [
    _line("l1", "host", 0, "So what is a vector index?"),
    _line("l2", "expert", 1, "Think of it as a very nosy librarian, honestly."),
    _line("l3", "host", 2, "And how fast is it?"),
]


def two_speakers(audio: bool = False) -> Narration:
    """Host then expert on one slide, the expert jokey so the sample is
    recognisable in the prompt."""
    return Narration(list(SPEAKERS), [Part("one", 0, "", [], list(LINES))], audio)


def test_the_speaker_mid_line_answers() -> None:
    assert voice.narrating_speaker(two_speakers(), At(0, "l2", 1500)) == ("am_adam", "Adam")


def test_an_unheard_line_hands_the_floor_back() -> None:
    """Cut in between lines: the next line is queued at zero but nobody has
    heard its speaker yet, so the one who just finished answers."""
    assert voice.narrating_speaker(two_speakers(), At(0, "l3", 0)) == ("am_adam", "Adam")


def test_the_first_line_keeps_its_speaker() -> None:
    """Before anything has played there is nobody to hand back to."""
    assert voice.narrating_speaker(two_speakers(), At(0, "l1", 0))[1] == "Bella"


def test_a_role_word_is_answered_by_the_voices_name() -> None:
    n = Narration([Speaker("host", "bm_george", "Host", "")], [])
    assert voice.narrating_speaker(n, At()) == ("bm_george", "George")
    assert voice.narrating_speaker(Narration([], []), At()) == ("", "Studio")


def test_the_prompt_is_in_the_speakers_persona() -> None:
    """Written as that speaker: their name, their role, the co-host, and
    their own words as the sample of how they talk."""
    p = voice.context_for(two_speakers(), At(0, "l3", 0), "normal", "English")
    assert p.startswith("You are Adam,"), p
    assert "explains, with a joke or two" in p
    assert "Bella (asks the questions)" in p
    assert "- Think of it as a very nosy librarian" in p
    # The host's line is not offered as Adam's voice.
    assert "- So what is a vector index?" not in p
    # l3 was not heard, so the cut mark sits in front of it.
    assert "before this next line" in p
    assert "And how fast is it?" not in p
    assert "plain spoken English" in p
    assert "<answer>ONE or TWO short sentences" in p
    # The three typed parts are asked for.
    assert "<think>" in p and "<back>" in p


def test_the_prompt_of_an_audio_overview_says_listening_and_chapters() -> None:
    """An audio overview's listener is listening, and its parts are
    chapters."""
    p = voice.context_for(two_speakers(audio=True), At(0, "l2", 900), "normal", "English")
    assert "an audio overview the listener is listening to" in p
    assert "THE EPISODE'S CHAPTERS" in p and "CURRENT CHAPTER" in p
    assert "slide" not in p.lower(), p


def test_the_prompt_speaks_to_the_listener_and_knows_keep_going() -> None:
    """The faults of a real turn, each answered in the prompt: talking about
    the listener instead of to them, "keep going" taken as a question, a
    misheard name, and a handback that repeats the line replayed after it."""
    p = voice.context_for(two_speakers(), At(0, "l2", 900), "normal", "English")
    assert 'as "you"' in p
    assert 'never call them "they"' in p
    assert '"keep going"' in p
    assert '"Mochi" for "Moshi"' in p
    assert "do not repeat or paraphrase it" in p
    assert "I was saying that" not in p, "the example the model copied is gone"
    assert "THE SESSION'S SLIDES" in p
    assert "1. one  <- they are here" in p
    assert "[the listener cut in here, 900 ms into this line]" in p


def test_the_prompt_follows_length_and_language() -> None:
    """Answer length and language come from settings and land in the
    prompt."""
    p = voice.context_for(two_speakers(), At(0, "l2", 900), "detailed", "French")
    assert "plain spoken French" in p
    assert "<answer>THREE or FOUR sentences" in p
    p = voice.context_for(two_speakers(), At(0, "l2", 900), "short", "")
    assert "plain spoken English" in p
    assert "<answer>ONE short sentence" in p


# ── the route ─────────────────────────────────────────────────────────────────

ANSWER = (
    "<think>Oh, the speed of it?</think><answer>It answers in milliseconds. "
    "That is the point of the index.</answer><back>Okay, back to the librarian.</back>"
)


class Model:
    """The answer model: streams `reply` in small deltas, after `wait`
    seconds, or answers with an HTTP status."""

    def __init__(self, reply: str | int = ANSWER, wait: float = 0.0) -> None:
        self.reply = reply
        self.wait = wait
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        self.bodies.append(json.loads(request.content))
        await asyncio.sleep(self.wait)
        if isinstance(self.reply, int):
            return httpx2.Response(self.reply, json={"error": {"message": "down"}})
        chunks = [self.reply[i : i + 9] for i in range(0, len(self.reply), 9)]
        events: list[dict[str, Any]] = [{"choices": [{"delta": {"content": c}}]} for c in chunks]
        events.append(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 40, "cost": 0.0006},
            }
        )
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx2.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )


class Voice:
    """The speech server: every line back as 24 kHz mono, 60 ms a word; the
    question heard as `heard`."""

    def __init__(self, heard: str = "How fast is it?", up: bool = True) -> None:
        self.heard = heard
        self.up = up
        self.said: list[tuple[str, str]] = []
        self.transcribed = 0

    async def synthesize(self, line: str, voice_id: str) -> bytes:
        if not self.up:
            raise speech.unreachable("http://tts.test/v1", "refused")
        self.said.append((line, voice_id))
        return ramp_wav(24_000, 1, 24_000 * 60 * len(line.split()) // 1000)

    async def transcribe(self, wav: bytes) -> str:
        self.transcribed += 1
        if not self.up:
            raise speech.unreachable("http://stt.test/v1", "refused")
        return self.heard


@pytest.fixture
def studio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Model, Voice]:
    model, v = Model(), Voice()
    ai = Ai(
        "http://ai.test/v1",
        "k",
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(model)),
        retries=0,
        backoff=0,
    )
    monkeypatch.setattr(ai_client, "ai", lambda: ai)
    monkeypatch.setattr(speech, "speech", lambda: v)
    monkeypatch.setattr(storage, "_root", lambda: tmp_path)
    # The courtesy rotation is per output and in memory; each test starts it
    # fresh.
    monkeypatch.setattr(banter, "_counters", {})
    return model, v


async def _output(client: AsyncClient, audio: bool = False) -> str:
    cid = (await client.post("/api/collections", json={"title": "Vectors"})).json()["id"]
    async with sessionmaker()() as s, s.begin():
        owner = (await s.get_one(Collection, uuid.UUID(cid))).owner_id
        o = Session(
            owner_id=owner,
            collection_id=uuid.UUID(cid),
            kind="audio" if audio else "slides",
            title="Vector indexes",
            state="ready",
            speakers=[sp.as_json() for sp in SPEAKERS],
            slides=[Part("one", 0, "Indexes", [], list(LINES)).as_json()],
            audio={"format": "deep_dive"} if audio else None,
        )
        s.add(o)
        await s.flush()
        return str(o.id)


def _events(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((fields["event"], fields["data"]))
    return out


def _t(data: str) -> str:
    return json.loads(data)["t"]


QUESTION = wav16(np.concatenate([silence(0.3, 48_000), voiced(1.8, 48_000)]), 48_000)
LONG_QUESTION = wav16(np.concatenate([silence(0.3, 48_000), voiced(3.5, 48_000)]), 48_000)


async def _ask(client: AsyncClient, sid: str, wav: bytes, **headers: str) -> list[tuple[str, str]]:
    r = await client.post(
        f"/api/sessions/{sid}/voice",
        params={"slide": 0, "line": "l3", "offset_ms": 1200},
        content=wav,
        headers={"Content-Type": "audio/wav", **headers},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    return _events(r.text)


async def _usage() -> list[dict[str, Any]]:
    async with engine().begin() as c:
        rows = await c.execute(text("SELECT kind, session_id, model FROM usage_events"))
        return [dict(m) for m in rows.mappings()]


async def test_silence_is_gated_before_the_model_and_never_billed(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    model, v = studio
    sid = await _output(client)
    got = await _ask(client, sid, wav16(silence(2, 48_000), 48_000))
    assert [e for e, _ in got] == ["silent"]
    data = json.loads(got[0][1])
    assert data["speech_ms"] == 0 and data["segment_count"] == 0
    assert data["input_ms"] >= 1_900
    assert model.bodies == [], "the model was never asked"
    assert v.transcribed == 0 and v.said == []
    assert await _usage() == [], "nothing was billed"


async def test_a_question_is_answered_in_the_narrators_voice(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    model, v = studio
    sid = await _output(client)
    got = await _ask(client, sid, QUESTION)
    names = [e for e, _ in got]
    # Cut in 1200 ms into l3: Bella was talking, so Bella answers.
    assert names[0] == "speaker" and _t(got[0][1]) == "Bella"
    assert names[-1] == "done"
    said = [_t(d) for e, d in got if e == "said"]
    assert said == [
        "Oh, the speed of it? ",
        "It answers in milliseconds. ",
        "That is the point of the index. ",
        "Okay, back to the librarian. ",
    ]
    # Each sentence's sound follows its words, in the voice that was talking.
    first = names.index("said")
    assert names[first + 1] == "audio"
    pcm = base64.b64decode(next(d for e, d in got if e == "audio"))
    assert 0 < len(pcm) <= voice.CHUNK_SAMPLES * 2 and len(pcm) % 2 == 0
    assert {who for _, who in v.said} == {"af_bella"}
    assert ("heard", json.dumps({"t": "How fast is it?"})) in got
    done = json.loads(got[-1][1])
    assert done == {
        "t": "Oh, the speed of it? It answers in milliseconds. That is the point of the "
        "index. Okay, back to the librarian.",
        "think": "Oh, the speed of it?",
        "answer": "It answers in milliseconds. That is the point of the index.",
        "back": "Okay, back to the librarian.",
    }

    # The model heard the question itself, as audio, and was asked for text.
    body = model.bodies[0]
    assert body["model"] == st.ANSWER_MODEL_DEFAULT
    assert "modalities" not in body and "audio" not in body
    system, question = body["messages"]
    assert system["content"].startswith("You are Bella,")
    part = question["content"][0]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    assert base64.b64decode(part["input_audio"]["data"]) == QUESTION
    assert await _usage() == [
        {"kind": "voice", "session_id": uuid.UUID(sid), "model": st.ANSWER_MODEL_DEFAULT}
    ]


async def test_keep_going_resumes_without_an_answer(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    """A short clip that only says carry on: the narrator says so, and the
    model's reply is dropped unread."""
    _, v = studio
    v.heard = "Keep going, please."
    sid = await _output(client)
    got = await _ask(client, sid, QUESTION)
    said = [_t(d) for e, d in got if e == "said"]
    assert len(said) == 1 and said[0].strip() in banter.RESUME
    assert "audio" in [e for e, _ in got]
    done = json.loads(next(d for e, d in got if e == "done"))
    assert done["answer"] == "" and done["back"] == "" and done["think"] in banter.RESUME


async def test_a_long_question_is_not_held_for_its_transcript(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    """Over 2.5 s of speech it is a question, whatever the transcript says."""
    _, v = studio
    v.heard = "Keep going"
    sid = await _output(client)
    got = await _ask(client, sid, LONG_QUESTION)
    assert json.loads(next(d for e, d in got if e == "done"))["back"] == (
        "Okay, back to the librarian."
    )


async def test_a_slow_answer_is_held_with_a_line_in_the_same_voice(
    client: AsyncClient, studio: tuple[Model, Voice], monkeypatch: pytest.MonkeyPatch
) -> None:
    model, v = studio
    model.wait = 0.4
    monkeypatch.setattr(voice, "HOLD_FIRST_MS", 50)
    sid = await _output(client)
    names = [e for e, _ in await _ask(client, sid, LONG_QUESTION)]
    assert names.index("hold") < names.index("hold_audio") < names.index("said")
    assert {who for _, who in v.said} == {"af_bella"}


async def test_with_courtesy_off_the_wait_is_silent(
    client: AsyncClient, studio: tuple[Model, Voice], monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _ = studio
    model.wait = 0.4
    monkeypatch.setattr(voice, "HOLD_FIRST_MS", 50)
    r = await client.patch(f"/api/settings/{st.COURTESY_KEY}", json={"value": "off"})
    assert r.status_code == 200, r.text
    sid = await _output(client)
    names = [e for e, _ in await _ask(client, sid, LONG_QUESTION)]
    assert "hold" not in names and "said" in names


async def test_with_no_voice_server_the_answer_is_still_said(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    """A synthesis that fails costs the sound and not the turn; a transcript
    that fails says why beside the question."""
    _, v = studio
    v.up = False
    sid = await _output(client)
    got = await _ask(client, sid, LONG_QUESTION)
    names = [e for e, _ in got]
    assert "audio" not in names and names.count("said") == 4 and names[-1] == "done"
    failed = next(d for e, d in got if e == "heard_failed")
    assert _t(failed).startswith("The voice server at http://stt.test/v1 is not answering.")


async def test_a_model_that_fails_says_why(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    model, _ = studio
    model.reply = 503
    sid = await _output(client)
    got = await _ask(client, sid, LONG_QUESTION)
    failed = [_t(d) for e, d in got if e == "failed"]
    assert failed == ["The AI provider is not answering right now. Try again in a minute."]
    assert "done" not in [e for e, _ in got]


async def test_unreadable_or_empty_audio_says_what_to_do(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    model, _ = studio
    sid = await _output(client)
    got = await _ask(client, sid, b"not a wav at all")
    assert got == [
        (
            "failed",
            json.dumps(
                {
                    "t": "The recording could not be read as audio. Check the microphone, "
                    "then ask again."
                }
            ),
        )
    ]
    got = await _ask(client, sid, b"")
    assert [_t(d) for _, d in got] == [voice.NO_AUDIO]
    assert model.bodies == []


async def test_with_no_ai_key_a_question_is_refused_but_silence_is_not(
    client: AsyncClient, studio: tuple[Model, Voice], monkeypatch: pytest.MonkeyPatch
) -> None:
    keyless = Ai("http://ai.test/v1", "")
    monkeypatch.setattr(ai_client, "ai", lambda: keyless)
    sid = await _output(client)
    assert [e for e, _ in await _ask(client, sid, wav16(silence(1, 16_000), 16_000))] == ["silent"]
    got = await _ask(client, sid, QUESTION)
    assert [_t(d) for _, d in got] == [voice.NO_KEY]


async def test_only_the_owner_can_ask(client: AsyncClient, studio: tuple[Model, Voice]) -> None:
    model, _ = studio
    sid = await _output(client)
    them = await other_person(client, "them@example.com")
    r = await client.post(
        f"/api/sessions/{sid}/voice",
        content=QUESTION,
        headers={"Content-Type": "audio/wav", **them},
    )
    assert r.status_code == 404
    assert r.json()["detail"].endswith("Reload the page to see what is.")
    assert model.bodies == []


async def test_an_audio_overview_is_answered_as_chapters(
    client: AsyncClient, studio: tuple[Model, Voice]
) -> None:
    model, _ = studio
    sid = await _output(client, audio=True)
    await _ask(client, sid, LONG_QUESTION)
    system = model.bodies[0]["messages"][0]["content"]
    assert "THE EPISODE'S CHAPTERS" in system and "1. Indexes  <- they are here" in system
