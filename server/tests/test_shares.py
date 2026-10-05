"""Sharing, Discover and reuse. Ported from the tests of the Rust server's
`share.rs` and `collection.rs`, plus the routes: who may read and change a
share, and that a reuse is a copy of its own."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import storage
from opennotebook.config import settings
from opennotebook.db.models import Collection, Share
from opennotebook.db.session import engine, sessionmaker
from opennotebook.domain import collections, shares
from opennotebook.domain.shares import NOTE_MAX, Card, Holdings, Key, Live, Out, Plan
from opennotebook.errors import Problem
from tests.conftest import other_person
from tests.model import install, says

DECK = uuid.UUID("01900000-0000-7000-8000-000000001000")
AUDIO = uuid.UUID("01900000-0000-7000-8000-000000002000")
MAP = uuid.UUID("6f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11")
NOTES = uuid.UUID("7f1c2a3e-8b4d-4c1f-9a2e-0d3b5c7e9f11")
T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings(), "files_dir", tmp_path)
    return tmp_path


# ── checking what is shared ───────────────────────────────────────────────────


def held() -> Holdings:
    return Holdings(sources=2, sessions={DECK}, maps={MAP})


def test_a_share_takes_only_ready_outputs_of_its_collection() -> None:
    src, outs, note = shares.validate(
        False, [f"session:{DECK}", f"mindmap:{MAP}", f" session:{DECK} "], "  a note  ", held()
    )
    assert not src
    assert outs == [f"session:{DECK}", f"mindmap:{MAP}"], "deduplicated, in order"
    assert note == "a note"

    # Not in the collection, or not ready (a preparing output is not held).
    with pytest.raises(Problem, match="not a ready output") as e:
        shares.validate(False, [f"session:{uuid.uuid4()}"], "", held())
    assert e.value.status == 422 and e.value.detail.endswith("Reload the page and pick again.")
    with pytest.raises(Problem, match="not a ready output"):
        # A map id is not a notes id.
        shares.validate(True, [f"notes:{MAP}"], "", held())
    # Not a key at all.
    for bad in ["deck:x", "session:", f"deck:{DECK}", "session:not-a-uuid"]:
        with pytest.raises(Problem, match="does not name an output"):
            shares.validate(True, [bad], "", held())


def test_a_share_with_nothing_in_it_is_refused() -> None:
    with pytest.raises(Problem) as e:
        shares.validate(False, [], "", held())
    assert e.value.detail == (
        "There is nothing to share. Include the sources or at least one ready output."
    )
    # Sources asked for, but there are none.
    with pytest.raises(Problem):
        shares.validate(True, [], "", Holdings())
    # Sources alone are a share.
    assert shares.validate(True, [], "", held())[0]


def test_a_long_note_is_cut_to_its_limit() -> None:
    _, _, note = shares.validate(True, [], f"  {'é' * 400}  ", held())
    assert len(note) == NOTE_MAX


def test_keys_read_back_as_they_were_written() -> None:
    for raw in [f"session:{DECK}", f"mindmap:{MAP}", f"notes:{NOTES}"]:
        key = Key.parse(raw)
        assert key is not None and key.render() == raw


# ── the feed ──────────────────────────────────────────────────────────────────


def card(
    n: int, title: str, note: str, terms: list[str], reuses: int, updated: int, **counts: int
) -> Card:
    return Card(
        id=uuid.UUID(int=n),
        collection_id=uuid.UUID(int=n),
        owner_id=uuid.UUID(int=0),
        shared_by="",
        title=title,
        note=note,
        cover_version="",
        terms=terms,
        reuses=reuses,
        allow_edits=False,
        created_at=T0,
        updated_at=T0 + timedelta(seconds=updated),
        **counts,
    )


def ids(cards: list[Card]) -> list[int]:
    return [c.id.int for c in cards]


def test_the_feed_sorts_newest_or_most_reused_and_searches_every_field() -> None:
    def entries() -> list[tuple[Card, str]]:
        return [
            (card(1, "Linux Kernel", "", [], 5, 100), "Kernels"),
            (card(2, "Speech", "about MOSHI", [], 1, 300), ""),
            (card(3, "Rust", "", ["Borrowing"], 5, 200), "Ownership"),
        ]

    assert ids(shares.feed(entries(), "", "newest")) == [2, 3, 1], "newest by default"
    assert ids(shares.feed(entries(), "", "reused")) == [3, 1, 2], "most reused, then newest"
    assert ids(shares.feed(entries(), "linux", "newest")) == [1], "title"
    assert ids(shares.feed(entries(), "moshi", "newest")) == [2], "note, any case"
    assert ids(shares.feed(entries(), "ownership", "newest")) == [3], "cover topic"
    assert ids(shares.feed(entries(), " BORROW ", "newest")) == [3], "cover terms"
    assert shares.feed(entries(), "nothing", "newest") == []


def live() -> Live:
    # Only ready decks and audio overviews: `live` leaves out the rest, as
    # its query does.
    return Live(
        sessions={
            DECK: Out("slides", DECK, "Deck", T0, 6),
            AUDIO: Out("audio", AUDIO, "Overview", T0, 3, 61_000),
        },
        maps={MAP: Out("mindmap", MAP, "Map", T0)},
        notes={NOTES: Out("notes", NOTES, "Notes", T0)},
    )


def test_a_view_skips_what_is_gone() -> None:
    outs = shares.included(
        [
            f"notes:{NOTES}",
            f"session:{uuid.uuid4()}",  # deleted since, not ready, or another collection's
            f"session:{AUDIO}",
            f"mindmap:{uuid.uuid4()}",  # deleted since
            f"notes:{NOTES}",
        ],
        live(),
    )
    assert [(o.key, o.kind, o.id) for o in outs] == [
        (f"notes:{NOTES}", "notes", NOTES),
        (f"session:{AUDIO}", "audio", AUDIO),
    ]
    assert outs[1].title == "Overview"


def test_a_card_counts_what_is_included_and_still_there() -> None:
    c = Collection(id=uuid.uuid4(), owner_id=uuid.uuid4(), title="Kernels")
    summary = collections.Summary(c, 4, 1, 1, 1, 1, 0, 0, "f-1")

    def share(include_sources: bool, outputs: list[str]) -> Share:
        return Share(
            id=uuid.uuid4(),
            owner_id=c.owner_id,
            collection_id=c.id,
            include_sources=include_sources,
            outputs=outputs,
            note="",
            reuses=3,
            created_at=T0,
            updated_at=T0,
        )

    from opennotebook.cover import CoverSpec

    spec = CoverSpec(topic="Kernels", terms=["Scheduling"])
    sh = share(False, [f"session:{DECK}", f"session:{AUDIO}", f"notes:{uuid.uuid4()}"])
    got = shares.card(sh, "", summary, shares.included(sh.outputs, live()), spec)
    assert (got.sources, got.decks, got.audios, got.maps, got.notes, got.reuses) == (
        0,
        1,
        1,
        0,
        0,
        3,
    )
    assert got.title == "Kernels" and got.terms == ["Scheduling"]
    got = shares.card(share(True, []), "", summary, [], spec)
    assert got.sources == 4, "sources counted only when included"


# ── copying ───────────────────────────────────────────────────────────────────


def test_an_audio_path_moves_to_the_copys_directory() -> None:
    old, new = uuid.uuid4(), uuid.uuid4()
    assert shares.moved(f"audio/{old}/s0l0.wav", old, new) == f"audio/{new}/s0l0.wav"
    # Recorded under a directory that has since moved.
    assert shares.moved(f"/old/var/audio/{old}/x/s0l0.wav", old, new) == (
        f"/old/var/audio/{new}/x/s0l0.wav"
    )
    assert shares.moved("/elsewhere/a.wav", old, new) == "/elsewhere/a.wav"
    # The deck it names, wherever it is in the row.
    assert shares.moved(
        {"collection": str(old), "lines": [{"audio_path": f"audio/{old}/a.wav"}], "n": 3},
        old,
        new,
    ) == {"collection": str(new), "lines": [{"audio_path": f"audio/{new}/a.wav"}], "n": 3}


def test_a_copy_keeps_its_cover_only_when_it_holds_everything() -> None:
    h = Holdings(sources=2, sessions={DECK}, maps={MAP})
    inc = [Out("slides", DECK, "", T0), Out("mindmap", MAP, "", T0)]
    two = [uuid.uuid4(), uuid.uuid4()]
    assert shares.plan_of(True, two, inc).complete(h)
    assert not shares.plan_of(False, two, inc).complete(h), "without the sources"
    assert not shares.plan_of(True, two, inc[:1]).complete(h), "without the map"
    assert Plan().empty() and not shares.plan_of(True, two, []).empty()


def test_a_copy_never_writes_over_what_is_there(files: Path) -> None:
    storage.put("decks/a/studio/intro.html", b"<p>slide</p>")
    assert storage.copy_tree("decks/a", "decks/b")
    assert (files / "decks/b/studio/intro.html").read_bytes() == b"<p>slide</p>"
    with pytest.raises(FileExistsError):
        storage.copy_tree("decks/a", "decks/b")
    assert not storage.copy_tree("decks/none", "decks/c"), "nothing to copy"
    storage.put("uploads/x.md", b"x")
    with pytest.raises(FileExistsError):
        storage.copy_new("uploads/x.md", "uploads/x.md")


# ── through the routes ────────────────────────────────────────────────────────


async def _me(client: AsyncClient, headers: dict[str, str] | None = None) -> str:
    return (await client.get("/api/me", headers=headers)).json()["id"]


async def _collection(
    client: AsyncClient, title: str, headers: dict[str, str] | None = None
) -> str:
    r = await client.post("/api/collections", json={"title": title}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _note(
    client: AsyncClient, cid: str, body: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    r = await client.post(
        f"/api/collections/{cid}/sources",
        json={"kind": "text", "text": body, "title": body.split()[0] + " note"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()[0]["source"]


async def _rows(owner: str, cid: str, *, state: str = "ready") -> dict[str, str]:
    """A deck, a map and notes in a collection, written directly: their making
    is ported later."""
    async with engine().begin() as c:

        async def one(sql: str, **kw: Any) -> str:
            return str((await c.execute(text(sql), {"o": owner, "c": cid, **kw})).scalar_one())

        deck = await one(
            "INSERT INTO sessions (owner_id, collection_id, kind, title, state, slides, pinned,"
            " spent_usd, spent_known) VALUES (:o, :c, 'slides', 'Deck', :state,"
            " CAST(:slides AS jsonb), true, 0.3, true) RETURNING id",
            state=state,
            slides='[{"title": "Intro", "lines": []}]',
        )
        slides = [
            {
                "title": "Intro",
                "slide_ref": {"collection": deck},
                "lines": [{"line_id": "s0l0", "audio_path": f"audio/{deck}/s0l0.wav"}],
            }
        ]
        await c.execute(
            text(
                "UPDATE sessions SET slides = CAST(:s AS jsonb), deck_ref = CAST(:d AS jsonb)"
                " WHERE id = :id"
            ),
            {
                "id": deck,
                "s": json.dumps(slides),
                "d": json.dumps({"collection": deck, "presentation": "studio"}),
            },
        )
        mid = await one(
            "INSERT INTO mindmaps (owner_id, collection_id, title, root, node_count)"
            " VALUES (:o, :c, 'Map', CAST(:root AS jsonb), 1) RETURNING id",
            root='{"name": "Reefs", "children": [{"name": "Polyps", "children": []}]}',
        )
        nid = await one(
            "INSERT INTO study_notes (owner_id, collection_id, title, body) VALUES (:o, :c,"
            " 'Notes', CAST(:body AS jsonb)) RETURNING id",
            body='{"overview": "Reefs are built by polyps [1].", "ideas": []}',
        )
    storage.put(f"decks/{deck}/studio/intro.html", b"<p>slide</p>")
    storage.put(f"audio/{deck}/s0l0.wav", b"RIFF")
    return {"deck": deck, "map": mid, "notes": nid}


REEF = "Reefs are built by colonies of coral polyps over centuries, slowly and patiently."
KELP = "Kelp forests grow in cold water along rocky coasts, and shelter many fish species."


async def test_a_share_is_reused_counted_and_removed_with_its_collection(
    client: AsyncClient, files: Path
) -> None:
    r = await client.post(f"/api/collections/{uuid.uuid4()}/shares", json={"include_sources": True})
    assert r.status_code == 404, "unknown collection"

    cid = await _collection(client, "zz share test")
    me = await _me(client)
    await _note(client, cid, REEF)
    up = await client.post(
        f"/api/collections/{cid}/sources/files",
        files={"files": ("kelp.md", f"# Kelp\n\n{KELP}".encode())},
    )
    assert up.status_code == 201, up.text
    made = await _rows(me, cid)

    assert (await client.get(f"/api/collections/{cid}/share")).json()["share"] is None
    r = await client.post(
        f"/api/collections/{cid}/shares",
        json={"include_sources": False, "outputs": ["mindmap:nope"]},
    )
    assert r.status_code == 422 and "does not name an output" in r.json()["detail"]
    r = await client.post(
        f"/api/collections/{cid}/shares",
        json={
            "include_sources": True,
            "outputs": [f"mindmap:{made['map']}", f"session:{made['deck']}"],
            "note": " hi ",
        },
    )
    assert r.status_code == 200, r.text
    sh = r.json()
    assert (sh["note"], sh["reuses"], sh["collection_id"]) == ("hi", 0, cid)
    assert (await client.get(f"/api/collections/{cid}/share")).json()["share"]["id"] == sh["id"]
    listed = (await client.get(f"/api/collections/{cid}")).json()["collection"]
    assert listed["shared"] and listed["reused_from"] is None

    # Someone else reuses it.
    them = await other_person(client, "them@example.com")
    r = await client.post(f"/api/shares/{sh['id']}/reuse", headers=them)
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["title"] == "zz share test" and not copy["title_auto"]
    assert copy["reused_from"] == sh["id"] and not copy["shared"]
    assert (copy["sources"], copy["decks"], copy["maps"], copy["notes"]) == (2, 1, 1, 0)
    again = (await client.get(f"/api/collections/{cid}/share")).json()["share"]
    assert again["reuses"] == 1, "counted"
    assert again["created_at"] == sh["created_at"]
    assert [c["id"] for c in (await client.get("/api/collections", headers=them)).json()] == [
        copy["id"]
    ]

    # Everything in the copy is the copy's own: rows, ids and files.
    detail = (await client.get(f"/api/collections/{copy['id']}", headers=them)).json()
    (deck,) = detail["outputs"]
    assert deck["id"] != made["deck"] and not deck["pinned"]
    assert Decimal(deck["spent_usd"]) == 0, "the copy cost nothing"
    full = (await client.get(f"/api/sessions/{deck['id']}", headers=them)).json()
    line = full["slides"][0]["lines"][0]
    assert line["audio_path"] == f"audio/{deck['id']}/s0l0.wav"
    assert full["slides"][0]["slide_ref"]["collection"] == deck["id"]
    assert (files / line["audio_path"]).read_bytes() == b"RIFF"
    assert (files / f"decks/{deck['id']}/studio/intro.html").is_file()
    (m,) = (await client.get(f"/api/collections/{copy['id']}/mindmaps", headers=them)).json()
    assert m["id"] != made["map"] and m["title"] == "Map"
    srcs = (await client.get(f"/api/collections/{copy['id']}/sources", headers=them)).json()
    assert [s["title"] for s in srcs] == ["Reefs note", "kelp"]
    copied_file = next(iter((files / "uploads" / copy["id"]).iterdir()))
    assert copied_file.read_bytes().endswith(KELP.encode())
    # Searchable without a model call: the passages came with the sources.
    async with sessionmaker()() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM chunks WHERE collection_id = :c"), {"c": copy["id"]}
        )
    assert n == 2

    view = (await client.get(f"/api/shares/{sh['id']}", headers=them)).json()
    assert (view["card"]["reuses"], len(view["sources"]), len(view["outputs"])) == (1, 2, 2)
    assert not view["card"]["mine"]

    # Deleting the collection takes its share; the copy stays whole.
    assert (await client.delete(f"/api/collections/{cid}")).status_code == 204
    storage.remove_tree(f"uploads/{cid}")
    for d in ("decks", "audio"):
        storage.remove_tree(f"{d}/{made['deck']}")
    r = await client.get(f"/api/shares/{sh['id']}", headers=them)
    assert r.status_code == 404
    assert r.json()["detail"] == (
        "That shared collection is no longer there. Reload the page to see what is."
    )
    assert (await client.post(f"/api/shares/{sh['id']}/reuse", headers=them)).status_code == 404
    still = (await client.get(f"/api/collections/{copy['id']}", headers=them)).json()
    assert (still["collection"]["maps"], still["collection"]["sources"]) == (1, 2)
    assert still["collection"]["reused_from"] == sh["id"], "kept after the share went"
    assert (files / line["audio_path"]).is_file()
    assert copied_file.is_file()


async def test_a_reuse_without_sources_copies_none(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    await _note(client, cid, REEF)
    made = await _rows(await _me(client), cid)
    sh = (
        await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": False, "outputs": [f"notes:{made['notes']}"]},
        )
    ).json()
    view = (await client.get(f"/api/shares/{sh['id']}")).json()
    assert view["sources"] == [] and view["card"]["sources"] == 0
    copy = (await client.post(f"/api/shares/{sh['id']}/reuse")).json()
    assert (copy["sources"], copy["notes"], copy["decks"]) == (0, 1, 0)


def _tree(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*"))


async def test_a_failed_reuse_leaves_nothing_of_the_copy(
    client: AsyncClient, files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await _collection(client, "Reefs")
    await client.post(
        f"/api/collections/{cid}/sources/files",
        files={"files": ("reef.md", f"# Reef\n\n{REEF}".encode())},
    )
    made = await _rows(await _me(client), cid)
    sh = (
        await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": True, "outputs": [f"session:{made['deck']}"]},
        )
    ).json()
    them = await other_person(client, "them@example.com")
    before = _tree(files)

    real = storage.copy_tree

    def fails(src: str, dst: str) -> bool:
        real(src, dst)
        raise OSError("No space left on device")

    monkeypatch.setattr(storage, "copy_tree", fails)
    with pytest.raises(OSError, match="No space"):
        async with sessionmaker()() as s, s.begin():
            await shares.reuse(s, uuid.UUID(await _me(client, them)), uuid.UUID(sh["id"]))
    assert (await client.get("/api/collections", headers=them)).json() == []
    assert _tree(files) == before, "no file left"
    assert (await client.get(f"/api/collections/{cid}/share")).json()["share"]["reuses"] == 0


async def test_a_copy_keeps_the_cover_when_it_holds_everything(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    await _note(client, cid, REEF)
    me = await _me(client)
    made = await _rows(me, cid)
    spec = '{"topic": "Coral reefs", "terms": ["Polyps"], "motif": "", "palette": "", "layout": ""}'
    async with engine().begin() as c:
        await c.execute(
            text(
                "UPDATE collections SET cover = CAST(:s AS jsonb), cover_version = 'g-1'"
                " WHERE id = :c"
            ),
            {"s": spec, "c": cid},
        )
    everything = [f"session:{made['deck']}", f"mindmap:{made['map']}", f"notes:{made['notes']}"]
    sh = (
        await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": True, "outputs": everything},
        )
    ).json()
    # Straight to the domain: through the route, a background refresh would
    # try the design and record what it tried.
    async with sessionmaker()() as s, s.begin():
        whole = await shares.reuse(s, uuid.UUID(me), uuid.UUID(sh["id"]))
    await client.patch(f"/api/shares/{sh['id']}", json={"outputs": everything[:1]})
    async with sessionmaker()() as s, s.begin():
        part = await shares.reuse(s, uuid.UUID(me), uuid.UUID(sh["id"]))
    async with sessionmaker()() as s:
        original = await s.get(Collection, uuid.UUID(cid))
    assert original is not None and original.cover is not None
    assert whole.cover == part.cover == original.cover, "the cover comes along"
    assert whole.cover_from, "current: no model call"
    assert part.cover_from == "", "designed again from what it holds"


async def test_the_feed_shows_everyones_shares(client: AsyncClient) -> None:
    them = await other_person(client, "them@example.com")
    mine = await _collection(client, "Reefs")
    await _note(client, mine, REEF)
    theirs = await _collection(client, "Kelp", them)
    await _note(client, theirs, KELP, them)
    hidden = await _collection(client, "Not shared", them)
    await _note(client, hidden, KELP, them)
    for cid, h in ((mine, None), (theirs, them)):
        r = await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": True, "note": "for divers"},
            headers=h,
        )
        assert r.status_code == 200, r.text

    for h in (None, them):
        feed = (await client.get("/api/shares", headers=h)).json()
        assert [c["title"] for c in feed] == ["Kelp", "Reefs"], "newest first, both people's"
    feed = (await client.get("/api/shares")).json()
    assert [c["mine"] for c in feed] == [False, True]
    assert feed[0]["sources"] == 1 and feed[0]["note"] == "for divers"
    found = (await client.get("/api/shares", params={"query": "KELP"})).json()
    assert [c["title"] for c in found] == ["Kelp"]
    r = await client.get("/api/shares", params={"sort": "oldest"})
    assert r.status_code == 422

    # The cover, as its owner sees it.
    r = await client.get(
        f"/api/shares/{feed[0]['id']}/cover", params={"v": feed[0]["cover_version"]}
    )
    assert r.status_code == 200 and "<svg" in r.text
    assert "immutable" in r.headers["cache-control"]

    # A person whose account is turned off is out of the feed.
    async with engine().begin() as c:
        await c.execute(
            text("UPDATE users SET disabled_at = now() WHERE email = 'them@example.com'")
        )
    assert [c["title"] for c in (await client.get("/api/shares")).json()] == ["Reefs"]
    assert (await client.get(f"/api/shares/{feed[0]['id']}")).status_code == 404


async def test_only_the_owner_changes_or_stops_a_share(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    await _note(client, cid, REEF)
    sh = (
        await client.post(f"/api/collections/{cid}/shares", json={"include_sources": True})
    ).json()
    them = await other_person(client, "them@example.com")

    for method, body in (("PATCH", {"note": "mine now"}), ("DELETE", None)):
        r = await client.request(method, f"/api/shares/{sh['id']}", json=body, headers=them)
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == (
            "Only the person who shared this collection can change or stop its share. "
            "Reuse it to make a copy of your own."
        )
    # Nor through the collection, which is not theirs.
    r = await client.post(
        f"/api/collections/{cid}/shares", json={"include_sources": False}, headers=them
    )
    assert r.status_code == 404
    assert (await client.get(f"/api/collections/{cid}/share", headers=them)).status_code == 404

    r = await client.patch(f"/api/shares/{sh['id']}", json={"note": "  for divers "})
    assert r.json()["note"] == "for divers" and r.json()["include_sources"]
    r = await client.patch(f"/api/shares/{sh['id']}", json={"include_sources": False})
    assert r.status_code == 422, "nothing would be left to share"

    assert (await client.delete(f"/api/shares/{sh['id']}")).status_code == 204
    assert (await client.get(f"/api/collections/{cid}/share")).json()["share"] is None
    r = await client.delete(f"/api/shares/{sh['id']}")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("That shared collection is no longer there.")
    assert (await client.get("/api/shares", headers=them)).json() == []


async def test_what_a_share_leaves_out_is_not_reachable(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    src = await _note(client, cid, REEF)
    me = await _me(client)
    made = await _rows(me, cid)
    preparing = await _rows(me, cid, state="preparing")
    sh = (
        await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": False, "outputs": [f"mindmap:{made['map']}"]},
        )
    ).json()
    them = await other_person(client, "them@example.com")
    base = f"/api/shares/{sh['id']}"

    r = await client.get(f"{base}/mindmaps/{made['map']}", headers=them)
    assert r.status_code == 200 and r.json()["root"]["name"] == "Reefs"
    for path, what in [
        (f"{base}/sessions/{made['deck']}", "That output"),
        (f"{base}/notes/{made['notes']}", "That output"),
        (f"{base}/mindmaps/{preparing['map']}", "That output"),
        (f"{base}/sessions/{preparing['deck']}", "That output"),
        # A map's id is not a deck's.
        (f"{base}/sessions/{made['map']}", "That output"),
        (f"{base}/sources/{src['name']}", "That source"),
    ]:
        r = await client.get(path, headers=them)
        assert r.status_code == 404, path
        assert r.json()["detail"] == f"{what} is no longer there. Reload the page to see what is."
    # The collection's own routes stay its owner's.
    for path in [
        f"/api/collections/{cid}",
        f"/api/collections/{cid}/mindmaps/{made['map']}",
        f"/api/collections/{cid}/sources/{src['name']}",
        f"/api/sessions/{made['deck']}",
        f"/api/collections/{cid}/cover",
    ]:
        assert (await client.get(path, headers=them)).status_code == 404, path

    # Shared, it is readable; unshared again, it is not.
    await client.patch(
        base,
        json={
            "include_sources": True,
            "outputs": [f"session:{made['deck']}", f"notes:{made['notes']}"],
        },
    )
    r = await client.get(f"{base}/sources/{src['name']}", headers=them)
    assert r.status_code == 200 and r.json()["text"].endswith(REEF)
    r = await client.get(f"{base}/sessions/{made['deck']}", headers=them)
    assert r.status_code == 200 and r.json()["slides"][0]["title"] == "Intro"
    r = await client.get(f"{base}/notes/{made['notes']}", headers=them)
    assert r.status_code == 200 and r.json()["overview"].startswith("Reefs")
    assert (await client.get(f"{base}/mindmaps/{made['map']}", headers=them)).status_code == 404
    view = (await client.get(base, headers=them)).json()
    assert [(o["kind"], o["title"], o["parts"]) for o in view["outputs"]] == [
        ("slides", "Deck", 1),
        ("notes", "Notes", 0),
    ]

    # An output deleted after it was shared drops out, and its key stays.
    assert (await client.delete(f"/api/sessions/{made['deck']}")).status_code == 204
    view = (await client.get(base, headers=them)).json()
    assert [o["kind"] for o in view["outputs"]] == ["notes"]
    assert view["card"]["decks"] == 0
    assert (
        f"session:{made['deck']}"
        in (await client.get(f"/api/collections/{cid}/share")).json()["share"]["outputs"]
    )


async def test_a_reuse_of_nothing_is_refused(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    made = await _rows(await _me(client), cid)
    sh = (
        await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": False, "outputs": [f"mindmap:{made['map']}"]},
        )
    ).json()
    assert (
        await client.delete(f"/api/collections/{cid}/mindmaps/{made['map']}")
    ).status_code == 204
    r = await client.post(f"/api/shares/{sh['id']}/reuse")
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Nothing this share included is still there")


async def test_a_reused_and_shared_collection_says_so_in_the_list(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    await _note(client, cid, REEF)
    sh = (
        await client.post(f"/api/collections/{cid}/shares", json={"include_sources": True})
    ).json()
    copy = (await client.post(f"/api/shares/{sh['id']}/reuse")).json()
    listed = {c["id"]: c for c in (await client.get("/api/collections")).json()}
    assert listed[copy["id"]]["reused_from"] == sh["id"] and not listed[copy["id"]]["shared"]
    assert listed[cid]["reused_from"] is None, "none when it was not reused"
    assert listed[cid]["shared"]
    # The original's name, as it is now, for as long as its share is there.
    assert (listed[copy["id"]]["reused_from_title"], listed[cid]["reused_from_title"]) == (
        "Reefs",
        None,
    )
    await client.patch(f"/api/collections/{cid}", json={"title": "Coral reefs"})
    got = (await client.get(f"/api/collections/{copy['id']}")).json()["collection"]
    assert got["reused_from_title"] == "Coral reefs"
    assert (await client.delete(f"/api/shares/{sh['id']}")).status_code == 204
    got = (await client.get(f"/api/collections/{copy['id']}")).json()["collection"]
    assert got["reused_from"] == sh["id"] and got["reused_from_title"] is None


# ── how often it was reused, and read-only copies ─────────────────────────────
#
# How often a share was reused, as its owner sees it, and whether the copies
# people make are theirs to change: a share that does not allow edits gives
# read-only copies, and the server refuses every change to one.


def refusal(original: str | None) -> str:
    of = f"“{original}”" if original else "a shared collection"
    return (
        f"This is a read-only copy of {of}. Its author did not allow edits, "
        "so it cannot be changed or shared. Start a new collection to make one of your own."
    )


async def _shared(
    client: AsyncClient, *, allow_edits: bool | None = None, title: str = "Reefs"
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """A collection with a source, a deck, a map and notes, all shared."""
    cid = await _collection(client, title)
    await _note(client, cid, REEF)
    made = await _rows(await _me(client), cid)
    body: dict[str, Any] = {
        "include_sources": True,
        "outputs": [f"session:{made['deck']}", f"mindmap:{made['map']}", f"notes:{made['notes']}"],
    }
    if allow_edits is not None:
        body["allow_edits"] = allow_edits
    r = await client.post(f"/api/collections/{cid}/shares", json=body)
    assert r.status_code == 200, r.text
    return cid, made, r.json()


async def _reuse(client: AsyncClient, share_id: str, headers: dict[str, str]) -> dict[str, Any]:
    r = await client.post(f"/api/shares/{share_id}/reuse", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def _held(client: AsyncClient, cid: str, h: dict[str, str]) -> dict[str, Any]:
    """What a copy holds, to see that a refused call changed nothing."""
    c = (await client.get(f"/api/collections/{cid}", headers=h)).json()
    maps = (await client.get(f"/api/collections/{cid}/mindmaps", headers=h)).json()
    notes = (await client.get(f"/api/collections/{cid}/notes", headers=h)).json()
    srcs = (await client.get(f"/api/collections/{cid}/sources", headers=h)).json()
    return {
        "title": c["collection"]["title"],
        "shared": c["collection"]["shared"],
        "outputs": sorted((o["id"], o["title"]) for o in c["outputs"]),
        "maps": sorted((m["id"], m["title"]) for m in maps),
        "notes": sorted((n["id"], n["title"]) for n in notes),
        "sources": sorted(s["name"] for s in srcs),
    }


# ── the count, on the owner's side ────────────────────────────────────────────


async def test_a_shared_collection_says_how_often_it_was_reused(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    await _note(client, cid, REEF)
    plain = await _collection(client, "Kelp")
    await _note(client, plain, KELP)
    sh = (
        await client.post(f"/api/collections/{cid}/shares", json={"include_sources": True})
    ).json()

    listed = {c["id"]: c for c in (await client.get("/api/collections")).json()}
    assert (listed[cid]["shared"], listed[cid]["reuses"]) == (True, 0)
    assert (listed[plain]["shared"], listed[plain]["reuses"]) == (False, 0)

    them = await other_person(client, "them@example.com")
    for _ in range(2):
        await _reuse(client, sh["id"], them)
    listed = {c["id"]: c for c in (await client.get("/api/collections")).json()}
    assert listed[cid]["reuses"] == 2
    one = (await client.get(f"/api/collections/{cid}")).json()["collection"]
    assert one["reuses"] == 2
    # The copies are not shared, so they have no count of their own.
    for c in (await client.get("/api/collections", headers=them)).json():
        assert (c["shared"], c["reuses"]) == (False, 0)

    # Stopped sharing: nothing to count.
    await client.delete(f"/api/shares/{sh['id']}")
    one = (await client.get(f"/api/collections/{cid}")).json()["collection"]
    assert (one["shared"], one["reuses"]) == (False, 0)


# ── allow_edits decides the copy, when it is made ─────────────────────────────


async def test_a_share_gives_read_only_copies_unless_it_allows_edits(client: AsyncClient) -> None:
    cid, _, sh = await _shared(client)
    assert sh["allow_edits"] is False, "off unless asked for"
    them = await other_person(client, "them@example.com")
    (card,) = (await client.get("/api/shares", headers=them)).json()
    assert card["allow_edits"] is False
    view = (await client.get(f"/api/shares/{sh['id']}", headers=them)).json()
    assert view["card"]["allow_edits"] is False

    locked = await _reuse(client, sh["id"], them)
    assert locked["read_only"] and locked["reused_from"] == sh["id"]

    r = await client.patch(f"/api/shares/{sh['id']}", json={"allow_edits": True})
    assert r.status_code == 200 and r.json()["allow_edits"] is True
    assert (await client.get(f"/api/shares/{sh['id']}")).json()["card"]["allow_edits"]
    free = await _reuse(client, sh["id"], them)
    assert not free["read_only"]

    # What the share says now does not change the copies made before.
    r = await client.patch(f"/api/shares/{sh['id']}", json={"note": "for divers"})
    assert r.json()["allow_edits"] is True, "kept when not sent"
    await client.patch(f"/api/shares/{sh['id']}", json={"allow_edits": False})
    listed = {c["id"]: c for c in (await client.get("/api/collections", headers=them)).json()}
    assert listed[locked["id"]]["read_only"] and not listed[free["id"]]["read_only"]
    await client.patch(f"/api/shares/{sh['id']}", json={"allow_edits": True})
    listed = {c["id"]: c for c in (await client.get("/api/collections", headers=them)).json()}
    assert listed[locked["id"]]["read_only"], "a read-only copy stays read-only"

    # Setting the share again through the collection, the box unticked.
    r = await client.post(
        f"/api/collections/{cid}/shares", json={"include_sources": True, "allow_edits": False}
    )
    assert r.json()["allow_edits"] is False
    assert (await _reuse(client, sh["id"], them))["read_only"]
    # And none of mine is read-only.
    assert not any(c["read_only"] for c in (await client.get("/api/collections")).json())


# ── a read-only copy ──────────────────────────────────────────────────────────


async def _read_only_copy(
    client: AsyncClient,
) -> tuple[str, dict[str, str], dict[str, Any], dict[str, str]]:
    """Someone's read-only copy of "Reefs", and the ids of what it holds."""
    _, _, sh = await _shared(client, allow_edits=False)
    them = await other_person(client, "them@example.com")
    copy = await _reuse(client, sh["id"], them)
    assert copy["read_only"]
    cid = copy["id"]
    (deck,) = (await client.get(f"/api/collections/{cid}", headers=them)).json()["outputs"]
    (m,) = (await client.get(f"/api/collections/{cid}/mindmaps", headers=them)).json()
    (n,) = (await client.get(f"/api/collections/{cid}/notes", headers=them)).json()
    (src,) = (await client.get(f"/api/collections/{cid}/sources", headers=them)).json()
    ids = {"deck": deck["id"], "map": m["id"], "notes": n["id"], "source": src["name"]}
    return cid, them, sh, ids


