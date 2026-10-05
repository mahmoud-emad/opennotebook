"""Study notes: ported from opennotebook_script/src/notes.rs and the Rust
server's notes_impl.rs, and the routes against a mocked model."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from opennotebook.api.notes import OUTPUT_TOKENS, NotesSummary, tokens_in
from opennotebook.db.models import StudyNotes as Row
from opennotebook.script.mindmap import NamedDoc
from opennotebook.script.notes import (
    WHOLE_TEXT_CHARS,
    check_citations,
    numbered,
    parse_sections,
    read,
    user_prompt,
)
from tests.conftest import other_person
from tests.model import PRICE_IN, PRICE_OUT, add_note, fails, install, says, spent


def p(texts: list[str]) -> list[tuple[int, str]]:
    return [(0, t) for t in texts]


REPLY = """\
# Moshi: Real-Time Spoken Dialogue

## Overview
Moshi is a speech-text foundation model for full-duplex dialogue [1].

## Key Ideas
### Speech in, speech out
Moshi skips the text pipeline [1]. Its latency is 200 ms in practice [2].

### Inner Monologue
- Text tokens are predicted before audio tokens [3].
- This improves linguistic quality [3].

## Glossary of Key Terms
- **Full-duplex**: listening and speaking at once [1].
- Mimi: a neural audio codec [2]

## Quiz
1. Why does Moshi skip text?
2. What is the Inner Monologue?
   Answer: Predicting text tokens before audio tokens [3].

## Quiz Answer Key
1. Because a pipeline adds latency and loses non-linguistic information [1].
2. ignored, the inline answer wins

## Essay Questions
1. How do latency and linguistic quality trade off in Moshi?
"""

PASSAGES = [
    "Moshi is a speech-text foundation model and full-duplex spoken dialogue framework. "
    "It skips the text pipeline, whose latency and loss of non-linguistic information it avoids.",
    "Mimi is a neural audio codec. Moshi has a practical latency of 200 ms.",
    "Inner Monologue predicts time-aligned text tokens before audio tokens, improving the "
    "linguistic quality of generated speech.",
]


def test_a_reply_reads_into_its_sections() -> None:
    n = read(REPLY, p(PASSAGES), "hint", True)
    assert n.title == "Moshi: Real-Time Spoken Dialogue"
    assert n.overview.startswith("Moshi is a speech-text")
    assert len(n.ideas) == 2
    assert n.ideas[1].heading == "Inner Monologue"
    assert "- Text tokens" in n.ideas[1].body
    assert [t.term for t in n.glossary] == ["Full-duplex", "Mimi"]
    assert len(n.quiz) == 2
    assert n.quiz[0].answer.startswith("Because a pipeline")
    assert n.quiz[1].answer.startswith("Predicting text tokens")
    assert len(n.essays) == 1
    assert n.dropped == 0
    # Three passages cited, numbered in reading order.
    assert len(n.cited) == 3
    assert n.cited[0].n == 1


def test_citations_are_renumbered_in_reading_order() -> None:
    n = read("## Overview\nLatency is 200 ms [2]. It skips text [1].\n", p(PASSAGES), "hint", True)
    assert n.overview == "Latency is 200 ms [1]. It skips text [2]."
    assert "200 ms" in n.cited[0].excerpt


def test_a_citation_to_a_passage_that_never_says_it_is_dropped_and_counted() -> None:
    t, dropped = check_citations(
        "Mimi is a neural audio codec [3]. Latency is 200 ms [2].", p(PASSAGES)
    )
    assert t == "Mimi is a neural audio codec. Latency is 200 ms [2]."
    assert dropped == 1
    # One of two numbers wrong: only that one goes.
    t, dropped = check_citations("Mimi is a neural audio codec [3, 2].", p(PASSAGES))
    assert t == "Mimi is a neural audio codec [2]."
    assert dropped == 1


def test_a_number_past_the_passages_is_removed_and_counted() -> None:
    n = read("## Overview\nIt skips text [9]. And [1].\n", p(PASSAGES), "h", True)
    assert n.overview == "It skips text. And [1]."
    assert n.dropped == 1


def test_a_run_of_markers_shares_one_claim() -> None:
    t, dropped = check_citations("Moshi has a practical latency of 200 ms [2][1].", p(PASSAGES))
    assert t == "Moshi has a practical latency of 200 ms [2][1]."
    assert dropped == 0


def test_plurals_match_their_passage() -> None:
    # "codecs" against a passage that says "codec".
    _, dropped = check_citations("Mimi codecs compress audio [2].", p(PASSAGES))
    assert dropped == 0


def test_headings_without_hashes_and_renamed_sections_are_still_found() -> None:
    n = parse_sections(
        "**Summary**\nA short summary.\n**Key Concepts**\n### One\nBody one.\n### Two\nBody two.\n"
        "**Short-Answer Questions**\n1) First?\n2) Second?\n**Answers**\n1) A1.\n2) A2.\n",
        "Hint",
    )
    assert n.title == "Hint"
    assert n.overview == "A short summary."
    assert len(n.ideas) == 2
    assert len(n.quiz) == 2
    assert n.quiz[1].answer == "A2."


def test_an_idea_written_one_level_too_high_is_still_an_idea() -> None:
    n = parse_sections(
        "## Key ideas\n## Latency\nIt is low.\n### Codec\nMimi.\n## Glossary\n- **X**: y\n", "h"
    )
    assert [i.heading for i in n.ideas] == ["Latency", "Codec"]
    assert len(n.glossary) == 1


def test_the_markdown_export_carries_every_section_and_its_sources() -> None:
    n = read(REPLY, p(PASSAGES), "hint", True)
    md = n.to_markdown(lambda _: "Moshi paper")
    for h in (
        "# Moshi: Real-Time Spoken Dialogue",
        "## Overview",
        "### Inner Monologue",
        "- **Full-duplex**:",
        "## Quiz",
        "## Answer key",
        "## Essay questions",
        "## Sources",
        '1. Moshi paper: "Moshi is a speech-text',
    ):
        assert h in md, f"{h} missing from\n{md}"


def test_every_passage_of_a_short_source_is_numbered() -> None:
    docs = [
        NamedDoc(
            "a.md",
            "A",
            "First paragraph of real prose, long enough to be kept as a passage here.\n\n"
            "Second paragraph of real prose, also long enough to count as a passage.",
        )
    ]
    ps, cut = numbered(docs, "A")
    assert not cut
    assert ps
    prompt = user_prompt(docs, ps, " latency ")
    assert "[1] (A) First paragraph" in prompt
    assert prompt.endswith("Focus: latency\n")


# ── what is kept (notes_impl.rs) ──────────────────────────────────────────────


def test_a_summary_counts_the_sections() -> None:
    row = Row(
        id=uuid.uuid4(),
        collection_id=uuid.uuid4(),
        title="Moshi",
        focus="",
        sources=["a.md"],
        created_at=datetime.now(UTC),
        body={
            "ideas": [{"heading": "Latency", "body": "200 ms [1]."}],
            "quiz": [{"question": "Why?", "answer": "Because [1]."}],
            "glossary": [{"term": "Mimi", "definition": "A codec [1]."}],
        },
    )
    s = NotesSummary.of(row)
    assert (s.ideas, s.questions, s.terms) == (1, 1, 1)
    assert s.headings == ["Latency"]


def test_the_estimate_counts_the_text_sent_not_the_text_staged() -> None:
    # The 189,870 byte collection: about 47.5k tokens of text, the prompt, and
    # a label for each of about 211 passages.
    assert tokens_in(189_870) == 47_468 + 1_200 + 211 * 6
    assert tokens_in(5_000_000) == tokens_in(WHOLE_TEXT_CHARS)


# ── the routes ────────────────────────────────────────────────────────────────

MOSHI = (
    "Moshi is a speech-text foundation model and full-duplex spoken dialogue framework. "
    "It skips the text pipeline, whose latency and loss of non-linguistic information it avoids."
)
MIMI = "Mimi is a neural audio codec. Moshi has a practical latency of 200 ms in conversation."

# [1] is the Moshi passage, [2] the Mimi one. The overview's second claim
# cites a passage that never says it, and [7] names no passage.
NOTES = """\
# Moshi notes

