"""The settings catalogue (ported from `session/src/settings.rs` and the
server's `settings_impl.rs` / `settings_api.rs` tests) and its three routes."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook.api.settings import Setting
from opennotebook.config import settings as config
from opennotebook.db.session import engine, sessionmaker
from opennotebook.domain import settings as st
from opennotebook.domain.styles import DEFAULT_STYLE, STYLES, style
from tests.conftest import other_person


@pytest.fixture(autouse=True)
def no_operator_overrides(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An environment variable of a setting's name wins over everything, so
    one left set on the machine running the tests would decide them."""
    for d in st.CATALOGUE:
        monkeypatch.delenv(d.key, raising=False)
    yield


def described(c: st.Current) -> Setting:
    return Setting.of(c)


# ── the catalogue ─────────────────────────────────────────────────────────────


def test_the_defaults_are_the_benchmarked_models() -> None:
    # A change to either is a change to what every output costs and how it
    # sounds, so it should show up in a diff rather than in a bill.
    assert st.SCRIPT_MODEL_DEFAULT == "anthropic/claude-haiku-4.5"
    assert st.ANSWER_MODEL_DEFAULT == "openai/gpt-audio-mini"


async def test_every_default_is_valid_for_its_own_setting() -> None:
    # A default the page cannot select would show a blank control.
    for d in st.CATALOGUE:
        assert st.validate(d, d.default) is None, d.key
        assert await st.check(d, d.default) is None, d.key
        assert d.tab in st.TABS, f"{d.key} is on unknown tab {d.tab}"


def test_keys_are_unique_and_namespaced() -> None:
    seen: set[str] = set()
    for d in st.CATALOGUE:
        assert d.key.startswith("OPENNOTEBOOK_"), d.key
        assert d.key not in seen, f"{d.key} twice"
        seen.add(d.key)


def test_there_are_29_settings_and_no_secret_among_them() -> None:
    assert len(st.CATALOGUE) == 29
    # The AI key comes from the environment only, never from a settings row.
    for d in st.CATALOGUE:
        assert "KEY" not in d.key and "SECRET" not in d.key, d.key
    assert st.find("OPENNOTEBOOK_AI_KEY") is None


def test_the_models_are_the_instances_and_the_rest_each_persons() -> None:
    for d in st.CATALOGUE:
        assert d.scope == ("instance" if d.kind.name == "model" else "user"), d.key
    assert st.find(st.MAX_BUILD_USD_KEY).scope == "user"  # pyright: ignore[reportOptionalMemberAccess]


def test_out_of_range_and_unknown_choices_are_refused() -> None:
    slides = st.need(st.SLIDE_COUNT_KEY)
    assert st.validate(slides, "2") is not None
    assert st.validate(slides, "13") is not None
    assert st.validate(slides, "abc") is not None
    assert st.validate(slides, "8_0") is not None
    assert st.validate(slides, "8") is None
    assert st.validate(st.need(st.SPEAKER1_VOICE_KEY), "zz_nobody") is not None
    assert st.validate(st.need(st.INTERRUPT_KEY), "maybe") is not None
    assert st.validate(st.need(st.SPEAKER1_NAME_KEY), "x" * 401) is not None
    assert st.validate(st.need(st.SPEAKER1_NAME_KEY), "  ") is None


def test_the_tabs_are_in_the_plans_order_and_each_has_its_info() -> None:
    assert st.TABS == (
        "General",
        "Generation defaults",
        "Voices",
        "Live conversation",
        "Costs & limits",
        "Models",
    )
    assert tuple(t.label for t in st.TAB_INFO) == st.TABS
    for t in st.TABS:
        assert any(d.tab == t for d in st.CATALOGUE), f"{t} is empty"


