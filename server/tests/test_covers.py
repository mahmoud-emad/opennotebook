"""Collection covers. Ported from the Rust server's tests in `cover/mod.rs`,
`cover/render.rs`, `cover/motifs.rs` and the cover tests of `collection.rs`,
with the routes that serve and redesign a cover."""

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import cover
from opennotebook.cover import CoverSpec, NotACover, Theme, draw, motifs
from opennotebook.db.models import Collection, MindMap, Session, StudyNotes
from opennotebook.db.session import engine, sessionmaker
from opennotebook.domain import collections, covers, naming, refresh
from opennotebook.domain import settings as st
from tests.conftest import other_person
from tests.fake_ai import FakeModel

CID = "s1750000000000"

# ── the model's answer (cover/mod.rs) ─────────────────────────────────────────


def test_a_good_reply_is_taken_as_it_is() -> None:
    s = cover.validate(
        CID,
        '{"topic":"Cell Biology","terms":["Mitosis","Organelles","DNA Replication"],'
        '"motif":"virus","palette":"green","layout":"map"}',
    )
    assert s.topic == "Cell Biology"
    assert s.terms == ["Mitosis", "Organelles", "DNA Replication"]
    assert (s.motif, s.palette, s.layout) == ("virus", "green", "map")


def test_a_fenced_reply_with_a_preamble_is_still_read() -> None:
    raw = (
        "Here is the cover:\n```json\n"
        '{"topic": "\\"Rust Async.\\"", "terms": ["Futures", "futures", 7, "Tokio"], '
        '"motif": "CPU", "palette": "Graphite", "layout": "editorial"}\n```'
    )
    s = cover.validate(CID, raw)
    assert s.topic == "Rust Async"
    assert s.terms == ["Futures", "Tokio"], "duplicates and non-strings dropped"
    assert (s.motif, s.palette) == ("cpu", "slate")
    old = cover.validate(CID, '{"topic":"Rome","motif":"bank","palette":"sand","layout":"emblem"}')
    # The first palettes and layouts are read as their nearest.
    assert (old.palette, old.layout) == ("amber", "card")


@pytest.mark.parametrize(
    "raw",
    ["I cannot do that", '{"topic": ', '{"terms": ["a"]}', '{"topic": "  "}', '["topic"]'],
)
def test_bad_json_or_no_topic_is_refused(raw: str) -> None:
    with pytest.raises(NotACover):
        cover.validate(CID, raw)


def test_unknown_choices_fall_back_one_by_one() -> None:
    s = cover.validate(
        CID,
        '{"topic":"Volcanoes","terms":["Magma"],"motif":"volcano","palette":"neon",'
        '"layout":"collage"}',
    )
    fb = cover.fallback(CID, "", [])
    assert s.motif == motifs.FALLBACK
    assert s.palette == fb.palette
    assert s.layout == fb.layout
    assert s.topic == "Volcanoes", "the words are kept"


def test_long_topics_and_terms_are_clipped() -> None:
    raw = json.dumps(
        {
            "topic": "Very " * 40,
            "terms": ["Supercalifragilistic" * 4, "a b c d e f", "x", "y", "z", "w", "v"],
        }
    )
    s = cover.validate(CID, raw)
    assert len(s.topic) <= cover.TOPIC_MAX_CHARS, s.topic
    assert len(s.topic.split()) <= cover.TOPIC_MAX_WORDS
    assert len(s.terms) == cover.TERMS_MAX
    assert all(len(t) <= cover.TERM_MAX_CHARS for t in s.terms), s.terms
    assert s.terms[1] == "a b c d"


def test_the_fallback_is_the_cids_own_and_never_untitled_blank() -> None:
    a = cover.fallback("s1", "", [])
    assert a.topic == "Untitled collection"
    assert a == cover.fallback("s1", "", []), "deterministic"
    titles = ["Intro to Kernels", "Scheduling"]
    b = cover.fallback("s1", "Linux Internals", titles)
    assert b.topic == "Linux Internals"
    assert b.terms == titles
    assert (a.palette, a.layout) == (b.palette, b.layout)
    # Across many cids every accent and layout comes up.
    pals: set[str] = set()
    lays: set[str] = set()
    for i in range(400):
        f = cover.fallback(f"s17{i:011}", "", [])
        pals.add(f.palette)
        lays.add(f.layout)
    assert len(pals) == len(draw.ACCENTS)
    assert len(lays) == len(draw.LAYOUTS)