def refused_calls(cid: str, ids: dict[str, str]) -> list[tuple[str, str, dict[str, Any]]]:
    """Every call that changes a collection or what it holds, or shares it."""
    c = f"/api/collections/{cid}"
    return [
        ("POST", f"{c}/sources", {"json": {"kind": "text", "text": KELP}}),
        ("POST", f"{c}/sources", {"json": {"kind": "urls", "urls": ["https://example.com/"]}}),
        ("POST", f"{c}/sources/files", {"files": {"files": ("kelp.md", KELP.encode())}}),
        ("DELETE", f"{c}/sources/{ids['source']}", {}),
        ("POST", f"{c}/research", {"json": {"topic": "kelp"}}),
        ("PATCH", c, {"json": {"title": "Mine now"}}),
        ("PATCH", c, {"json": {"title": ""}}),
        ("PATCH", c, {"json": {"title": "Mine now", "pinned": True}}),
        ("POST", f"{c}/cover", {}),
        ("POST", f"{c}/outputs", {"json": {"kind": "slides"}}),
        ("POST", f"{c}/outputs/estimate", {"json": {"kind": "audio"}}),
        ("POST", f"{c}/mindmaps", {"json": {}}),
        ("POST", f"{c}/notes", {"json": {}}),
        ("PATCH", f"{c}/mindmaps/{ids['map']}", {"json": {"title": "Mine"}}),
        ("DELETE", f"{c}/mindmaps/{ids['map']}", {}),
        ("PATCH", f"{c}/notes/{ids['notes']}", {"json": {"title": "Mine"}}),
        ("DELETE", f"{c}/notes/{ids['notes']}", {}),
        ("PATCH", f"/api/sessions/{ids['deck']}", {"json": {"title": "Mine"}}),
        ("DELETE", f"/api/sessions/{ids['deck']}", {}),
        ("POST", f"{c}/shares", {"json": {"include_sources": True}}),
        ("POST", f"{c}/shares", {"json": {"include_sources": True, "allow_edits": True}}),
    ]


