"""Mind maps: ported from opennotebook_script/src/mindmap.rs and the
Rust server's mindmap_impl.rs, and the routes against a mocked model."""

import uuid

import pytest
from httpx import AsyncClient

from opennotebook.api.mindmaps import map_title, shape, tokens_for
from opennotebook.script.mindmap import (
    LABEL_CHARS,
    MAX_CHILDREN,
    MAX_DEPTH,
    WHOLE_TEXT_CHARS,
    MindNode,
    NamedDoc,
    clean,
    material,
    mentioned,
    parse_outline,
    system_prompt,
    thin,
    user_prompt,
)
from tests.conftest import other_person
from tests.model import PRICE_IN, PRICE_OUT, add_note, fails, install, made, says, spent


def names(n: MindNode) -> list[str]:
    return [c.name for c in n.children]


def parsed(raw: str, hint: str = "x") -> MindNode:
    t = parse_outline(raw, hint)
    assert t is not None, raw
    return t


TWO = "- Transformers\n  - Attention\n    - Queries and keys\n  - Training\n    - Data\n"

# ── parsing ───────────────────────────────────────────────────────────────────


def test_indentation_by_two_four_tabs_or_a_mix_gives_one_tree() -> None:
    two = parsed(TWO)
    four = (
        "- Transformers\n    - Attention\n        - Queries and keys\n    - Training\n"
        "        - Data\n"
    )
    tabs = "- Transformers\n\t- Attention\n\t\t- Queries and keys\n\t- Training\n\t\t- Data\n"
    mixed = "- Transformers\n  - Attention\n      - Queries and keys\n  - Training\n    - Data\n"
    for other in (four, tabs, mixed):
        assert parsed(other) == two, other
    assert two.name == "Transformers"
    assert names(two) == ["Attention", "Training"]
    assert two.depth() == 2


@pytest.mark.parametrize(
    "raw",
    [
        "- **Bold label**:",
        "1. Bold label",
        "* Bold label",
        "- Bold label.",
        "2) `Bold label`",
        "- ## Bold label",
    ],
)
def test_decoration_is_stripped_from_labels(raw: str) -> None:
    assert names(parsed(f"- Root\n  {raw}\n")) == ["Bold label"]


def test_an_explanation_after_a_colon_is_dropped() -> None:
    assert names(parsed("- Root\n  - Cost: what the user pays each month\n")) == ["Cost"]


def test_a_long_label_is_cut_at_a_word() -> None:
    t = parsed(f"- Root\n  - {'word ' * 30}\n")
    assert len(t.children[0].name) <= LABEL_CHARS
    assert not t.children[0].name.endswith(" ")


def test_a_missing_root_is_taken_from_the_title_line_or_the_hint() -> None:
    t = parsed("# Rust ownership\n- Borrowing\n- Lifetimes\n", "hint")
    assert t.name == "Rust ownership"
    assert names(t) == ["Borrowing", "Lifetimes"]

    preamble = "Here is a mind map of the material:\n- Borrowing\n- Lifetimes\n"
    assert parsed(preamble, "My draft").name == "My draft"


def test_commentary_after_the_outline_is_not_a_node() -> None:
    assert names(parsed("- Root\n  - A\n  - B\nI hope this helps.\n")) == ["A", "B"]


def test_a_fenced_reply_parses_like_a_bare_one() -> None:
    assert parse_outline(f"```markdown\n{TWO}```\n", "x") == parse_outline(TWO, "x")


def test_no_bullet_is_no_map() -> None:
    assert parse_outline("I cannot make a map of this.", "x") is None


# ── cleaning ──────────────────────────────────────────────────────────────────


def test_siblings_saying_the_same_thing_merge_and_keep_both_childrens() -> None:
    t, _ = clean(parsed("- Root\n  - Cost\n    - Hosting\n  - cost.\n    - Licences\n"), None)
    assert names(t) == ["Cost"]
    assert names(t.children[0]) == ["Hosting", "Licences"]