def test_the_new_settings_exist_with_their_defaults() -> None:
    want = [
        (st.AUDIO_FORMAT_KEY, st.TAB_DEFAULTS, "deep_dive"),
        (st.AUDIO_LENGTH_KEY, st.TAB_DEFAULTS, "default"),
        (st.RESEARCH_DEPTH_KEY, st.TAB_DEFAULTS, "standard"),
        (st.AUTO_NAME_KEY, st.TAB_GENERAL, "on"),
        (st.COVERS_KEY, st.TAB_GENERAL, "on"),
        (st.SHOW_COST_KEY, st.TAB_COSTS, "on"),
        (st.SEARCH_MODEL_KEY, st.TAB_MODELS, st.SEARCH_MODEL_DEFAULT),
        (st.SLIDE_MODEL_KEY, st.TAB_MODELS, st.SLIDE_MODEL_DEFAULT),
    ]
    for key, tab, default in want:
        d = st.need(key)
        assert (d.tab, d.default) == (tab, default), key
    assert st.validate(st.need(st.AUDIO_FORMAT_KEY), "podcast") is not None
    assert st.validate(st.need(st.RESEARCH_DEPTH_KEY), "quick") is None
    assert st.validate(st.need(st.RESEARCH_DEPTH_KEY), "deep") is not None
    assert st.validate(st.need(st.SHOW_COST_KEY), "maybe") is not None


def test_every_research_depth_is_a_tier() -> None:
    # The Rust test also checked the audio formats and lengths against the
    # session model's parser, which is not ported yet.
    for v, _ in st.RESEARCH_DEPTHS:
        assert st.research_tier(v) == v
    assert st.research_tier("nonsense") == "standard"


def test_models_are_advanced_grouped_and_offer_their_default() -> None:
    for d in st.CATALOGUE:
        is_model = d.kind.name == "model"
        assert is_model == (d.tab == st.TAB_MODELS), d.key
        assert d.advanced == is_model, d.key
        if is_model:
            assert d.group, f"{d.key} has no group"
            assert any(v == d.default for v, _ in d.kind.options), d.key
    m = st.need(st.SCRIPT_MODEL_KEY)
    assert st.validate(m, "some-vendor/any-new-model") is None
    assert st.validate(m, "not a model") is not None


def test_the_chat_model_has_one_fallback() -> None:
    d = st.need(st.CHAT_MODEL_KEY)
    assert d.default == st.AGENT_MODEL_DEFAULT
    assert "Gemini 2.5 Flash Lite" in d.help
    assert st.or_default("", st.AGENT_MODEL_DEFAULT) == st.AGENT_MODEL_DEFAULT
    assert st.or_default("  ", st.AGENT_MODEL_DEFAULT) == st.AGENT_MODEL_DEFAULT
    assert st.or_default("x/y", st.AGENT_MODEL_DEFAULT) == "x/y"


def test_the_slide_range_is_one_range() -> None:
    d = st.need(st.SLIDE_COUNT_KEY)
    assert (d.kind.name, d.kind.min, d.kind.max) == ("number", st.SLIDES_MIN, st.SLIDES_MAX)
    assert d.default == str(st.SLIDES_DEFAULT)
    assert st.clamp_slides(1) == st.SLIDES_MIN
    assert st.clamp_slides(30) == st.SLIDES_MAX
    assert st.clamp_slides(7) == 7


def test_every_language_has_a_recogniser_tag() -> None:
    for name, _ in st.LANGUAGE_CHOICES:
        assert st.language_tag(name) != ""
        assert any(n == name for n, _ in st.LANGUAGES), name
    assert st.language_tag("Klingon") == "en-US"
    assert st.language_rule("English") == ""
    assert "French" in st.language_rule("French")


def test_limits_minutes_and_toggles_read_as_rust_did() -> None:
    assert st.parse_limit("0.50") == 0.5
    assert st.parse_limit("off") is None
    assert st.parse_limit("0") is None
    assert st.session_minutes("40") == 20
    assert st.session_minutes("junk") == 5
    assert st.is_on("on") and st.is_on("") and not st.is_on("off")


# ── styles ────────────────────────────────────────────────────────────────────


def test_style_ids_are_unique_and_every_style_carries_a_brief() -> None:
    for i, s in enumerate(STYLES):
        assert s.brief, f"{s.id} has no brief"
        assert all(o.id != s.id for o in STYLES[:i]), f"duplicate id {s.id}"
        assert style(s.id) == s
    assert style("nothing-like-this") is None