async def test_a_read_only_copy_refuses_every_change(client: AsyncClient) -> None:
    cid, them, _, ids = await _read_only_copy(client)
    before = await _held(client, cid, them)
    for method, path, kw in refused_calls(cid, ids):
        r = await client.request(method, path, headers=them, **kw)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["detail"] == refusal("Reefs"), (method, path)
    assert await _held(client, cid, them) == before, "nothing changed"
    assert (await client.get(f"/api/collections/{cid}/share", headers=them)).json()["share"] is None


async def test_a_read_only_copy_still_reads_asks_pins_plays_and_goes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid, them, _, ids = await _read_only_copy(client)
    c = f"/api/collections/{cid}"
    for path in [
        c,
        f"{c}/sources",
        f"{c}/sources/{ids['source']}",
        f"{c}/mindmaps/{ids['map']}",
        f"{c}/notes/{ids['notes']}",
        f"/api/sessions/{ids['deck']}",
        f"{c}/chat",
        f"{c}/cover",
    ]:
        r = await client.get(path, headers=them)
        assert r.status_code == 200, (path, r.text)

    r = await client.patch(c, json={"pinned": True}, headers=them)
    assert r.status_code == 200 and r.json()["pinned"]
    r = await client.patch(f"/api/sessions/{ids['deck']}", json={"pinned": True}, headers=them)
    assert r.status_code == 200 and r.json()["pinned"]
    r = await client.put(
        f"/api/sessions/{ids['deck']}/playback",
        json={"slide_ordinal": 1, "line_id": "s0l0", "offset_ms": 900, "state": "paused"},
        headers=them,
    )
    assert r.status_code == 200, r.text
    assert (await client.delete(f"{c}/chat", headers=them)).status_code == 204

    model = install(monkeypatch)
    model.answers(says("Polyps build them [1]."))
    r = await client.post(f"{c}/ask", json={"question": "Who builds reefs?"}, headers=them)
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "Polyps build them [1]."

    assert (await client.delete(c, headers=them)).status_code == 204
    assert (await client.get(c, headers=them)).status_code == 404