def test_nothing_is_kept_below_the_depth_limit() -> None:
    deep = "- R\n  - 1\n    - 2\n      - 3\n        - 4\n          - 5\n"
    t, _ = clean(parsed(deep), None)
    assert t.depth() == MAX_DEPTH
    assert t.count() == MAX_DEPTH + 1


def test_a_node_the_sources_never_mention_is_dropped_with_its_children() -> None:
    raw = (
        "- Rust\n  - Ownership\n    - Borrow checker\n  - Quantum Entanglement\n"
        "    - Spooky action\n"
    )
    sources = "rust ownership is enforced by the borrow checker at compile time."
    t, dropped = clean(parsed(raw), sources)
    assert names(t) == ["Ownership"]
    assert dropped == 2


def test_an_organising_label_over_mentioned_ones_is_kept() -> None:
    raw = "- Rust\n  - Key ideas\n    - Ownership\n    - Unicorns\n"
    t, dropped = clean(parsed(raw), "ownership rules")
    assert names(t) == ["Key ideas"]
    assert names(t.children[0]) == ["Ownership"]
    assert dropped == 1


def test_a_plural_label_is_found_by_its_stem() -> None:
    assert mentioned("Costs", "the cost of hosting")
    assert mentioned("Training runs", "we train for a week")
    assert not mentioned("Unicorns", "the cost of hosting")
    # Nothing worth matching: nothing to check it by.
    assert mentioned("Why", "anything")


def test_the_root_survives_even_when_unmentioned() -> None:
    t, _ = clean(parsed("- Zebra\n  - Ownership\n"), "ownership")
    assert t.name == "Zebra"


def test_a_node_keeps_at_most_the_child_cap() -> None:
    raw = "- Root\n" + "".join(f"  - Topic {i}\n" for i in range(20))
    t, _ = clean(parsed(raw), None)
    assert len(t.children) == MAX_CHILDREN


def test_a_map_of_a_handful_of_nodes_is_thin() -> None:
    assert thin(parsed("- R\n  - A\n    - a1\n  - B\n    - b1\n  - C\n"))
    raw = "- R\n" + "".join(f"  - {t}\n    - {t}1\n    - {t}2\n" for t in "ABCD")
    assert not thin(parsed(raw))
    assert thin(parsed("- R\n  - Only\n"))


def test_the_outline_export_reads_back_as_the_same_tree() -> None:
    t = parsed(TWO)
    assert parsed(t.to_outline()) == t


# ── the prompt ────────────────────────────────────────────────────────────────


