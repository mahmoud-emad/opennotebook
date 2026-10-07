"""The studio's settings, and the slide styles."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from opennotebook.ai.client import ai
from opennotebook.ai.prices import Price, hint
from opennotebook.api.deps import Db, Me
from opennotebook.db.session import release
from opennotebook.domain import collections, settings, styles
from opennotebook.domain import sessions as sess
from opennotebook.domain import sessions_estimate as est
from opennotebook.domain import sources as sources_domain
from opennotebook.domain.settings import TAB_INFO, Current
from opennotebook.domain.styles import STYLES, SlideStyle

router = APIRouter(prefix="/api", tags=["settings"])


class SettingOption(BaseModel):
    value: str
    label: str
    hint: str = Field(description="A model's price; empty when unknown or not a model")


class Setting(BaseModel):
    key: str
    tab: str
    group: str
    label: str
    help: str
    kind: str = Field(description="choice, number, toggle or text; a model is text")
    value: str = Field(
        description="The override in force for this person; empty means the default applies"
    )
    default: str = Field(
        description="What applies when value is empty: for a user setting, the instance's "
        "value when one is set, else the studio's default"
    )
    options: list[SettingOption]
    suggestions: list[str] = Field(description="Values a text setting offers; a model's tested ids")
    min: int | None = None
    max: int | None = None
    unit: str
    advanced: bool
    model: bool
    price: str = Field(description="The price of the model in force; empty when unknown")
    scope: str = Field(description="instance: set for everyone; user: each person's own")

    @classmethod
    def of(cls, c: Current, prices: dict[str, Price] | None = None) -> Setting:
        d = c.d
        kind = d.kind
        options: list[SettingOption] = []
        lo: int | None = None
        hi: int | None = None
        match kind.name:
            case "choice" | "model":
                options = [
                    SettingOption(value=v, label=lb, hint=_hint(prices, v))
                    for v, lb in kind.options
                ]
            case "style":
                options = [SettingOption(value=s.id, label=s.label, hint="") for s in STYLES]
            case "number":
                lo, hi = kind.min, kind.max
            case "toggle" | "text":
                pass
        # A model is text on the wire, so a page that knows only text still
        # edits it; `model` says what it is.
        wire = {"style": "choice", "model": "text"}.get(kind.name, kind.name)
        return cls(
            key=d.key,
            tab=d.tab,
            group=d.group,
            label=d.label,
            help=d.help,
            kind=wire,
            value=c.value,
            default=c.default,
            options=options,
            suggestions=[v for v, _ in kind.options] if kind.name == "model" else [],
            min=lo,
            max=hi,
            unit=d.unit,
            advanced=d.advanced,
            model=kind.name == "model",
            price=_hint(prices, c.value or c.default) if kind.name == "model" else "",
            scope=d.scope,
        )


def _hint(prices: dict[str, Price] | None, model: str) -> str:
    p = (prices or {}).get(model)
    return hint(p) if p else ""


class SettingTab(BaseModel):
    id: str
    label: str
    note: str
    advanced: bool


class SettingsOut(BaseModel):
    tabs: list[SettingTab]
    settings: list[Setting]


class SetValue(BaseModel):
    value: str = Field(description="Empty resets it to the default")


class Style(BaseModel):
    id: str
    label: str
    blurb: str


@router.get("/settings")
async def get_settings(s: Db, me: Me) -> SettingsOut:
    """Every setting with its current value, default and allowed values."""
    described = await settings.describe(s, me.id)
    # The price list can take seconds to read; no transaction waits for it.
    await release(s)
    prices = await ai().catalogue.prices()
    return SettingsOut(
        tabs=[
            SettingTab(id=t.id, label=t.label, note=t.note, advanced=t.advanced) for t in TAB_INFO
        ],
        settings=[Setting.of(c, prices) for c in described],
    )


@router.patch("/settings/{key}")
async def set_setting(key: str, body: SetValue, s: Db, me: Me) -> Setting:
    """Change one setting; an empty value resets it. A user setting changes
    only for the person asking; an instance setting changes for everyone and
    only whoever runs the studio may change it."""
    saved = await settings.save(s, me, key, body.value)
    # Saved before the price list is read, which can take seconds.
    await release(s)
    return Setting.of(saved, await ai().catalogue.prices())


# ── what the Create panel offers ─────────────────────────────────────────────

# The style pictures are part of the web app, under its mount.
THUMBNAILS = "assets/styles"


class StyleChoice(Style):
    thumbnail: str = Field(
        description="The style's picture, relative to the web app's mount: `assets/styles/…`"
    )


class LengthChoice(BaseModel):
    id: str
    label: str


class FormatChoice(BaseModel):
    """An audio overview's format, as the Create panel offers it."""

    id: str
    label: str
    blurb: str
    lengths: list[LengthChoice] = Field(
        description="The lengths it offers to choose from; empty when it has one, as Brief does"
    )
    voices: str = Field(description="Who it is read by, in a sentence: `Voices: Ava and Andrew.`")