def test_the_default_style_exists() -> None:
    assert st.need(st.STYLE_KEY).default == DEFAULT_STYLE.id


# ── checking and describing ───────────────────────────────────────────────────


async def test_a_tested_model_and_bad_values_are_judged_without_the_catalogue() -> None:
    d = st.need(st.SLIDE_MODEL_KEY)
    assert await st.check(d, "anthropic/claude-sonnet-5.5") is None
    assert await st.check(d, "has spaces in it") is not None
    # The seam: until the AI client lands, an untested id need only look
    # like vendor/name.
    assert await st.check(d, "vendor/new-model:free") is None
    assert await st.check(d, "no-slash") is not None
    assert await st.check(st.need(st.STYLE_KEY), "no-such-style") is not None
    assert await st.check(st.need(st.STYLE_KEY), "clay") is None


def test_a_model_setting_is_text_with_its_tested_options() -> None:
    d = st.need(st.SCRIPT_MODEL_KEY)
    s = described(st.Current(d, "", d.default))
    assert s.kind == "text" and s.model and s.advanced
    assert s.group == "Writing" and s.scope == "instance"
    assert any(o.value == st.SCRIPT_MODEL_DEFAULT for o in s.options)
    assert st.SCRIPT_MODEL_DEFAULT in s.suggestions
    # Prices wait for the AI client.
    assert s.price == "" and all(o.hint == "" for o in s.options)


def test_a_number_carries_its_unit_and_range() -> None:
    d = st.need(st.SLIDE_COUNT_KEY)
    s = described(st.Current(d, "", d.default))
    assert (s.min, s.max) == (3, 12)
    assert s.unit == "slides"
    assert not s.model and not s.advanced and s.price == ""


def test_a_choice_and_a_style_are_described_with_their_options() -> None:
    d = st.need(st.SPEAKER_COUNT_KEY)
    s = described(st.Current(d, "2", d.default))
    assert (s.kind, s.value, len(s.options), s.group, s.advanced) == ("choice", "2", 3, "", False)
    s = described(st.Current(st.need(st.STYLE_KEY), "", "editorial"))
    assert s.kind == "choice" and [o.value for o in s.options] == [x.id for x in STYLES]


def test_the_lookup_order_is_environment_person_instance_default() -> None:
    lang, model = st.LANGUAGE_KEY, st.SCRIPT_MODEL_KEY
    assert st.effective(lang, {}, {}, env={}) == "English"
    assert st.effective(lang, {}, {lang: "Dutch"}, env={}) == "Dutch"
    assert st.effective(lang, {lang: "French"}, {lang: "Dutch"}, env={}) == "French"
    assert st.effective(lang, {lang: "French"}, {}, env={lang: "German"}) == "German"
    # A person's row never decides an instance setting.
    assert st.effective(model, {model: "x/y"}, {}, env={}) == st.SCRIPT_MODEL_DEFAULT
    assert st.effective(model, {}, {model: "a/b"}, env={}) == "a/b"
    # Blank values fall through, as they did in the Rust file.
    assert st.effective(lang, {lang: " "}, {}, env={lang: ""}) == "English"


# ── the routes ────────────────────────────────────────────────────────────────