## Overview
Moshi is a full-duplex spoken dialogue model [1]. Mimi is a neural audio codec [1].

## Key ideas
### Latency
Moshi has a practical latency of 200 ms [2].

### Codec
Mimi is a neural audio codec [2][7].

## Quiz
1. What is Mimi?

## Answer key
1. A neural audio codec [2].

## Essay questions
1. Why does latency matter in dialogue?

## Glossary
- **Full-duplex**: listening and speaking at once, as Moshi's dialogue framework does [1].
"""


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def test_notes_are_written_checked_cited_and_kept(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    moshi = await add_note(client, cid, MOSHI, title="Moshi paper")
    mimi = await add_note(client, cid, MIMI, title="Mimi codec")
    model.answers(says(NOTES))
    r = await client.post(f"/api/collections/{cid}/notes", json={})
    assert r.status_code == 201, r.text
    n = r.json()
    assert n["title"] == "Moshi notes"
    assert n["overview"] == (
        "Moshi is a full-duplex spoken dialogue model [1]. Mimi is a neural audio codec."
    )
    assert [i["heading"] for i in n["idea_list"]] == ["Latency", "Codec"]
    assert n["idea_list"][1]["body"] == "Mimi is a neural audio codec [2]."
    # One citation that never says its claim, one number naming no passage.
    assert n["dropped"] == 2
    assert [(c["n"], c["name"], c["title"]) for c in n["citations"]] == [
        (1, moshi, "Moshi paper"),
        (2, mimi, "Mimi codec"),
    ]
    assert n["quiz"] == [{"question": "What is Mimi?", "answer": "A neural audio codec [2]."}]
    assert n["essays"] == ["Why does latency matter in dialogue?"]
    assert n["glossary"][0]["term"] == "Full-duplex"
    assert (n["ideas"], n["questions"], n["terms"]) == (2, 1, 1)
    assert n["sources"] == [moshi, mimi] and n["model"] == "google/gemini-2.5-flash-lite"
    # A titled note is stored with its title as a heading, as the Rust studio
    # stored it, so the passage quoted starts with that heading.
    assert '2. Mimi codec: "# Mimi codec Mimi is a neural audio codec.' in n["markdown"]
    assert "[1] (Moshi paper) # Moshi paper" in model.said_to(0, "user")

    got = (await client.get(f"/api/collections/{cid}/notes/{n['id']}")).json()
    assert got == n
    listed = (await client.get(f"/api/collections/{cid}/notes")).json()
    assert [x["id"] for x in listed] == [n["id"]]
    (row,) = await spent()
    assert row["kind"] == "notes" and str(row["collection_id"]) == cid


async def test_thin_notes_are_asked_for_once_more_and_the_fuller_kept(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    model.answers(says("## Overview\nMoshi is a dialogue model [1].\n"), says(NOTES))
    r = await client.post(f"/api/collections/{cid}/notes", json={"focus": "latency"})
    assert r.status_code == 201, r.text
    assert "missing their sections" in model.said_to(1, "user")
    assert "Focus: latency" in model.said_to(0, "user")
    assert len(r.json()["idea_list"]) == 2
    assert r.json()["focus"] == "latency"


async def test_notes_cut_off_at_the_token_ceiling_keep_what_came(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    model.answers(says(NOTES.split("## Quiz")[0], finish="length"))
    r = await client.post(f"/api/collections/{cid}/notes", json={})
    assert r.status_code == 201, r.text
    assert r.json()["quiz"] == []


async def test_an_account_out_of_credit_writes_no_notes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    model.answers(fails(402, "Insufficient credits"))
    r = await client.post(f"/api/collections/{cid}/notes", json={})
    assert r.status_code == 402
    assert r.json()["detail"].startswith("The AI account is out of credit")
    assert (await client.get(f"/api/collections/{cid}/notes")).json() == []


async def test_a_model_that_writes_nothing_is_a_sentence(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    model.answers(says("   "))
    r = await client.post(f"/api/collections/{cid}/notes", json={})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("The AI model gave back no usable study notes.")


async def test_the_notes_estimate_prices_the_text_and_the_writing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    await add_note(client, cid, MIMI)
    e = (await client.get(f"/api/collections/{cid}/notes/estimate")).json()
    chars = len(MOSHI) + len(MIMI)
    # Itemised the way a build's estimate is: one step, one or two calls.
    assert e["sources"] == 2 and e["source_chars"] == chars
    [line] = e["lines"]
    assert (line["group"], line["step"], line["unpriced"]) == (
        "Study notes",
        "Write the study notes",
        False,
    )
    assert (line["input_tokens"], line["output_tokens_typical"]) == (
        tokens_in(chars),
        OUTPUT_TOKENS,
    )
    assert (line["calls_typical"], line["calls_high"]) == (1, 2)
    one = tokens_in(chars) * PRICE_IN + OUTPUT_TOKENS * PRICE_OUT
    assert e["total_typical_usd"] == pytest.approx(one) == e["total_low_usd"]
    assert e["total_high_usd"] == pytest.approx(2 * one)
    assert line["price_in_per_million"] == pytest.approx(PRICE_IN * 1_000_000)
    assert e["model"] == "google/gemini-2.5-flash-lite"
    assert e["facts"] == [f"2 sources · {chars:,} characters", "by Gemini 2.5 Flash Lite"]


async def test_a_source_that_is_not_there_writes_no_notes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    r = await client.post(f"/api/collections/{cid}/notes", json={"sources": ["nope.md"]})
    assert r.status_code == 422
    assert "“nope.md” is not a source" in r.json()["detail"]


async def test_nobody_else_can_write_notes_of_a_collection(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    them = await other_person(client, "them@test")
    r = await client.post(f"/api/collections/{cid}/notes", json={}, headers=them)
    assert r.status_code == 404
    r = await client.get(f"/api/collections/{cid}/notes/estimate", headers=them)
    assert r.status_code == 404


async def test_the_spending_limit_is_checked_before_any_model_call(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, MOSHI)
    # Every tool is checked against the limit, not only builds.
    monkeypatch.setenv("OPENNOTEBOOK_MAX_BUILD_USD", "0.0000001")
    e = (await client.get(f"/api/collections/{cid}/notes/estimate")).json()
    assert e["limit_usd"] == pytest.approx(0.0000001) and e["over_limit"]
    r = await client.post(f"/api/collections/{cid}/notes", json={})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Study notes of these sources could cost up to $")
    assert "Settings › Costs & limits" in r.json()["detail"]
    monkeypatch.setenv("OPENNOTEBOOK_MAX_BUILD_USD", "5")
    e = (await client.get(f"/api/collections/{cid}/notes/estimate")).json()
    assert e["limit_usd"] == 5 and not e["over_limit"]