def test_versions_change_with_what_is_drawn_and_only_then() -> None:
    s = cover.fallback(CID, "Topic", [])
    t = CoverSpec(s.topic, list(s.terms), s.motif, s.palette, s.layout)
    assert cover.version(s) == cover.version(t)
    t = CoverSpec(s.topic, s.terms, s.motif, "teal" if s.palette == "rose" else "rose", s.layout)
    assert cover.version(s) != cover.version(t)
    assert cover.version(s).startswith("g-")
    names = ["a.md"]
    f = cover.fallback_version(CID, "Topic", names)
    assert f.startswith("f-")
    assert f == cover.fallback_version(CID, " Topic ", names)
    assert f != cover.fallback_version(CID, "Other", names)
    assert f != cover.fallback_version(CID, "Topic", [])


def test_versions_and_seeds_are_the_rust_servers() -> None:
    """Known answers printed by the Rust renderer, so a cover cached under a
    version means the same drawing on either side of the move."""
    cid = "0199a0b2-7c3e-7d4a-9f00-123456789abc"
    assert cover.seed(cid) == 10670302527987844670
    spec = CoverSpec("Cell Biology", ["Mitosis", "Organelles"], "virus", "green", "map")
    assert cover.version(spec) == "g-ef0bddfcb6361e2f"
    names = ["a.md", "b.md", "c.md", "d.md", "e.md", "f.md"]
    assert cover.fallback_version(cid, " Coral Reefs ", names) == "f-74fd18a8d78fb47d"
    assert cover.fallback_version(cid, "量子力学 Ünïcødé", []) == "f-8573f45e6567f1bd"


def assert_safe(html: str) -> None:
    assert html.startswith("<!doctype html>")
    lower = html.lower()
    assert "<script" not in lower, "no script"
    rest = lower.replace("http://www.w3.org/2000/svg", "")
    assert "http:" not in rest and "https:" not in rest and "//" not in rest, "nothing fetched"
    assert "url(http" not in lower and "@import" not in lower and "<img" not in lower
    assert "content-security-policy" in lower
    assert 'viewBox="0 0 1600 900"' in html
    assert 'preserveAspectRatio="xMidYMid slice"' in html


def attr(el: str, k: str) -> float | None:
    at = el.find(f' {k}="')
    if at == -1:
        return None
    at += len(k) + 3
    end = el.find('"', at)
    try:
        return float(el[at:end])
    except ValueError:
        return None


def assert_on_canvas(html: str) -> None:
    """Every `<text>`, every chip and every tile stays inside the margin a
    card's padding leaves, by the renderer's own measure, and no text leaves
    the box it was fitted to."""
    for t in html.split("<text ")[1:]:
        x, y, size = attr(t, "x"), attr(t, "y"), attr(t, "font-size")
        assert x is not None and y is not None and size is not None, t
        assert size * 0.7 < y < draw.H, f"y {y} off the canvas: {t}"
        assert 0.0 <= x <= draw.W, f"x {x}: {t}"
    for r in html.split("<rect ")[1:]:
        r = r[: r.find(">")]
        x, y, w, h = attr(r, "x"), attr(r, "y"), attr(r, "width"), attr(r, "height")
        if x is None or y is None or w is None or h is None:
            continue
        assert x >= 40 and y >= 40 and x + w <= draw.W - 40 and y + h <= draw.H - 40, (
            f"a shape past the margin: {r}"
        )


def assert_topic_legible(html: str) -> None:
    """The topic's size: at least 80, a 9 px cap height on a 256 px card."""
    sizes = [attr(f" {t}", "font-size") for t in html.split('<text class="t" ')[1:]]
    assert sizes, "a topic is drawn"
    for s in sizes:
        assert s is not None and s * 0.72 * 256.0 / draw.W >= 9.0, f"topic at {s}"