async def test_every_setting_is_listed_with_its_tabs(client: AsyncClient) -> None:
    r = await client.get("/api/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["label"] for t in body["tabs"]] == list(st.TABS)
    assert body["tabs"][-1] == {
        "id": "models",
        "label": "Models",
        "note": "Changing these affects quality and cost.",
        "advanced": True,
    }
    assert [x["key"] for x in body["settings"]] == [d.key for d in st.CATALOGUE]
    lang = next(x for x in body["settings"] if x["key"] == st.LANGUAGE_KEY)
    assert (lang["value"], lang["default"], lang["scope"]) == ("", "English", "user")
    # The page reads these names; a rename here is a blank page there.
    chat = next(x for x in body["settings"] if x["key"] == st.CHAT_MODEL_KEY)
    for k in [
        "key", "tab", "label", "help", "kind", "options", "suggestions", "default",
        "value", "group", "unit", "advanced", "model", "price", "scope",
    ]:  # fmt: skip
        assert k in chat, k
    assert chat["kind"] == "text" and chat["model"] and chat["suggestions"]


async def test_a_setting_is_set_and_reset(client: AsyncClient) -> None:
    r = await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={"value": " French "})
    assert r.status_code == 200, r.text
    assert (r.json()["value"], r.json()["default"]) == ("French", "English")
    async with sessionmaker()() as s:
        me = (await s.execute(text("SELECT id FROM users"))).scalar_one()
        assert await st.value(s, me, st.LANGUAGE_KEY) == "French"
        assert (await st.values(s, me))[st.LANGUAGE_KEY] == "French"

    # Setting it back to the default stores nothing, the same as never
    # changing it; so does an empty value.
    await client.patch(f"/api/settings/{st.SLIDE_COUNT_KEY}", json={"value": "8"})
    r = await client.patch(f"/api/settings/{st.SLIDE_COUNT_KEY}", json={"value": "5"})
    assert r.json()["value"] == ""
    r = await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={"value": ""})
    assert r.json()["value"] == ""
    async with engine().connect() as c:
        assert (await c.execute(text("SELECT count(*) FROM user_settings"))).scalar_one() == 0


async def test_a_bad_value_is_refused_in_a_sentence(client: AsyncClient) -> None:
    r = await client.patch(f"/api/settings/{st.SLIDE_COUNT_KEY}", json={"value": "40"})
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "Slides per deck must be a whole number from 3 to 12. Enter one in that range."
    )
    r = await client.patch(f"/api/settings/{st.SPEAKER1_VOICE_KEY}", json={"value": "zz"})
    assert (
        r.json()["detail"]
        == "“zz” is not one of the choices for Host voice. Pick one from the list."
    )
    r = await client.patch(f"/api/settings/{st.STYLE_KEY}", json={"value": "vector"})
    assert r.status_code == 422 and "not a slide style" in r.json()["detail"]
    r = await client.patch(f"/api/settings/{st.SCRIPT_MODEL_KEY}", json={"value": "gpt five"})
    assert r.status_code == 422 and r.json()["detail"].startswith("“gpt five” is not a model id.")
    r = await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={})
    assert r.status_code == 422 and r.json()["detail"] == "Fill in value, then try again."


async def test_an_unknown_setting_is_a_404_sentence(client: AsyncClient) -> None:
    r = await client.patch("/api/settings/OPENNOTEBOOK_AI_KEY", json={"value": "sk-x"})
    assert r.status_code == 404
    assert r.json()["detail"] == (
        "There is no setting called OPENNOTEBOOK_AI_KEY. "
        "Reload the page to see the settings there are."
    )


async def test_one_persons_choice_is_not_anothers(client: AsyncClient) -> None:
    them = await other_person(client, "them@example.com")
    await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={"value": "French"})
    r = await client.patch(
        f"/api/settings/{st.SPEAKER1_VOICE_KEY}", json={"value": "bm_lewis"}, headers=them
    )
    assert r.status_code == 200, r.text

    def of(body: dict[str, list[dict[str, str]]], key: str) -> str:
        return next(x["value"] for x in body["settings"] if x["key"] == key)

    mine = (await client.get("/api/settings")).json()
    theirs = (await client.get("/api/settings", headers=them)).json()
    assert of(mine, st.LANGUAGE_KEY) == "French" and of(theirs, st.LANGUAGE_KEY) == ""
    assert of(mine, st.SPEAKER1_VOICE_KEY) == "" and of(theirs, st.SPEAKER1_VOICE_KEY) == "bm_lewis"


