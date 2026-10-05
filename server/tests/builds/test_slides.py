# ruff: noqa: E501
"""The deck: kits, the cleaner, the slide writer and the check. Ported from
`opennotebook_build/src/{kits,clean,slides,validate}.rs`. Plan §9: kit
palettes pass WCAG AA; `clean` strips colour, gradients, background shapes,
emoji and scripts; a missing slide is drawn plain from its copy."""

from pathlib import Path

import pytest

from opennotebook import storage
from opennotebook.build import kits, slides, validate
from opennotebook.build.clean import clean
from opennotebook.build.errors import EmptyAudio, SlideMissing
from opennotebook.domain import styles
from opennotebook.domain.sessions import Line, Part
from tests.builds.fake import install

FIXTURE = Path(__file__).parent / "fixtures" / "dark_backdrop_slide.html"


def lum(hex_: str) -> float:
    def c(i: int) -> float:
        v = int(hex_[i : i + 2], 16) / 255
        return v / 12.92 if v <= 0.039_28 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * c(1) + 0.7152 * c(3) + 0.0722 * c(5)


def contrast(a: str, b: str) -> float:
    x, y = lum(a), lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def test_every_style_has_a_kit() -> None:
    for s in styles.STYLES:
        assert kits.kit(s.id) is not None, f"style `{s.id}` has no kit"


@pytest.mark.parametrize("kit", kits.KITS, ids=lambda k: k.id)
def test_every_kit_is_readable(kit: kits.Kit) -> None:
    # Text, secondary text and the accent (kickers are small accent text) at
    # 4.5:1 on the ground and on a panel: WCAG AA.
    bg, text, muted, accent, panel, _ = kit.palette
    for what, fg in (("text", text), ("secondary", muted), ("accent", accent)):
        for on, ground in (("bg", bg), ("panel", panel)):
            r = contrast(fg, ground)
            assert r >= 4.5, f"{kit.id}: {what} on {on} is {r:.2f}:1"


def test_the_kit_goes_last_in_head_and_the_filters_first_in_body() -> None:
    k = kits.kit("clay")
    assert k is not None
    html = '<!doctype html><html><head><style>h1{margin:0}</style></head><body class="x"><h1>t</h1></body></html>'
    out = kits.apply(html, k)
    assert out.index("h1{margin:0}") < out.index("data-kit")
    assert '<body class="x"><svg width="0"' in out
    assert kits.apply(out, k) == out, "applying twice changes nothing"


def test_a_real_dark_backdrop_slide_comes_out_on_the_kit() -> None:
    # A slide a model really wrote: a near-black gradient rect over the whole
    # slide, a figure faded to 15% and pinned over everything, the text pinned
    # into a 600px strip, and a stylesheet link of its own.
    raw = FIXTURE.read_text()
    k = kits.kit("editorial")
    assert k is not None
    out = kits.apply(raw, k)
    model_part = out[out.index("</defs></svg>") :]
    for gone in (
        "Gradient",
        "#1a1a1a",
        "#0d0d0d",
        "<rect",
        "position",
        "opacity",
        "k-style.css",
        "bottom:",
    ):
        assert gone not in model_part, f"`{gone}` survived:\n{model_part}"
    head = out[: out.index("</head>")]
    assert "k-style.css" not in head and "position: absolute" not in head
    assert '<h1 class="k-title">Handling Arbitrary Conversation Dynamics</h1>' in out
    assert "🎤" not in out, "an emoji survived"
    assert slides.usable(raw)


def test_every_kit_draws_its_sample() -> None:
    for k in kits.KITS:
        out = kits.sample(k)
        assert "data-kit" in out and "k-title" in out


def test_a_painted_background_rect_and_its_gradient_go() -> None:
    html = (
        '<figure class="k-figure" style="position:absolute;top:0;width:100%"><svg viewBox="0 0 '
        '1920 1080"><defs><linearGradient id="g"><stop offset="0" style="stop-color:#1a1a1a"/>'
        '</linearGradient></defs><rect width="1920" height="1080" fill="url(#g)"/><circle '
        'class="k-ink" cx="9" cy="9" r="4" stroke-width="2"/></svg></figure>'
    )
    out = clean(html)
    assert "linearGradient" not in out
    assert "<rect" not in out
    assert "position" not in out
    assert 'style="width:100%"' in out
    assert '<circle class="k-ink" cx="9" cy="9" r="4" stroke-width="2"/>' in out


def test_colours_go_and_fill_none_stays() -> None:
    out = clean(
        '<svg viewBox="0 0 800 600"><path class="k-ink" d="M0 0" fill="none" stroke="#000"/>'
        '<text fill="#111" x="4">hi</text><rect class="k-f1" x="1" y="1" width="80" height="60" '
        'fill="black"/></svg>'
    )
    assert out == (
        '<svg viewBox="0 0 800 600"><path class="k-ink" d="M0 0" fill="none"/><text x="4">hi'
        '</text><rect class="k-f1" x="1" y="1" width="80" height="60"/></svg>'
    )