class SpeakerChoice(BaseModel):
    """How many voices read a deck, as the Create panel offers it."""

    count: int = Field(ge=1, le=2)
    label: str = Field(description="`One` or `Two`")
    voices: str = Field(description="Who reads it, in a sentence: `Voices: Ava and Andrew.`")


class ResearchHint(BaseModel):
    label: str = Field(description="`Quick research` or `Standard research`")
    takes: str = Field(description="How long it reads the web: `about a minute`")


class UploadRules(BaseModel):
    """What the sources panel takes. The server refuses anything else."""

    extensions: list[str] = Field(description="Without the dot, lower case")
    accept: str = Field(description="The file picker's `accept`, from the same list")
    kinds: str = Field(description="What can be uploaded, in words")
    max_mb: int
    max_files: int = Field(description="Files one upload takes")
    max_links: int = Field(description="Links one add takes")
    hint: str = Field(description="What can be uploaded, as the add box says it")
    title: str = Field(description="The same, as the Upload button's tooltip")


class StudioOptions(BaseModel):
    """Everything the Create panel offers for one collection, and how it says
    it: the choices, the starting picks from the person's settings, and the
    lines it shows from them."""

    styles: list[StyleChoice]
    audio_formats: list[FormatChoice]
    default_style: str
    default_audio_format: str
    default_audio_length: str
    deck_summary: str = Field(
        description="What a deck of this collection is made with: `5 slides · about 5 min`"
    )
    deck_speakers: list[SpeakerChoice] = Field(
        description="A deck's speaker counts to choose from, one and two, with their voices"
    )
    default_deck_speakers: int = Field(
        description="The count a deck of this collection starts on, as Settings › Voices "
        "decide: Automatic is two from two sources on"
    )
    language_note: str | None = Field(
        description="The output language when it is not English: `Writing in French.`"
    )
    build_language_note: str | None = Field(
        description="The same for a deck or an audio overview, saying when the voices keep "
        "their English accent"
    )
    ask_note: str = Field(
        description="Who answers in Ask, in the words of Settings: "
        "`Answers in English with Gemini 2.5 Flash Lite.`"
    )
    show_cost: bool = Field(description="Say what a tool costs before it is made")
    research: ResearchHint
    upload: UploadRules


def _style_choice(st: SlideStyle) -> StyleChoice:
    return StyleChoice(
        id=st.id, label=st.label, blurb=st.blurb, thumbnail=f"{THUMBNAILS}/{st.id}.jpg"
    )


def _shown(key: str, value: str) -> str:
    """A setting's value as the Settings page shows it: its option's label,
    a model's name, or the value itself."""
    d = settings.find(key)
    if d is None:
        return value
    label = next((label for v, label in d.kind.options if v == value), None)
    if label is not None:
        return label
    return est.model_name(value) if d.kind.name == "model" and value else value