async def test_the_refusal_names_the_original_as_it_is_now(client: AsyncClient) -> None:
    cid, them, sh, _ = await _read_only_copy(client)
    original = (await client.get(f"/api/shares/{sh['id']}")).json()["card"]["collection_id"]
    await client.patch(f"/api/collections/{original}", json={"title": "Coral reefs"})
    r = await client.patch(f"/api/collections/{cid}", json={"title": "x"}, headers=them)
    assert r.json()["detail"] == refusal("Coral reefs")

    # The share gone, the copy is still read-only, and says so without a name.
    assert (await client.delete(f"/api/shares/{sh['id']}")).status_code == 204
    r = await client.post(
        f"/api/collections/{cid}/sources", json={"kind": "text", "text": KELP}, headers=them
    )
    assert r.status_code == 403 and r.json()["detail"] == refusal(None)
    listed = (await client.get(f"/api/collections/{cid}", headers=them)).json()["collection"]
    assert listed["read_only"]


# ── an editable copy ──────────────────────────────────────────────────────────


async def test_an_editable_copy_is_changed_and_shared_again(client: AsyncClient) -> None:
    _, _, sh = await _shared(client, allow_edits=True)
    assert sh["allow_edits"] is True
    them = await other_person(client, "them@example.com")
    copy = await _reuse(client, sh["id"], them)
    assert not copy["read_only"]
    cid = copy["id"]
    c = f"/api/collections/{cid}"

    r = await client.patch(c, json={"title": "My reefs"}, headers=them)
    assert r.status_code == 200 and r.json()["title"] == "My reefs"
    await _note(client, cid, KELP, them)
    (m,) = (await client.get(f"{c}/mindmaps", headers=them)).json()
    r = await client.patch(f"{c}/mindmaps/{m['id']}", json={"title": "Mine"}, headers=them)
    assert r.status_code == 200
    (deck,) = (await client.get(c, headers=them)).json()["outputs"]
    assert (await client.delete(f"/api/sessions/{deck['id']}", headers=them)).status_code == 204

    # Shared again, with edits off: copies of the copy are read-only, and
    # Discover still knows where it came from.
    r = await client.post(f"{c}/shares", json={"include_sources": True}, headers=them)
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["allow_edits"] is False
    listed = (await client.get(c, headers=them)).json()["collection"]
    assert listed["shared"] and listed["reused_from"] == sh["id"]
    feed = {s["id"]: s for s in (await client.get("/api/shares")).json()}
    assert feed[again["id"]]["title"] == "My reefs" and not feed[again["id"]]["mine"]
    third = await _reuse(client, again["id"], {})
    assert third["read_only"] and third["reused_from"] == again["id"]
    r = await client.patch(f"/api/collections/{third['id']}", json={"title": "x"})
    assert r.json()["detail"] == refusal("My reefs")
    # The copy that was reused counts it.
    listed = (await client.get(c, headers=them)).json()["collection"]
    assert listed["reuses"] == 1