def test_every_layout_and_accent_draws_safely_with_hostile_text() -> None:
    hostile = [
        "<script>alert(1)</script>",
        'Ünïcødé & "quotes"',
        "Photosynthesis",
        "量子力学の基礎",
        "x" * 32,
    ]
    long = "The Extraordinarily Long History of Pneumonoultramicroscopic Things"
    cases = [(long, hostile), ("Cells", hostile[2:3]), ("Cells", []), ("W" * 60, hostile[:2])]
    for i, a in enumerate(draw.ACCENTS):
        for lay, _ in draw.LAYOUTS:
            for topic, terms in cases:
                for theme in (Theme.DARK, Theme.LIGHT):
                    spec = CoverSpec(topic, terms, "globe-americas", a.id, lay)
                    html = cover.render(f"s17{i:011}", spec, theme)
                    assert_safe(html)
                    assert_on_canvas(html)
                    assert_topic_legible(html)
                    assert "<script>alert" not in html, "escaped"
                    assert a.fill in html, f"{lay}/{a.id}"


def test_a_stored_spec_with_unknown_choices_still_draws() -> None:
    html = cover.render(CID, CoverSpec("T", [], "gone", "gone", "gone"), Theme.DARK)
    assert_safe(html)
    assert "<path" in html, "the fallback glyph is drawn"


def test_the_prompt_names_every_choice() -> None:
    p = cover.system_prompt("")
    for m, _ in motifs.MOTIFS:
        assert m in p, m
    for a in draw.ACCENTS:
        assert f"- {a.id}:" in p
    for lay, _ in draw.LAYOUTS:
        assert f"- {lay}:" in p


# ── drawing (cover/render.rs) ─────────────────────────────────────────────────


def lum(hex: str) -> float:
    h = hex.removeprefix("#")
    assert len(h) == 6, f"not a #rrggbb colour: {hex}"
    v = int(h, 16)

    def ch(shift: int) -> float:
        c = ((v >> shift) & 0xFF) / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(16) + 0.7152 * ch(8) + 0.0722 * ch(0)


def ratio(a: str, b: str) -> float:
    x, y = lum(a), lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def tokens(head: str) -> dict[str, str]:
    """The `--name: value` pairs of the theme block that starts at `head`."""
    css = draw.theme_css()
    start = css.index(head) + len(head)
    body = css[start : css.index("}", start)]
    out: dict[str, str] = {}
    for decl in body.split(";"):
        decl = decl.strip().rsplit("*/", 1)[-1].strip()
        k, sep, v = decl.partition(":")
        if sep and k.strip().startswith("--"):
            out[k.strip()] = v.strip()
    return out


def themes() -> list[tuple[str, dict[str, str]]]:
    return [("dark", tokens(":root {")), ("light", tokens(':root[data-bs-theme="light"] {'))]


def test_every_accent_holds_its_motif_and_stands_on_every_surface_in_both_themes() -> None:
    assert 6 <= len(draw.ACCENTS) <= 8
    assert draw.ACCENTS[0].fill == "#2563eb", "the first is the studio's primary"
    fails: list[str] = []
    for a in draw.ACCENTS:
        r = ratio(a.on_fill, a.fill)
        if r < 4.5:
            fails.append(f"{a.id}: motif on fill {r:.2f}:1")
        # The tile is a graphic: 3:1 against what it sits on.
        for theme, t in themes():
            for surface in ("--bs-body-bg", "--bs-secondary-bg"):
                r = ratio(a.fill, t[surface])
                if r < 3.0:
                    fails.append(f"{a.id}: fill on {theme} {surface} {r:.2f}:1")
    assert not fails, fails
    assert len({a.id for a in draw.ACCENTS}) == len(draw.ACCENTS)


def test_every_word_on_a_cover_reads_at_aa_in_both_themes() -> None:
    # (text, surface) for the topic, the label and the chips, wherever a
    # layout puts them.
    pairs = [
        ("--bs-emphasis-color", "--bs-body-bg"),
        ("--bs-emphasis-color", "--bs-secondary-bg"),
        ("--bs-secondary-color", "--bs-body-bg"),
        ("--bs-secondary-color", "--bs-secondary-bg"),
        ("--bs-body-color", "--bs-secondary-bg"),
        ("--bs-body-color", "--bs-body-bg"),
    ]
    fails = [
        f"{theme}: {fg} on {bg}"
        for theme, t in themes()
        for fg, bg in pairs
        if ratio(t[fg], t[bg]) < 4.5
    ]
    assert not fails, fails