async def test_the_owner_sets_a_model_for_everyone(client: AsyncClient) -> None:
    them = await other_person(client, "them@example.com")
    key = st.SLIDE_MODEL_KEY
    r = await client.patch(f"/api/settings/{key}", json={"value": "anthropic/claude-sonnet-5.5"})
    assert r.status_code == 200, r.text
    theirs = (await client.get("/api/settings", headers=them)).json()
    assert next(x for x in theirs["settings"] if x["key"] == key)["value"] == (
        "anthropic/claude-sonnet-5.5"
    )
    # Someone else signed in with a key may not change it, even in local mode.
    r = await client.patch(f"/api/settings/{key}", json={"value": ""}, headers=them)
    assert r.status_code == 403
    assert r.json()["detail"] == (
        "Slide model is set for everyone by whoever runs this studio, so it cannot be "
        "changed here. Ask them to change it."
    )
    # The owner resets it.
    r = await client.patch(f"/api/settings/{key}", json={"value": ""})
    assert r.json()["value"] == ""


async def test_in_keys_mode_nobody_changes_an_instance_setting(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    them = await other_person(client, "owner@example.com")
    monkeypatch.setattr(config(), "auth", "keys")
    r = await client.patch(
        f"/api/settings/{st.CHAT_MODEL_KEY}", json={"value": "openai/gpt-5.4"}, headers=them
    )
    assert r.status_code == 403
    # Their own settings are theirs to change.
    r = await client.patch(
        f"/api/settings/{st.LANGUAGE_KEY}", json={"value": "Hindi"}, headers=them
    )
    assert r.status_code == 200, r.text


async def test_an_instance_value_is_a_persons_default(client: AsyncClient) -> None:
    async with engine().begin() as c:
        await c.execute(
            text("INSERT INTO instance_settings (key, value) VALUES (:k, 'Spanish')"),
            {"k": st.LANGUAGE_KEY},
        )
    body = (await client.get("/api/settings")).json()
    lang = next(x for x in body["settings"] if x["key"] == st.LANGUAGE_KEY)
    assert (lang["value"], lang["default"]) == ("", "Spanish")
    # Choosing what applies anyway stores nothing.
    r = await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={"value": "Spanish"})
    assert r.json()["value"] == ""
    async with engine().connect() as c:
        assert (await c.execute(text("SELECT count(*) FROM user_settings"))).scalar_one() == 0