async def test_an_ordinary_collection_is_not_read_only(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    got = (await client.get(f"/api/collections/{cid}")).json()["collection"]
    assert got["read_only"] is False and got["reuses"] == 0
    r = await client.patch(f"/api/collections/{cid}", json={"title": "Coral"})
    assert r.status_code == 200


async def test_the_share_dialog_opens_on_what_can_be_shared(client: AsyncClient) -> None:
    cid = await _collection(client, "Reefs")
    await _note(client, cid, REEF)
    me = await _me(client)
    made = await _rows(me, cid)
    async with engine().begin() as c:
        await c.execute(text("UPDATE mindmaps SET title = '' WHERE id = :m"), {"m": made["map"]})
        # A deck still being made cannot be shared yet.
        await c.execute(
            text(
                "INSERT INTO sessions (owner_id, collection_id, kind, title, state)"
                " VALUES (:o, :c, 'slides', 'Later', 'preparing')"
            ),
            {"o": me, "c": cid},
        )
    d = (await client.get(f"/api/collections/{cid}/share")).json()
    assert d["share"] is None and d["sources"] == 1 and d["note_max"] == 280
    keys = {i["key"]: i for i in d["items"]}
    assert set(keys) == {
        f"session:{made['deck']}",
        f"mindmap:{made['map']}",
        f"notes:{made['notes']}",
    }
    assert keys[f"session:{made['deck']}"]["kind"] == "slides"
    # An output with no name is called by its kind.
    assert keys[f"mindmap:{made['map']}"]["title"] == "Mind map"
    # Not shared yet: everything there is, ticked; edits off.
    assert d["include_sources"] and sorted(d["picked"]) == sorted(keys)
    assert (d["note"], d["allow_edits"]) == ("", False)

    sh = await client.post(
        f"/api/collections/{cid}/shares",
        json={
            "include_sources": False,
            "outputs": [f"notes:{made['notes']}"],
            "note": "for divers",
            "allow_edits": True,
        },
    )
    assert sh.status_code == 200, sh.text
    d = (await client.get(f"/api/collections/{cid}/share")).json()
    # Shared: the share's own choices.
    assert d["share"]["id"] == sh.json()["id"]
    assert (d["include_sources"], d["picked"], d["note"], d["allow_edits"]) == (
        False,
        [f"notes:{made['notes']}"],
        "for divers",
        True,
    )


async def test_untitled_shared_things_are_shown_by_what_they_are(client: AsyncClient) -> None:
    cid = await _collection(client, "")
    await _note(client, cid, REEF)
    made = await _rows(await _me(client), cid)
    async with engine().begin() as c:
        await c.execute(text("UPDATE mindmaps SET title = '' WHERE id = :m"), {"m": made["map"]})
        await c.execute(text("UPDATE collections SET title = '' WHERE id = :c"), {"c": cid})
    sh = (
        await client.post(
            f"/api/collections/{cid}/shares",
            json={"include_sources": True, "outputs": [f"mindmap:{made['map']}"]},
        )
    ).json()
    view = (await client.get(f"/api/shares/{sh['id']}")).json()
    assert view["card"]["display_title"] == "Untitled collection"
    assert [o["display_title"] for o in view["outputs"]] == ["Untitled mind map"]
    # Its sources come described, as a collection's own do.
    assert view["sources"][0]["detail"].startswith("note · ")
    items = (await client.get("/api/shares/items", params={"kind": "mindmap"})).json()["items"]
    assert [(i["display_title"], i["collection_display_title"]) for i in items] == [
        ("Untitled mind map", "Untitled collection")
    ]