def test_the_first_palettes_and_layouts_still_name_one() -> None:
    for old, new in draw.OLD_PALETTES:
        a = draw.accent(old)
        assert a is not None and a.id == new
    for old, new in draw.OLD_LAYOUTS:
        assert draw.layout(old) == new
    assert draw.accent("neon") is None
    assert draw.layout("collage") is None


def test_the_theme_is_the_apps_and_dark_unless_chosen() -> None:
    assert Theme.parse("light") == Theme.LIGHT
    assert Theme.parse(" Light ") == Theme.LIGHT
    for s in ("", "dark", "auto", "x"):
        assert Theme.parse(s) == Theme.DARK

    def page(theme: Theme) -> str:
        return draw.page(
            draw.Cover("Cells", ("Mitosis",), "virus", draw.ACCENTS[0], "card", theme, 7)
        )

    dark, light = page(Theme.DARK), page(Theme.LIGHT)
    assert dark.startswith("<!doctype html><html><head>")
    assert light.startswith('<!doctype html><html data-bs-theme="light"><head>')
    assert draw.theme_css() in dark, "the app's own tokens"
    assert dark.replace("<html>", '<html data-bs-theme="light">', 1) == light, (
        "the theme switch is the only difference"
    )


def test_a_word_too_long_for_a_line_is_broken_not_overflowed() -> None:
    lines = draw.wrap(
        "Pneumonoultramicroscopicsilicovolcanoconiosis", 100.0, 500.0, draw.Face.TITLE
    )
    assert len(lines) > 1
    for line in lines:
        assert draw.width(line, 100.0, draw.Face.TITLE) <= 500.0, line


def test_fitting_shrinks_then_clips_with_an_ellipsis() -> None:
    short = draw.fit("Cells", draw.Face.TITLE, 1000.0, 300.0, 2, 100.0, 50.0)
    assert (short.size, len(short.lines)) == (100.0, 1)
    f = draw.fit("word " * 200, draw.Face.TITLE, 600.0, 400.0, 3, 120.0, 60.0)
    assert f.size == 60.0
    assert len(f.lines) <= 3
    assert f.lines[-1].endswith("…"), f.lines
    for line in f.lines:
        assert draw.width(line, f.size, draw.Face.TITLE) <= 600.0, line
    assert len(f.lines) * f.size * 1.12 <= 400.0


# ── motifs (cover/motifs.rs) ──────────────────────────────────────────────────


def test_a_near_miss_motif_resolves_to_a_listed_one() -> None:
    assert motifs.resolve("bi-tree-fill") == "tree"
    assert motifs.resolve("Leaf") == "tree"
    assert motifs.resolve("rockets") == "rocket"
    assert motifs.resolve("Machine Learning") == "cpu"
    assert motifs.resolve("photosynthesis") == "tree"
    assert motifs.resolve("volcano") is None


def test_every_alias_points_at_a_vendored_motif() -> None:
    for words, m in motifs.ALIASES:
        assert motifs.motif(m) is not None, f"{words} -> {m} is not vendored"


def test_motifs_are_sorted_drawable_and_include_the_fallback() -> None:
    names = motifs.names()
    assert all(a < b for a, b in itertools.pairwise(names))
    assert motifs.motif(motifs.FALLBACK) is not None
    assert motifs.motif("not-a-motif") is None
    for n, inner in motifs.MOTIFS:
        assert motifs.motif(n) == inner
        assert inner.startswith("<path") and ' d="' in inner, n
        assert "<script" not in inner and "href" not in inner, n
    assert 50 <= len(motifs.MOTIFS) <= 70, len(motifs.MOTIFS)


# ── when a cover is designed (collection.rs) ──────────────────────────────────

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def made(id: str, kind: str, title: str, parts: list[str] | None = None) -> covers.Made:
    return covers.Made(id, kind, title, T0, parts or [])


def held() -> covers.Held:
    return covers.Held(
        names=["0.md", "1.md"],
        titles=["Intro", "More"],
        made=[
            made("d1", "Narrated slides", "Kernels"),
            made("m1", "Mind map", "Map", ["Scheduling", "Memory"]),
        ],
    )