async def test_the_environment_overrides_what_is_stored(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await client.patch(f"/api/settings/{st.LANGUAGE_KEY}", json={"value": "French"})
    monkeypatch.setenv(st.LANGUAGE_KEY, "German")
    body = (await client.get("/api/settings")).json()
    assert next(x for x in body["settings"] if x["key"] == st.LANGUAGE_KEY)["value"] == "German"


def _shipped(path: str) -> bool:
    """Whether the web app has a file at `path` under its mount."""
    return (Path(__file__).resolve().parents[2] / "web" / "public" / path).is_file()


async def test_the_styles_are_listed_default_first(client: AsyncClient) -> None:
    r = await client.get("/api/styles")
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [s.id for s in STYLES]
    assert r.json()[0] == {
        "id": "editorial",
        "label": "Editorial",
        "blurb": "Magazine type, thin rules",
        "thumbnail": "assets/styles/editorial.jpg",
    }
    # Every picture named is one the web app ships.
    assert all(_shipped(x["thumbnail"]) for x in r.json())


# ── what the Create panel offers ─────────────────────────────────────────────


async def _options(client: AsyncClient, cid: str) -> dict[str, Any]:
    r = await client.get(f"/api/collections/{cid}/options")
    assert r.status_code == 200, r.text
    return r.json()


async def _set(client: AsyncClient, key: str, value: str) -> None:
    r = await client.patch(f"/api/settings/{key}", json={"value": value})
    assert r.status_code == 200, r.text


async def test_the_create_panel_offers_what_the_settings_say(client: AsyncClient) -> None:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    o = await _options(client, cid)
    assert [x["id"] for x in o["styles"]] == [s.id for s in STYLES]
    assert o["styles"][1]["thumbnail"] == "assets/styles/professional.jpg"
    assert (o["default_style"], o["default_audio_format"], o["default_audio_length"]) == (
        "editorial",
        "deep_dive",
        "default",
    )
    formats = {f["id"]: f for f in o["audio_formats"]}
    assert list(formats) == ["deep_dive", "brief", "critique", "debate"]
    assert [n["id"] for n in formats["deep_dive"]["lengths"]] == ["shorter", "default", "longer"]
    assert [n["label"] for n in formats["debate"]["lengths"]] == ["Shorter", "Default"]
    # Brief is always about two minutes: nothing to choose.
    assert formats["brief"]["lengths"] == []
    assert formats["brief"]["voices"].startswith("Brief: ")
    assert formats["brief"]["voices"].endswith(" alone, about 2 minutes.")
    # No source yet, and Automatic: a deck is read by the host alone.
    assert " and " not in o["deck_summary"].split(" · ")[2]
    assert o["deck_summary"].startswith("5 slides · about 5 min · ")
    assert o["language_note"] is None and o["build_language_note"] is None
    # Who answers in Ask, named as Settings names the model.
    assert o["ask_note"] == "Answers in English with Gemini 2.5 Flash Lite."
    assert o["show_cost"]
    assert o["research"] == {"label": "Standard research", "takes": "a few minutes"}
    up = o["upload"]
    assert up["accept"] == ".pdf,.docx,.pptx,.xlsx,.md,.markdown,.txt,.csv"
    assert (up["max_mb"], up["max_files"], up["max_links"]) == (25, 10, 8)
    assert up["hint"] == "PDF, Office, Markdown, text or CSV, up to 25 MB. Or drop them here."


async def test_the_create_panel_follows_a_change_of_settings(client: AsyncClient) -> None:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    for key, value in (
        (st.STYLE_KEY, "clay"),
        (st.AUDIO_FORMAT_KEY, "debate"),
        (st.AUDIO_LENGTH_KEY, "shorter"),
        (st.SLIDE_COUNT_KEY, "8"),
        (st.SPEAKER_COUNT_KEY, "2"),
        (st.SPEAKER1_NAME_KEY, "Ava"),
        (st.SPEAKER2_NAME_KEY, "Andrew"),
        (st.LANGUAGE_KEY, "French"),
        (st.RESEARCH_DEPTH_KEY, "quick"),
        (st.SHOW_COST_KEY, "off"),
    ):
        await _set(client, key, value)
    o = await _options(client, cid)
    assert (o["default_style"], o["default_audio_format"], o["default_audio_length"]) == (
        "clay",
        "debate",
        "shorter",
    )
    assert o["deck_summary"] == "8 slides · about 5 min · Ava and Andrew"
    assert next(f for f in o["audio_formats"] if f["id"] == "debate")["voices"] == (
        "Voices: Ava and Andrew."
    )
    assert o["language_note"] == "Writing in French."
    assert o["ask_note"].startswith("Answers in French with ")
    assert o["build_language_note"] in (
        "Writing in French.",
        "Writing in French · voices are English.",
    )
    assert not o["show_cost"]
    assert o["research"] == {"label": "Quick research", "takes": "about a minute"}


async def test_options_are_only_for_ones_own_collection(client: AsyncClient) -> None:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    them = await other_person(client, "them@test")
    assert (await client.get(f"/api/collections/{cid}/options", headers=them)).status_code == 404


def test_voices_that_keep_their_accent_are_said_beside_the_language() -> None:
    from opennotebook.api.settings import studio_options

    v = {d.key: d.default for d in st.CATALOGUE}
    v[st.LANGUAGE_KEY] = "German"
    v[st.SPEAKER1_VOICE_KEY] = "en-US-AvaMultilingualNeural"
    v[st.SPEAKER2_VOICE_KEY] = "en-US-GuyNeural"
    assert studio_options(v, 1).build_language_note == "Writing in German · voices are English."
    v[st.SPEAKER2_VOICE_KEY] = "en-US-AndrewMultilingualNeural"
    assert studio_options(v, 1).build_language_note == "Writing in German."
    # Automatic: two voices from two sources on.
    assert studio_options(v, 2).deck_summary.endswith(" and Expert")