def studio_options(v: dict[str, str], sources: int) -> StudioOptions:
    """The Create panel's offer for a collection of `sources` sources, from
    the settings values `v` in force."""
    host, second = v[settings.SPEAKER1_NAME_KEY].strip(), v[settings.SPEAKER2_NAME_KEY].strip()
    # A deck's voices follow the plan's rule: two on Automatic with two or
    # more sources.
    count = v[settings.SPEAKER_COUNT_KEY].strip()
    two = {"1": False, "2": True}.get(count, sources >= 2)
    deck = (
        f"{v[settings.SLIDE_COUNT_KEY].strip()} slides · "
        f"about {v[settings.SESSION_MINUTES_KEY].strip()} min"
    )
    deck_speakers = [
        SpeakerChoice(count=1, label="One", voices=f"Voices: {host} alone."),
        SpeakerChoice(count=2, label="Two", voices=f"Voices: {host} and {second}."),
    ]
    formats = [
        FormatChoice(
            id=f,
            label=sess.FORMAT_LABELS[f],
            blurb=sess.FORMAT_BLURBS[f],
            lengths=[
                LengthChoice(id=n, label=sess.LENGTH_LABELS[n]) for n in sess.format_lengths(f)
            ]
            if len(sess.format_lengths(f)) > 1
            else [],
            voices=f"Brief: {host} alone, about 2 minutes."
            if f == "brief"
            else f"Voices: {host} and {second}.",
        )
        for f in sess.FORMATS
    ]
    language = v[settings.LANGUAGE_KEY].strip()
    other = language if language and language != "English" else None
    # Microsoft's Multilingual voices speak any output language natively; the
    # others keep an English accent.
    native = all(
        "Multilingual" in v[k] for k in (settings.SPEAKER1_VOICE_KEY, settings.SPEAKER2_VOICE_KEY)
    )
    quick = v[settings.RESEARCH_DEPTH_KEY].strip() == "quick"
    exts: list[str] = list(sources_domain.UPLOAD_EXTENSIONS)
    kinds = sources_domain.UPLOAD_KINDS
    mb = sources_domain.MAX_UPLOAD_BYTES // (1024 * 1024)
    return StudioOptions(
        styles=[_style_choice(st) for st in STYLES],
        audio_formats=formats,
        default_style=(styles.style(v[settings.STYLE_KEY].strip()) or styles.DEFAULT_STYLE).id,
        default_audio_format=sess.parse_format(v[settings.AUDIO_FORMAT_KEY]),
        default_audio_length=sess.parse_length(v[settings.AUDIO_LENGTH_KEY]),
        deck_summary=deck,
        deck_speakers=deck_speakers,
        default_deck_speakers=2 if two else 1,
        language_note=f"Writing in {other}." if other else None,
        build_language_note=(
            f"Writing in {other}." if native else f"Writing in {other} · voices are English."
        )
        if other
        else None,
        ask_note=f"Answers in {_shown(settings.LANGUAGE_KEY, language or 'English')} with "
        f"{_shown(settings.CHAT_MODEL_KEY, v[settings.CHAT_MODEL_KEY].strip())}.",
        show_cost=v[settings.SHOW_COST_KEY].strip() != "off",
        research=ResearchHint(
            label="Quick research" if quick else "Standard research",
            takes="about a minute" if quick else "a few minutes",
        ),
        upload=UploadRules(
            extensions=exts,
            accept=",".join(f".{e}" for e in exts),
            kinds=kinds,
            max_mb=mb,
            max_files=sources_domain.MAX_FILES,
            max_links=sources_domain.MAX_URLS,
            hint=f"PDF, Office, Markdown, text or CSV, up to {mb} MB. Or drop them here.",
            title=f"{kinds}, up to {mb} MB each. Or drop files on this panel.",
        ),
    )


@router.get("/collections/{cid}/options")
async def get_studio_options(cid: uuid.UUID, s: Db, me: Me) -> StudioOptions:
    """What the Create panel offers for a collection, from the person's
    settings: the styles and formats with their pictures and lengths, the
    starting picks, the lines it shows (a deck's slides and voices, the
    output language), how long research takes, and what an upload takes."""
    await collections.owned(s, me.id, cid)
    return studio_options(await settings.values(s, me.id), await collections.source_count(s, cid))


@router.get("/styles")
async def list_styles(me: Me) -> list[StyleChoice]:
    """The slide styles a deck can be built in, the default first, each with
    its picture."""
    return [_style_choice(st) for st in STYLES]