def summary(sources: int = 2, **counts: int) -> collections.Summary:
    c = Collection(id=uuid.uuid4(), title="", cover=None, cover_from="")
    return collections.Summary(
        c,
        sources,
        counts.get("decks", 0),
        counts.get("audios", 0),
        counts.get("maps", 0),
        counts.get("notes", 0),
        counts.get("preparing", 0),
        counts.get("failed", 0),
    )


def test_the_cover_key_follows_sources_and_ready_outputs_only() -> None:
    key = covers.content_key(held())
    assert key == covers.content_key(held()), "stable"
    more = held()
    more.names.append("z.md")
    assert covers.content_key(more) != key, "a source added is new content"
    renamed = held()
    renamed.made[1].title = "Other"
    assert covers.content_key(renamed) != key, "a map renamed is"
    notes = held()
    notes.made.append(made("n1", "Study notes", "Notes"))
    assert covers.content_key(notes) != key, "a set of notes added is"
    # The order outputs are read in is not content.
    swapped = held()
    swapped.made.reverse()
    assert covers.content_key(swapped) == key


def test_covers_off_never_asks_the_model() -> None:
    for force in (False, True):
        for holds in (False, True):
            assert not covers.wants_design(False, force, holds, "", "k")
            assert not covers.wants_design(False, force, holds, "k", "k")
    assert covers.wants_design(True, False, True, "", "k"), "never designed"
    assert not covers.wants_design(True, False, True, "k", "k"), "unchanged"
    assert covers.wants_design(True, True, True, "k", "k"), "asked for"
    assert not covers.wants_design(True, True, False, "", "k"), "nothing to read"

    # And a list queues nothing with covers off.
    x = summary()
    h = {x.collection.id: held()}
    assert covers.stale_covers(True, [x], h) == [x.collection.id]
    assert covers.stale_covers(False, [x], h) == []


def test_a_cover_designed_from_this_content_is_not_queued_again() -> None:
    x = summary()
    x.collection.cover_from = covers.content_key(held())
    assert covers.stale_covers(True, [x], {x.collection.id: held()}) == []
    # An empty collection has nothing to design from.
    assert covers.stale_covers(True, [summary(sources=0)], {}) == []
    # Nor does one whose only outputs are still being made or failed.
    assert not covers.holds(summary(sources=0, decks=1, audios=1, preparing=1, failed=1))
    assert covers.holds(summary(sources=0, decks=2, preparing=1))


def test_the_cover_version_is_the_designed_one_only_while_covers_are_on() -> None:
    cid = "s1770000000000"
    names = ["0.md", "1.md"]
    fallback = cover.current_version(cid, "", None, True, names)
    assert fallback.startswith("f-"), fallback

    spec = cover.fallback(cid, "Designed", [])
    designed = cover.current_version(cid, "", spec.to_json(), True, names)
    assert designed == cover.version(spec)
    c = Collection(id=uuid.uuid4(), title="", cover=spec.to_json())
    assert covers.cover_spec(c, True, []) == spec

    assert cover.current_version(cid, "", spec.to_json(), False, names) == fallback
    assert covers.cover_spec(c, False, []).motif == motifs.FALLBACK
    # The fallback follows the title the list shows.
    assert cover.current_version(cid, "Named Now", None, True, names) != fallback


def test_the_digest_reads_ready_outputs_titles_within_its_bound() -> None:
    d = naming.Digest(
        title="Linux",
        sources=[naming.Opening("Intro", "The kernel")],
        made=[naming.Part(m.kind, m.title, m.parts) for m in held().made],
    )
    p = d.cover_prompt()
    assert p.startswith("Collection title: Linux")
    assert "Source: Intro\nThe kernel" in p
    assert "Narrated slides: Kernels" in p
    assert "Mind map: Map — Scheduling; Memory" in p

    long = naming.Digest(
        made=[naming.Part("Study notes", f"Notes {i}", ["x" * 80] * 4) for i in range(200)]
    )
    assert len(long.made_text()) <= naming.MADE_CHARS
    assert d.sources_text() == "Source: Intro\nThe kernel"