def test_sources_under_the_limit_go_whole_and_over_it_go_as_excerpts() -> None:
    m, cut = material([NamedDoc("a.md", "A", "Short text.")], "q")
    assert not cut
    assert "SOURCE 1: A" in m and "Short text." in m

    para = "Ownership moves values between bindings in a program.\n\n"
    big = [NamedDoc("b.md", "B", para * (WHOLE_TEXT_CHARS // len(para) + 100))]
    m, cut = material(big, "ownership")
    assert cut
    assert len(m) <= WHOLE_TEXT_CHARS + 1_000


def test_the_prompts_example_is_a_nested_outline() -> None:
    """The example in the prompt must itself parse to a nested tree: a model
    copies its shape, so a broken example teaches the flat reply."""
    p = system_prompt(None, "")
    example = "\n".join(p[p.index("- Subject") :].split("\n")[:3])
    assert parsed(example).depth() == 2, example


def test_the_focus_reaches_both_prompts_only_when_given() -> None:
    assert "A focus is given" in system_prompt("security", "")
    assert "A focus is given" not in system_prompt(None, "")
    assert "A focus is given" not in system_prompt("  ", "")
    assert user_prompt("M\n\n", "security").endswith("Focus: security")
    assert "Focus" not in user_prompt("M\n\n", None)


# ── what is kept (mindmap_impl.rs) ────────────────────────────────────────────


def test_a_generic_root_does_not_title_the_map() -> None:
    assert map_title("Photosynthesis", "Leaves") == "Photosynthesis"
    assert map_title("Your sources", "Leaves") == "Leaves"
    assert map_title("your sources ", None) == "Your sources"
    assert map_title("", "Leaves") == "Leaves"


def test_a_summary_carries_the_outline_of_its_tree() -> None:
    t = parsed("- Rust\n  - Ownership\n    - Borrowing\n  - Traits\n")
    assert shape(t.to_json()) == [1, 0]
    assert t.to_json()["children"][0]["children"][0] == {"name": "Borrowing", "children": []}


def test_the_estimate_counts_the_text_sent_not_the_text_staged() -> None:
    # The 189,870 byte collection: about 47.5k tokens of text and the prompt.
    assert tokens_for(189_870) == (47_468 + 500, 1_000)
    # Past the cap the generator sends excerpts, so the estimate stops growing.
    assert tokens_for(5_000_000) == tokens_for(WHOLE_TEXT_CHARS)
    assert tokens_for(0) == (500, 1_000)


# ── the routes ────────────────────────────────────────────────────────────────

REEFS = (
    "Coral reefs cover less than one percent of the ocean floor but shelter a quarter of "
    "marine species. Reef fish, corals, sponges and algae live together. Warming water "
    "causes bleaching, and tourism and fishing depend on healthy reefs."
)

OUTLINE = """Here is the map:
- Coral reefs
  - Ocean floor
    - One percent
  - Marine species
    - Reef fish
    - Sponges
    - Algae
  - Threats
    - Bleaching
    - Warming water
  - Economy
    - Tourism
    - Fishing
  - Dragons
    - Fire breathing
"""


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def test_a_map_is_made_checked_against_the_sources_and_kept(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    name = await add_note(client, cid, REEFS, title="Reefs")
    model.answers(says(OUTLINE))
    job, m = await made(client, f"/api/collections/{cid}/mindmaps", {"focus": " threats "})
    assert job["status"] == "done" and job["kind"] == "mindmap", job
    assert m is not None and m["state"] == "ready"
    assert m["title"] == "Coral reefs" and m["root"]["name"] == "Coral reefs"
    # The dragons are nowhere in the sources: gone, with their child.
    assert [c["name"] for c in m["root"]["children"]] == [
        "Ocean floor",
        "Marine species",
        "Threats",
        "Economy",
    ]
    assert m["dropped"] == 2 and m["node_count"] == 13 and m["shape"] == [1, 3, 2, 2]
    assert m["focus"] == "threats" and m["sources"] == [name]
    assert m["model"] == "google/gemini-2.5-flash-lite"
    assert not m["excerpted"] and not m["unchecked"]
    assert "Focus: threats" in model.said_to(0, "user")
    assert "SOURCE 1: Reefs" in model.said_to(0, "user")

    listed = (await client.get(f"/api/collections/{cid}/mindmaps")).json()
    assert [x["id"] for x in listed] == [m["id"]]
    got = (await client.get(f"/api/collections/{cid}/mindmaps/{m['id']}")).json()
    assert got == m
    (row,) = await spent()
    assert row["kind"] == "mindmap" and str(row["collection_id"]) == cid
    assert str(row["job_id"]) == job["id"]


async def test_a_reply_with_no_outline_is_asked_for_once_more(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    model.answers(says("I cannot make a map of this."), says(OUTLINE))
    job, m = await made(client, f"/api/collections/{cid}/mindmaps")
    assert job["status"] == "done" and m is not None
    assert "too thin" in model.said_to(1, "user")
    assert len(await spent()) == 2


async def test_a_map_cut_off_at_the_token_ceiling_keeps_what_came(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    model.answers(says(OUTLINE, finish="length"))
    _, m = await made(client, f"/api/collections/{cid}/mindmaps")
    assert m is not None and m["node_count"] == 13


async def test_a_map_in_another_language_is_not_checked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    await client.patch("/api/settings/OPENNOTEBOOK_LANGUAGE", json={"value": "French"})
    model.answers(says(OUTLINE))
    _, m = await made(client, f"/api/collections/{cid}/mindmaps")
    assert m is not None and m["unchecked"] and m["dropped"] == 0 and m["node_count"] == 15


async def test_a_model_that_maps_nothing_is_a_sentence(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    model.answers(says("No."), says("Still no."))
    job, m = await made(client, f"/api/collections/{cid}/mindmaps")
    # The map is removed, and its job says why.
    assert job["status"] == "failed" and m is None
    assert job["error"].startswith("The AI model gave back no usable mind map.")
    assert (await client.get(f"/api/collections/{cid}/mindmaps")).json() == []


async def test_a_busy_provider_is_a_sentence(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    # Asked three times: the first call and its two retries.
    model.answers(*[fails(503, "Service Unavailable")] * 3)
    job, _ = await made(client, f"/api/collections/{cid}/mindmaps")
    assert job["status"] == "failed"
    assert job["error"].startswith("The AI provider is not answering right now.")


async def test_the_estimate_prices_one_call_and_a_second_at_most(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    r = await client.get(f"/api/collections/{cid}/mindmaps/estimate")
    assert r.status_code == 200, r.text
    e = r.json()
    tokens_in, tokens_out = tokens_for(len(REEFS))
    # Itemised the way a build's estimate is: one step, one or two calls.
    assert e["sources"] == 1 and e["source_chars"] == len(REEFS)
    [line] = e["lines"]
    assert (line["group"], line["step"], line["unpriced"]) == (
        "Mind map",
        "Draw the mind map",
        False,
    )
    assert (line["input_tokens"], line["output_tokens_typical"]) == (tokens_in, tokens_out)
    one = tokens_in * PRICE_IN + tokens_out * PRICE_OUT
    assert e["total_typical_usd"] == pytest.approx(one)
    assert e["total_high_usd"] == pytest.approx(2 * one)
    assert e["facts"] == [f"1 source · {len(REEFS):,} characters", "by Gemini 2.5 Flash Lite"]
    # Estimating costs nothing.
    assert await spent() == []


async def test_an_empty_collection_has_nothing_to_map(client: AsyncClient) -> None:
    cid = await _collection(client)
    for r in (
        await client.post(f"/api/collections/{cid}/mindmaps", json={}),
        await client.get(f"/api/collections/{cid}/mindmaps/estimate"),
    ):
        assert r.status_code == 409
        assert r.json()["detail"].startswith("Add a source first")


async def test_nobody_else_can_map_a_collection(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    them = await other_person(client, "them@test")
    r = await client.post(f"/api/collections/{cid}/mindmaps", json={}, headers=them)
    assert r.status_code == 404
    r = await client.get(f"/api/collections/{cid}/mindmaps/estimate", headers=them)
    assert r.status_code == 404
    r = await client.post(f"/api/collections/{uuid.uuid4()}/mindmaps", json={})
    assert r.status_code == 404


async def test_the_spending_limit_is_checked_before_any_model_call(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    # Every tool is checked against the limit, not only builds.
    monkeypatch.setenv("OPENNOTEBOOK_MAX_BUILD_USD", "0.0000001")
    e = (await client.get(f"/api/collections/{cid}/mindmaps/estimate")).json()
    assert e["limit_usd"] == pytest.approx(0.0000001) and e["over_limit"]
    r = await client.post(f"/api/collections/{cid}/mindmaps", json={})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("A mind map of these sources could cost up to $")
    assert "Settings › Costs & limits" in r.json()["detail"]
    monkeypatch.setenv("OPENNOTEBOOK_MAX_BUILD_USD", "5")
    e = (await client.get(f"/api/collections/{cid}/mindmaps/estimate")).json()
    assert e["limit_usd"] == 5 and not e["over_limit"]


async def test_a_model_with_no_price_is_estimated_and_marked_unpriced(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    cid = await _collection(client)
    await add_note(client, cid, REEFS)
    monkeypatch.setenv("OPENNOTEBOOK_MINDMAP_MODEL", "acme/unlisted-1")
    e = (await client.get(f"/api/collections/{cid}/mindmaps/estimate")).json()
    [line] = e["lines"]
    assert line["unpriced"] and line["price_in_per_million"] is None
    assert e["total_high_usd"] == 0 and not e["over_limit"]
    assert e["facts"][1] == "by Unlisted 1"
    assert any("no price" in a for a in e["assumptions"])