def test_a_slide_stylesheet_keeps_only_layout() -> None:
    out = clean(
        "<style>body{background:#000;color:#fff} .a{display:grid;gap:20px;color:red} "
        ".b{font-size:9px} @media (x){.c{width:50%;fill:red}}</style><p>x</p>"
    )
    assert out == "<style>.a{display:grid;gap:20px}@media (x){.c{width:50%}}</style><p>x</p>"


def test_the_kits_own_style_and_plain_text_pass_through() -> None:
    html = (
        "<!doctype html><html><head><style data-kit>body{color:red}</style></head><body><!-- a "
        '> b --><p class="k-sub">5 > 3 & "so"</p></body></html>'
    )
    assert clean(html) == html


def test_scripts_and_emoji_go() -> None:
    out = clean('<p>Hi 🎉 there</p><script>alert(1)</script><link rel="stylesheet" href="x.css">')
    assert out == "<p>Hi  there</p>"


def test_documents_are_cut_out_of_a_reply_in_order() -> None:
    reply = (
        "<!-- SLIDE 1 -->\n<!doctype html><html><body>a</body></html>\nnote\n"
        "<!-- SLIDE 2 -->\n<!DOCTYPE html><html><body>b</body></html>\n"
        "<!-- SLIDE 3 -->\n<!doctype html><html><body>cut"
    )
    docs = slides.complete_docs(reply)
    assert len(docs) == 2, "the unfinished third is not a slide"
    assert ">a<" in docs[0] and ">b<" in docs[1]


def part(n: int, *on_slide: str) -> Part:
    return Part(
        f"slide_{n}",
        n,
        f"Title {n}",
        list(on_slide),
        [Line(f"s{n}l0", "host", 0, "Spoken words for this slide.")],
    )


def plan() -> slides.DeckPlan:
    k = kits.kit("editorial")
    assert k is not None
    return slides.DeckPlan(
        "Reefs", k, "Editorial", "brief", "anthropic/claude-haiku-4.5", "", "sid1"
    )


def test_a_missing_slide_is_drawn_plain_from_its_copy() -> None:
    html = slides.plain_slide(
        plan(), part(0, "Point: polyps <build>", "Stat: 30 — years", "Subhead: slow")
    )
    assert '<h1 class="k-title">Title 0</h1>' in html
    assert "<li>polyps &lt;build&gt;</li>" in html
    assert '<div class="k-num">30</div><div class="k-cap">years</div>' in html
    assert '<p class="k-sub">slow</p>' in html
    assert slides.usable(html)


async def test_a_deck_is_written_four_slides_a_call_with_the_kit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    studio, _ = install(monkeypatch, tmp_path)
    parts = [part(i, "Point: p") for i in range(5)]
    out = await slides.write_deck(plan(), parts)
    assert out.written == [f"slide_{i}" for i in range(5)]
    assert out.fallbacks == 0
    assert studio.asked.count("slides") == 2, "a batch of four and a batch of one"
    for i in range(5):
        html = storage.read(f"decks/sid1/studio/slide_{i}.html").decode()
        assert "data-kit" in html and f"Title {i}" in html
    validate.slides_are_written("sid1", parts)


async def test_a_batch_that_never_comes_back_is_drawn_plain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    studio, _ = install(monkeypatch, tmp_path)
    studio.slide_docs = False
    parts = [part(i, "Point: kept") for i in range(2)]
    out = await slides.write_deck(plan(), parts)
    # Asked once more, then each slide drawn plain: a session never fails
    # over a figure.
    assert studio.asked.count("slides") == 2
    assert out.fallbacks == 2
    assert "<li>kept</li>" in storage.read("decks/sid1/studio/slide_1.html").decode()


def test_a_slide_missing_from_the_volume_fails_the_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(storage, "_root", lambda: tmp_path)
    storage.put("decks/s/studio/slide_0.html", b"<!doctype html><html><body>cut")
    with pytest.raises(SlideMissing) as e:
        validate.slides_are_written("s", [part(0)])
    assert e.value.sentence.endswith(".")


def test_a_line_with_audio_and_a_real_duration_passes() -> None:
    p = part(0)
    p.lines[0].audio_path, p.lines[0].duration_ms = "audio/s/s0l0.wav", 1200
    validate.narration_is_playable([p])


def test_a_missing_path_or_a_zero_duration_fails() -> None:
    # Both halves of the check fail on their own: a path with no duration and
    # a duration of zero are different bugs.
    p = part(0)
    p.lines[0].duration_ms = 1200
    with pytest.raises(EmptyAudio):
        validate.narration_is_playable([p])
    p.lines[0].audio_path, p.lines[0].duration_ms = "audio/s/s0l0.wav", 0
    with pytest.raises(EmptyAudio):
        validate.narration_is_playable([p])