async def test_what_a_cover_reads_is_the_ready_outputs_newest_first(client: AsyncClient) -> None:
    c = (await client.post("/api/collections", json={"title": "Kernels"})).json()
    cid = uuid.UUID(c["id"])
    async with sessionmaker()() as s, s.begin():
        owner = (await s.get_one(Collection, cid)).owner_id
        common: dict[str, Any] = {"owner_id": owner, "collection_id": cid}
        s.add_all(
            [
                Session(
                    **common,
                    kind="slides",
                    title="Old deck",
                    state="ready",
                    slides=[{"title": "Why"}, {"title": " "}],
                    created_at=T0,
                ),
                Session(
                    **common,
                    kind="audio",
                    title="New talk",
                    state="ready",
                    created_at=T0 + timedelta(days=1),
                ),
                Session(**common, kind="slides", title="Still going", state="preparing"),
                Session(**common, kind="slides", title="Broke", state="failed"),
                MindMap(**common, title="Map", root={"name": "r", "children": [{"name": "A"}]}),
                StudyNotes(**common, title="Notes", body={"ideas": [{"heading": "One"}]}),
            ]
        )
    async with sessionmaker()() as s:
        h = (await covers.gather(s, owner, [cid]))[cid]
    assert [(m.kind, m.title, m.parts) for m in h.made] == [
        ("Audio overview", "New talk", []),
        ("Narrated slides", "Old deck", ["Why"]),
        ("Mind map", "Map", ["A"]),
        ("Study notes", "Notes", ["One"]),
    ]


# ── the routes ────────────────────────────────────────────────────────────────

NOTE = {"kind": "text", "text": "Coral reefs are built by colonies of tiny polyps over centuries."}
DESIGNED = json.dumps(
    {
        "topic": "Coral Reefs",
        "terms": ["Polyps", "Bleaching"],
        "motif": "water",
        "palette": "teal",
        "layout": "card",
    }
)


async def _with_a_source(client: AsyncClient, title: str = "Reefs") -> dict[str, Any]:
    c = (await client.post("/api/collections", json={"title": title})).json()
    assert (await client.post(f"/api/collections/{c['id']}/sources", json=NOTE)).status_code == 201
    await refresh.settle()
    return next(x for x in (await client.get("/api/collections")).json() if x["id"] == c["id"])


async def _usage(kind: str) -> int:
    async with engine().begin() as c:
        return (
            await c.execute(text("SELECT count(*) FROM usage_events WHERE kind = :k"), {"k": kind})
        ).scalar_one()


async def test_a_cover_is_served_with_its_theme_and_cache(client: AsyncClient) -> None:
    c = await _with_a_source(client)
    # The model was down, so the cover is still the one drawn from the title.
    v = c["cover_version"]
    assert v.startswith("f-"), v
    url = f"/api/collections/{c['id']}/cover"

    r = await client.get(url, params={"v": v, "theme": "light"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert r.text.startswith('<!doctype html><html data-bs-theme="light">')
    assert "Reefs" in r.text
    assert_safe(r.text)

    r = await client.get(url, params={"v": "f-old", "theme": "dark"})
    assert r.headers["cache-control"] == "no-cache", "an old version is never cached for good"
    assert r.text.startswith("<!doctype html><html><head>")
    r = await client.get(url, params={"v": v, "theme": "sepia"})
    assert r.text.startswith("<!doctype html><html><head>"), "anything but light is dark"

    # A new title is a new fallback, and so a new URL.
    await client.patch(f"/api/collections/{c['id']}", json={"title": "Atolls"})
    listed = (await client.get("/api/collections")).json()[0]
    assert listed["cover_version"] != v and listed["cover_version"].startswith("f-")


async def test_one_person_cannot_see_or_redraw_anothers_cover(client: AsyncClient) -> None:
    c = (await client.post("/api/collections", json={"title": "Mine"})).json()
    them = await other_person(client, "them@example.com")
    for method in ("GET", "POST"):
        r = await client.request(method, f"/api/collections/{c['id']}/cover", headers=them)
        assert r.status_code == 404, (method, r.text)
        assert r.json() == {
            "detail": "That collection is no longer there. Reload the page to see what is."
        }


async def test_a_redraw_designs_the_cover_and_answers_once_drawn(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = await _with_a_source(client)
    model = FakeModel(DESIGNED)
    monkeypatch.setattr(covers, "ai", model)
    r = await client.post(f"/api/collections/{c['id']}/cover")
    assert r.status_code == 200, r.text
    spec = CoverSpec("Coral Reefs", ["Polyps", "Bleaching"], "water", "teal", "card")
    assert r.json()["cover_version"] == cover.version(spec)
    assert len(model.asked) == 1 and model.asked[0]["max_tokens"] == 300
    prompt = model.asked[0]["messages"][1]["content"]
    assert prompt.startswith("Collection title: Reefs") and "Coral reefs are built" in prompt
    assert await _usage("cover") == 1, "the call is on the ledger"

    page = await client.get(
        f"/api/collections/{c['id']}/cover", params={"v": r.json()["cover_version"]}
    )
    assert "Coral Reefs" in page.text and "Polyps" in page.text and "#0f7a72" in page.text
    assert (await client.get("/api/collections")).json()[0]["cover_version"] == cover.version(spec)

    # Turned off, every cover is the drawn one again, and nothing is asked.
    r = await client.patch(f"/api/settings/{st.COVERS_KEY}", json={"value": "off"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/collections/{c['id']}/cover")
    assert r.status_code == 200
    assert r.json()["cover_version"] == c["cover_version"]
    assert len(model.asked) == 1, "covers off asks no model"


async def test_a_redraw_with_covers_off_asks_no_model(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await client.patch(f"/api/settings/{st.COVERS_KEY}", json={"value": "off"})
    c = await _with_a_source(client)
    model = FakeModel(DESIGNED)
    monkeypatch.setattr(covers, "ai", model)
    r = await client.post(f"/api/collections/{c['id']}/cover")
    assert r.status_code == 200, r.text
    assert r.json()["cover_version"] == c["cover_version"]
    assert model.asked == []
    assert await _usage("cover") == 0


async def test_a_failed_redraw_keeps_the_cover_and_says_why(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = await _with_a_source(client)
    monkeypatch.setattr(covers, "ai", FakeModel(503))
    r = await client.post(f"/api/collections/{c['id']}/cover")
    assert r.status_code == 503
    assert (
        r.json()["detail"] == "The AI provider is not answering right now. Try again in a minute."
    )
    monkeypatch.setattr(covers, "ai", FakeModel(402))
    r = await client.post(f"/api/collections/{c['id']}/cover")
    assert r.status_code == 502
    assert r.json()["detail"].startswith("The AI account is out of credit")
    monkeypatch.setattr(covers, "ai", FakeModel("I would rather not."))
    r = await client.post(f"/api/collections/{c['id']}/cover")
    assert r.status_code == 502
    assert r.json()["detail"].startswith("The AI model's answer could not be drawn as a cover")
    listed = (await client.get("/api/collections")).json()[0]
    assert listed["cover_version"] == c["cover_version"], "the cover is kept"


async def test_a_source_added_designs_the_cover_in_the_background(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeModel(DESIGNED)
    monkeypatch.setattr(covers, "ai", model)
    c = await _with_a_source(client)
    assert c["cover_version"].startswith("g-")
    assert len(model.asked) == 1
    # Listed again with nothing new, it is not designed again.
    await client.get("/api/collections")
    await client.get(f"/api/collections/{c['id']}")
    await refresh.settle()
    assert len(model.asked) == 1


async def test_a_listed_collection_with_an_out_of_date_cover_is_redesigned(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = await _with_a_source(client)
    # As an output finishing while nothing listened: new content, no trigger.
    async with sessionmaker()() as s, s.begin():
        owner = (await s.get_one(Collection, uuid.UUID(c["id"]))).owner_id
        s.add(MindMap(owner_id=owner, collection_id=uuid.UUID(c["id"]), title="Map", root={}))
    model = FakeModel(DESIGNED)
    monkeypatch.setattr(covers, "ai", model)
    await client.get("/api/collections")
    await refresh.settle()
    assert len(model.asked) == 1
    assert (await client.get("/api/collections")).json()[0]["cover_version"].startswith("g-")
