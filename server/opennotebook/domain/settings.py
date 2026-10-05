"""The studio's settings: what exists, what each accepts, and the value in
force for a person.

Every setting the studio has is one row of `CATALOGUE`. The settings page is
drawn from it and every write is checked against it, so the page cannot offer
a value the studio would not accept, and a setting added here appears on the
page without touching the page.

Each setting has a scope:

- `instance`: set once for everyone by whoever runs the studio. The models
  are, because they decide what every output costs and whether it works at
  all on the instance's AI endpoint.
- `user`: each person's own choice, such as their style, voices, language and
  spending limit.

A setting is looked up in this order, the first non-empty value winning:

1. an environment variable of the same name (`OPENNOTEBOOK_LANGUAGE=French`),
   which is the operator's override for everyone, as in the Rust studio;
2. the person's row in `user_settings` (user-scoped settings only);
3. the row in `instance_settings`;
4. the catalogue default, which is always a working value, so an empty
   database runs.

Secrets are not settings: the AI key comes from the environment only
(`config.Settings.ai_key`), never from these tables.
"""

import dataclasses
import functools
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.config import settings as config
from opennotebook.db.models import InstanceSetting, User, UserSetting
from opennotebook.domain.styles import STYLES
from opennotebook.errors import Problem
from opennotebook.speech import provider as speech_provider

Scope = Literal["instance", "user"]

# ── models ────────────────────────────────────────────────────────────────────

# The model that writes outlines, slide scripts and an output's bookends.
# Haiku since 2026-10-03: on content, nova-micro restated one fact on three
# slides and named things its source never mentioned, where Haiku kept a
# debate's sides apart. See docs/audio-overview-spec.md section 4.
SCRIPT_MODEL_KEY = "OPENNOTEBOOK_SCRIPT_MODEL"
SCRIPT_MODEL_DEFAULT = "anthropic/claude-haiku-4.5"

# The model that answers a spoken question: audio in, text out. A replacement
# has to accept audio input, or the question would need transcribing first.
ANSWER_MODEL_KEY = "OPENNOTEBOOK_ANSWER_MODEL"
ANSWER_MODEL_DEFAULT = "openai/gpt-audio-mini"

# Mind maps and study notes read every source whole in one call, so they need
# a long context window. See docs/mindmap-spec.md section 0.
MINDMAP_MODEL_KEY = "OPENNOTEBOOK_MINDMAP_MODEL"
MINDMAP_MODEL_DEFAULT = "google/gemini-2.5-flash-lite"
NOTES_MODEL_KEY = "OPENNOTEBOOK_NOTES_MODEL"
NOTES_MODEL_DEFAULT = "google/gemini-2.5-flash-lite"

# The model that writes the slides as HTML, figures included. Haiku does a
# five-slide deck for about $0.05 on OpenRouter's 2026-10-02 prices.
SLIDE_MODEL_KEY = "OPENNOTEBOOK_SLIDE_MODEL"
SLIDE_MODEL_DEFAULT = "anthropic/claude-haiku-4.5"

# Perplexity's Sonar does a real web search behind a chat completion and
# returns the pages it found.
SEARCH_MODEL_KEY = "OPENNOTEBOOK_SEARCH_MODEL"
SEARCH_MODEL_DEFAULT = "perplexity/sonar"

# The model behind chat and Ask, the add-sources assistant and collection
# naming. It has to call tools reliably, which the script model's cheaper
# alternatives do not.
CHAT_MODEL_KEY = "OPENNOTEBOOK_CHAT_MODEL"
AGENT_MODEL_DEFAULT = "google/gemini-2.5-flash-lite"

type Pairs = tuple[tuple[str, str], ...]

SLIDE_MODELS: Pairs = (
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5"),
)
SCRIPT_MODELS: Pairs = (
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("amazon/nova-micro-v1", "Amazon Nova Micro"),
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("qwen/qwen3.7-flash", "Qwen 3.7 Flash"),
    ("openai/gpt-5.4", "GPT-5.4"),
)
# Long-context models, for the steps that read every source whole.
LONG_MODELS: Pairs = (
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("openai/gpt-5.4", "GPT-5.4"),
)
# Models that call tools reliably, which the add-sources assistant needs.
CHAT_MODELS: Pairs = (
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("anthropic/claude-haiku-4.5", "Claude Haiku 4.5"),
    ("openai/gpt-5.4", "GPT-5.4"),
)
ANSWER_MODELS: Pairs = (
    ("openai/gpt-audio-mini", "GPT Audio Mini"),
    ("openai/gpt-audio", "GPT Audio"),
)
# Search models that return the pages they cite.
SEARCH_MODELS: Pairs = (
    ("perplexity/sonar", "Perplexity Sonar"),
    ("perplexity/sonar-pro", "Perplexity Sonar Pro"),
)

# ── the other keys ────────────────────────────────────────────────────────────

STYLE_KEY = "OPENNOTEBOOK_STYLE"
SLIDE_COUNT_KEY = "OPENNOTEBOOK_SLIDE_COUNT"
SESSION_MINUTES_KEY = "OPENNOTEBOOK_SESSION_MINUTES"
AUDIO_FORMAT_KEY = "OPENNOTEBOOK_AUDIO_FORMAT"
AUDIO_LENGTH_KEY = "OPENNOTEBOOK_AUDIO_LENGTH"
RESEARCH_DEPTH_KEY = "OPENNOTEBOOK_RESEARCH_DEPTH"
AUTO_NAME_KEY = "OPENNOTEBOOK_AUTO_NAME"
COVERS_KEY = "OPENNOTEBOOK_COVERS"
SPEAKER_COUNT_KEY = "OPENNOTEBOOK_SPEAKER_COUNT"
SPEAKER1_NAME_KEY = "OPENNOTEBOOK_SPEAKER1_NAME"
SPEAKER1_VOICE_KEY = "OPENNOTEBOOK_SPEAKER1_VOICE"
SPEAKER1_ROLE_KEY = "OPENNOTEBOOK_SPEAKER1_ROLE"
SPEAKER2_NAME_KEY = "OPENNOTEBOOK_SPEAKER2_NAME"
SPEAKER2_VOICE_KEY = "OPENNOTEBOOK_SPEAKER2_VOICE"
SPEAKER2_ROLE_KEY = "OPENNOTEBOOK_SPEAKER2_ROLE"
LANGUAGE_KEY = "OPENNOTEBOOK_LANGUAGE"
ANSWER_LENGTH_KEY = "OPENNOTEBOOK_ANSWER_LENGTH"
PATIENCE_KEY = "OPENNOTEBOOK_TURN_PATIENCE"
INTERRUPT_KEY = "OPENNOTEBOOK_INTERRUPT"
COURTESY_KEY = "OPENNOTEBOOK_COURTESY"
MAX_BUILD_USD_KEY = "OPENNOTEBOOK_MAX_BUILD_USD"
SHOW_COST_KEY = "OPENNOTEBOOK_SHOW_COST"

# How many slides a deck may have: one range for the setting and for every
# build request, the page's and an agent's alike. A request outside it is
# clamped into it.
SLIDES_MIN = 3
SLIDES_MAX = 12
SLIDES_DEFAULT = 5


def clamp_slides(n: int) -> int:
    return max(SLIDES_MIN, min(SLIDES_MAX, n))


# The voices offered depend on who reads aloud (`OPENNOTEBOOK_TTS_PROVIDER`,
# `speech/provider.py`): Microsoft's neural voices by default, Kokoro's on an
# OpenAI-compatible server. The catalogue below is written for the default;
# `for_provider` gives the voice and language rows for the provider in force.
VOICES: Pairs = speech_provider.voices(speech_provider.DEFAULT)
KOKORO_VOICES: Pairs = speech_provider.KOKORO_VOICES

# The languages narration and answers can be written in, with the BCP 47 tag
# the browser's speech recogniser wants for the same language.
LANGUAGES: Pairs = (
    ("English", "en-US"),
    ("Arabic", "ar-EG"),
    ("Chinese", "zh-CN"),
    ("Dutch", "nl-NL"),
    ("French", "fr-FR"),
    ("German", "de-DE"),
    ("Hindi", "hi-IN"),
    ("Italian", "it-IT"),
    ("Japanese", "ja-JP"),
    ("Portuguese", "pt-BR"),
    ("Spanish", "es-ES"),
)
LANGUAGE_CHOICES: Pairs = tuple((name, name) for name, _ in LANGUAGES)

AUDIO_FORMATS: Pairs = (
    ("deep_dive", "Deep Dive"),
    ("brief", "Brief"),
    ("critique", "Critique"),
    ("debate", "Debate"),
)
AUDIO_LENGTHS: Pairs = (
    ("shorter", "Shorter"),
    ("default", "Default"),
    ("longer", "Longer"),
)
RESEARCH_DEPTHS: Pairs = (
    ("quick", "Quick (about 1 min)"),
    ("standard", "Standard (a few min)"),
)

# ── the catalogue ─────────────────────────────────────────────────────────────

TAB_GENERAL = "General"
TAB_DEFAULTS = "Generation defaults"
TAB_VOICES = "Voices"
TAB_CONVERSATION = "Live conversation"
TAB_COSTS = "Costs & limits"
TAB_MODELS = "Models"


@dataclass(frozen=True)
class Tab:
    # A stable id, for links such as "Settings › Costs & limits".
    id: str
    label: str
    # Shown above the tab's rows.
    note: str = ""
    # Drawn collapsed until opened.
    advanced: bool = False


# The tabs, in display order. General comes first and is where Settings
# opens; the page draws the theme at its top, because the theme belongs to
# the browser rather than to the studio.
TAB_INFO: tuple[Tab, ...] = (
    Tab("general", TAB_GENERAL),
    Tab("defaults", TAB_DEFAULTS),
    Tab("voices", TAB_VOICES),
    Tab("conversation", TAB_CONVERSATION),
    Tab("costs", TAB_COSTS),
    Tab("models", TAB_MODELS, "Changing these affects quality and cost.", advanced=True),
)
TABS: tuple[str, ...] = tuple(t.label for t in TAB_INFO)


@dataclass(frozen=True)
class Kind:
    """What kind of control edits a setting, and what it accepts.

    - `choice`: one of `options`, a fixed list of (value, label) pairs;
    - `style`: one of the slide styles (`domain/styles.py`);
    - `number`: a whole number from `min` to `max`;
    - `toggle`: `on` or `off`;
    - `text`: free text, with `options` as suggestions;
    - `model`: a model id, one of the tested `options` or any id the AI
      endpoint offers (`check_model`).
    """

    name: Literal["choice", "style", "number", "toggle", "text", "model"]
    options: Pairs = ()
    min: int = 0
    max: int = 0


def choice(*options: tuple[str, str]) -> Kind:
    return Kind("choice", options)


def number(lo: int, hi: int) -> Kind:
    return Kind("number", min=lo, max=hi)


def model(tested: Pairs) -> Kind:
    return Kind("model", tested)


STYLE = Kind("style")
TOGGLE = Kind("toggle")
TEXT = Kind("text")


@dataclass(frozen=True)
class Def:
    # The name it is stored under, in the tables and the environment.
    key: str
    tab: str
    label: str
    help: str
    kind: Kind
    # A working value; never empty.
    default: str
    scope: Scope = "user"
    # A heading inside the tab that rows sharing it sit under; empty for none.
    group: str = ""
    # What a number counts, shown beside it ("slides", "min").
    unit: str = ""
    # Drawn in a collapsed "Advanced" section.
    advanced: bool = False


WRITING = "Writing"
CHAT = "Chat & answers"
HOST = "Host"
SECOND = "Second voice"
# The General tab's heading over how collections are named and drawn.
COLLECTIONS = "Collections"


def _model(key: str, label: str, help: str, tested: Pairs, default: str, group: str) -> Def:
    # Every model is the operator's: it decides what every output costs and
    # must be one the instance's AI endpoint serves.
    return Def(
        key,
        TAB_MODELS,
        label,
        help,
        model(tested),
        default,
        scope="instance",
        group=group,
        advanced=True,
    )


CATALOGUE: tuple[Def, ...] = (
    # Generation defaults
    Def(
        STYLE_KEY,
        TAB_DEFAULTS,
        "Slide style",
        "Starting style for new decks. You can pick another when you create one.",
        STYLE,
        "editorial",
    ),
    Def(
        SLIDE_COUNT_KEY,
        TAB_DEFAULTS,
        "Slides per deck",
        "Decks only. Audio overviews set their own length.",
        number(SLIDES_MIN, SLIDES_MAX),
        str(SLIDES_DEFAULT),
        unit="slides",
    ),
    Def(
        SESSION_MINUTES_KEY,
        TAB_DEFAULTS,
        "Deck narration length",
        "About how long the spoken script runs. Decks only.",
        number(2, 20),
        "5",
        unit="min",
    ),
    Def(
        AUDIO_FORMAT_KEY,
        TAB_DEFAULTS,
        "Audio overview format",
        "Starting format in the Create panel.",
        choice(*AUDIO_FORMATS),
        "deep_dive",
    ),
    Def(
        AUDIO_LENGTH_KEY,
        TAB_DEFAULTS,
        "Audio overview length",
        "Brief is always about 2 minutes.",
        choice(*AUDIO_LENGTHS),
        "default",
    ),
    Def(
        RESEARCH_DEPTH_KEY,
        TAB_DEFAULTS,
        "Web research depth",
        "How long Research a topic reads the web before it writes its report.",
        choice(*RESEARCH_DEPTHS),
        "standard",
    ),
    # General
    Def(
        LANGUAGE_KEY,
        TAB_GENERAL,
        "Output language",
        "Scripts, notes, maps, answers and chat are written in this. Multilingual voices speak "
        "it natively; the others keep their own accent.",
        choice(*LANGUAGE_CHOICES),
        "English",
    ),
    Def(
        AUTO_NAME_KEY,
        TAB_GENERAL,
        "Name collections automatically",
        "Titles a new collection from its sources. Your own title is never replaced.",
        TOGGLE,
        "on",
        group=COLLECTIONS,
    ),
    Def(
        COVERS_KEY,
        TAB_GENERAL,
        "Design collection covers",
        "Draws each collection's cover from its sources and what you made. One small model "
        "call when they change, well under a cent.",
        TOGGLE,
        "on",
        group=COLLECTIONS,
    ),
    # Voices
    Def(
        SPEAKER_COUNT_KEY,
        TAB_VOICES,
        "Speakers in a deck",
        "Automatic uses two voices when there are two or more sources. Audio overview "
        "formats choose their own.",
        choice(("auto", "Automatic"), ("1", "One"), ("2", "Two")),
        "auto",
    ),
    Def(
        SPEAKER1_NAME_KEY,
        TAB_VOICES,
        "Host name",
        "Shown beside everything they say, and used when they answer a question.",
        TEXT,
        "Host",
        group=HOST,
    ),
    Def(
        SPEAKER1_VOICE_KEY,
        TAB_VOICES,
        "Host voice",
        "Microsoft neural voice. Multilingual voices speak the output language natively.",
        choice(*VOICES),
        speech_provider.default_voice(speech_provider.DEFAULT, 1),
        group=HOST,
    ),
    Def(
        SPEAKER1_ROLE_KEY,
        TAB_VOICES,
        "Host persona",
        "A few words on who they are and how they talk.",
        TEXT,
        "narrator",
        group=HOST,
    ),
    Def(
        SPEAKER2_NAME_KEY,
        TAB_VOICES,
        "Second voice name",
        "Used for two-voice decks and Deep Dive, Critique and Debate.",
        TEXT,
        "Expert",
        group=SECOND,
    ),
    Def(
        SPEAKER2_VOICE_KEY,
        TAB_VOICES,
        "Second voice",
        "Microsoft neural voice. Multilingual voices speak the output language natively.",
        choice(*VOICES),
        speech_provider.default_voice(speech_provider.DEFAULT, 2),
        group=SECOND,
    ),
    Def(
        SPEAKER2_ROLE_KEY,
        TAB_VOICES,
        "Second voice persona",
        "A few words on who they are and how they talk.",
        TEXT,
        "expert",
        group=SECOND,
    ),
    # Live conversation
    Def(
        ANSWER_LENGTH_KEY,
        TAB_CONVERSATION,
        "Answer length",
        "How long a spoken answer to your question is.",
        choice(
            ("short", "Short, one sentence"),
            ("normal", "Normal, one or two sentences"),
            ("detailed", "Detailed, three or four sentences"),
        ),
        "normal",
    ),
    Def(
        PATIENCE_KEY,
        TAB_CONVERSATION,
        "Waiting for you to finish",
        "How long the studio waits after a pause that sounds unfinished, such as one after "
        '"and" or "um", before it answers. Takes effect next time you open the player.',
        choice(
            ("quick", "Quick, 1 second"),
            ("balanced", "Balanced, 2 seconds"),
            ("patient", "Patient, 4 seconds"),
        ),
        "balanced",
    ),
    Def(
        INTERRUPT_KEY,
        TAB_CONVERSATION,
        "Let me talk over the studio",
        "When on, starting to speak while the studio is talking stops it and gives you the "
        "floor. Takes effect next time you open the player.",
        TOGGLE,
        "on",
    ),
    Def(
        COURTESY_KEY,
        TAB_CONVERSATION,
        "Courtesy lines",
        'The short "go ahead" and "good question" lines around a question. Skipped when '
        "the output language is not English.",
        TOGGLE,
        "on",
    ),
    # Costs & limits
    Def(
        MAX_BUILD_USD_KEY,
        TAB_COSTS,
        "Spending limit per output",
        "A deck or audio overview whose highest estimate is over this is refused before it "
        "starts. Mind maps and notes cost under a cent and are not limited.",
        choice(
            ("0.25", "$0.25"),
            ("0.50", "$0.50"),
            ("1", "$1"),
            ("2", "$2"),
            ("5", "$5"),
            ("off", "No limit"),
        ),
        "0.50",
    ),
    Def(
        SHOW_COST_KEY,
        TAB_COSTS,
        "Show cost before generating",
        "Shows the estimate in the Create panel. A build over your limit is always flagged.",
        TOGGLE,
        "on",
    ),
    # Models
    _model(
        SCRIPT_MODEL_KEY,
        "Script model",
        "Writes the outline and every spoken line. It must keep two speakers apart.",
        SCRIPT_MODELS,
        SCRIPT_MODEL_DEFAULT,
        WRITING,
    ),
    _model(
        SLIDE_MODEL_KEY,
        "Slide model",
        "Writes every slide and draws its figure. Sonnet takes more care over layouts and "
        "costs about twice as much as Haiku.",
        SLIDE_MODELS,
        SLIDE_MODEL_DEFAULT,
        WRITING,
    ),
    _model(
        MINDMAP_MODEL_KEY,
        "Mind map model",
        "Reads every source whole and draws the mind map. It needs a long context window.",
        LONG_MODELS,
        MINDMAP_MODEL_DEFAULT,
        WRITING,
    ),
    _model(
        NOTES_MODEL_KEY,
        "Study notes model",
        "Reads every source whole and writes notes that cite their passages. It needs a long "
        "context window.",
        LONG_MODELS,
        NOTES_MODEL_DEFAULT,
        WRITING,
    ),
    _model(
        CHAT_MODEL_KEY,
        "Chat & Ask model",
        "Answers in Ask, runs the add-sources assistant and names collections. Empty uses "
        "Gemini 2.5 Flash Lite.",
        CHAT_MODELS,
        AGENT_MODEL_DEFAULT,
        CHAT,
    ),
    _model(
        ANSWER_MODEL_KEY,
        "Spoken answer model",
        "Answers questions asked aloud in the player. It must accept audio input.",
        ANSWER_MODELS,
        ANSWER_MODEL_DEFAULT,
        CHAT,
    ),
    _model(
        SEARCH_MODEL_KEY,
        "Web search model",
        "Finds pages for the add-sources assistant. It must return the pages it cites.",
        SEARCH_MODELS,
        SEARCH_MODEL_DEFAULT,
        CHAT,
    ),
)

_BY_KEY = {d.key: d for d in CATALOGUE}

# The voice settings, by speaker: the host is 1, the second voice 2.
VOICE_KEYS: dict[str, Literal[1, 2]] = {SPEAKER1_VOICE_KEY: 1, SPEAKER2_VOICE_KEY: 2}


@functools.cache
def _for(p: speech_provider.Provider) -> dict[str, Def]:
    """The catalogue as it reads under the speech provider `p`."""
    out = dict(_BY_KEY)
    if speech_provider.is_microsoft(p):
        return out
    # An OpenAI-compatible server, with Kokoro's English voices.
    for key, speaker in VOICE_KEYS.items():
        out[key] = dataclasses.replace(
            out[key],
            help="Local English voice.",
            kind=choice(*speech_provider.voices(p)),
            default=speech_provider.default_voice(p, speaker),
        )
    out[LANGUAGE_KEY] = dataclasses.replace(
        out[LANGUAGE_KEY],
        help="Scripts, notes, maps, answers and chat are written in this. Voices stay "
        "English-accented.",
    )
    return out


def for_provider(d: Def) -> Def:
    """`d` as it reads under the speech provider in force: the voices it
    offers and their default, and what the language row says about them."""
    return _for(speech_provider.provider()).get(d.key, d)


def fit(key: str, v: str) -> str:
    """A voice the provider in force cannot read in is read as its default
    for that speaker: a voice kept from another provider never fails a
    build. Any other value is returned as it is."""
    speaker = VOICE_KEYS.get(key)
    if speaker is None or not v:
        return v
    return speech_provider.resolve(speech_provider.provider(), v, speaker)


def find(key: str) -> Def | None:
    """The catalogue entry for a key, as it reads under the speech provider in
    force."""
    d = _BY_KEY.get(key)
    return None if d is None else for_provider(d)


def need(key: str) -> Def:
    """The catalogue entry for a key a caller asks for by name."""
    if (d := find(key)) is None:
        raise Problem(
            404, f"There is no setting called {key}. Reload the page to see the settings there are."
        )
    return d


# ── checking a value ──────────────────────────────────────────────────────────

MAX_TEXT_BYTES = 400
MAX_MODEL_BYTES = 200
_WHOLE_NUMBER = re.compile(r"[+-]?[0-9]+")
# `vendor/name`, with the `:free`-style suffixes some endpoints use.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:+-]*")


def validate(d: Def, value: str) -> str | None:
    """Why `value` is not acceptable for `d`, or None when it is. An empty
    value always is: it means "use the default". Styles and untested models
    are checked further by `check`."""
    v = value.strip()
    if not v:
        return None
    match d.kind.name:
        case "choice":
            if any(o == v for o, _ in d.kind.options):
                return None
            return f"“{v}” is not one of the choices for {d.label}. Pick one from the list."
        case "number":
            if _WHOLE_NUMBER.fullmatch(v) and d.kind.min <= int(v) <= d.kind.max:
                return None
            return (
                f"{d.label} must be a whole number from {d.kind.min} to {d.kind.max}. "
                "Enter one in that range."
            )
        case "toggle":
            return None if v in ("on", "off") else f"{d.label} must be on or off."
        case "text":
            if len(v.encode()) > MAX_TEXT_BYTES:
                return f"{d.label} is too long. Keep it under {MAX_TEXT_BYTES} characters."
            return None
        case "model":
            if len(v.encode()) > MAX_MODEL_BYTES or any(c.isspace() for c in v):
                return (
                    f"“{v}” is not a model id. Model ids look like "
                    f"{SCRIPT_MODEL_DEFAULT}; check the spelling and try again."
                )
            return None
        case "style":
            return None


async def check_model(model_id: str) -> str | None:
    """Why `model_id` cannot be used, or None when it can.

    The id must look like `vendor/name`, and the AI endpoint's model list must
    name it. When the list cannot be read, the id is accepted on its shape, as
    the Rust studio did: an unreachable catalogue is not a reason to refuse."""
    if not _MODEL_ID.fullmatch(model_id):
        return (
            f"“{model_id}” is not a model id. Model ids look like {SCRIPT_MODEL_DEFAULT}; "
            "check the spelling and try again."
        )
    from opennotebook.ai.client import ai

    listed = await ai().catalogue.prices()
    if listed and model_id.split(":")[0] not in {m.split(":")[0] for m in listed}:
        return (
            f"The AI endpoint does not offer “{model_id}”. Pick a model from the list, "
            "or check the id on the provider's site."
        )
    return None


async def check(d: Def, value: str) -> str | None:
    """Why `value` may not be stored for `d`, or None when it may."""
    v = value.strip()
    if why := validate(d, v):
        return why
    if not v:
        return None
    if d.kind.name == "style" and not any(s.id == v for s in STYLES):
        return f"“{v}” is not a slide style. Pick one of the styles in the list."
    if d.kind.name == "model" and not any(o == v for o, _ in d.kind.options):
        return await check_model(v)
    return None


# ── the value in force ────────────────────────────────────────────────────────


def from_env(key: str, env: Mapping[str, str] | None = None) -> str:
    """The operator's override from the environment, or empty."""
    return (os.environ if env is None else env).get(key, "").strip()


def effective(
    key: str,
    user: Mapping[str, str],
    instance: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> str:
    """The value in force for `key`, given a person's rows and the instance's
    rows as plain mappings: the environment, then the person's row (for a
    user-scoped setting), then the instance's row, then the default. Never
    fails: an unknown key with nothing stored is empty."""
    d = find(key)
    if v := from_env(key, env):
        return fit(key, v)
    if (d is None or d.scope == "user") and (v := user.get(key, "").strip()):
        return fit(key, v)
    if v := instance.get(key, "").strip():
        return fit(key, v)
    return d.default if d else ""


async def rows(s: AsyncSession, owner: uuid.UUID) -> tuple[dict[str, str], dict[str, str]]:
    """A person's stored settings and the instance's, as mappings."""
    mine = await s.execute(
        select(UserSetting.key, UserSetting.value).where(UserSetting.owner_id == owner)
    )
    shared = await s.execute(select(InstanceSetting.key, InstanceSetting.value))
    user = {k: v for k, v in mine}
    instance = {k: v for k, v in shared}
    return user, instance


async def values(s: AsyncSession, owner: uuid.UUID) -> dict[str, str]:
    """Every catalogued setting's value in force for a person, read in one go,
    for a build that needs several."""
    user, instance = await rows(s, owner)
    return {d.key: effective(d.key, user, instance) for d in CATALOGUE}


async def value(s: AsyncSession, owner: uuid.UUID, key: str) -> str:
    """One setting's value in force for a person."""
    if v := from_env(key):
        return fit(key, v)
    d = find(key)
    if d is None or d.scope == "user":
        mine = await s.scalar(
            select(UserSetting.value).where(UserSetting.owner_id == owner, UserSetting.key == key)
        )
        if mine and mine.strip():
            return fit(key, mine.strip())
    shared = await s.scalar(select(InstanceSetting.value).where(InstanceSetting.key == key))
    if shared and shared.strip():
        return fit(key, shared.strip())
    return d.default if d else ""


@dataclass(frozen=True)
class Current:
    """One setting as the page sees it for a person.

    `value` is the override in force for them (the environment's, else their
    own row for a user setting, else the instance row for an instance one),
    empty when there is none. `default` is what applies when `value` is
    empty: for a user setting, the instance's value when the operator set
    one, else the catalogue's."""

    d: Def
    value: str
    default: str

    @property
    def effective(self) -> str:
        return self.value or self.default


def offered(key: str, v: str) -> str:
    """`v`, or empty for a voice the provider in force cannot read in: it is
    not offered, so the page shows the default that is read in instead."""
    if key in VOICE_KEYS and v and not speech_provider.belongs(speech_provider.provider(), v):
        return ""
    return v


def current(d: Def, user: Mapping[str, str], instance: Mapping[str, str]) -> Current:
    d = for_provider(d)
    env = offered(d.key, from_env(d.key))
    shared = offered(d.key, instance.get(d.key, "").strip())
    if d.scope == "instance":
        return Current(d, env or shared, d.default)
    own = offered(d.key, user.get(d.key, "").strip())
    return Current(d, env or own, shared or d.default)


async def describe(s: AsyncSession, owner: uuid.UUID) -> list[Current]:
    """Every catalogued setting as it stands for a person."""
    user, instance = await rows(s, owner)
    return [current(d, user, instance) for d in CATALOGUE]


def may_change(d: Def, who: User) -> str | None:
    """Why `who` may not change `d`, or None when they may.

    A user setting is anyone's own. An instance setting changes the studio for
    everyone: for now only the local owner may, in `local` mode, the studio
    run by one person. There are no admin roles yet, so in `keys` mode nobody
    can change one through the API."""
    if d.scope == "user":
        return None
    cfg = config()
    if cfg.auth == "local" and who.email.lower() == cfg.local_owner_email.lower():
        return None
    return (
        f"{d.label} is set for everyone by whoever runs this studio, so it cannot be "
        "changed here. Ask them to change it."
    )


async def save(s: AsyncSession, who: User, key: str, value: str) -> Current:
    """Check and store one setting for `who`, and return it as it now stands.

    An empty value, or the value that would apply anyway, removes the row
    rather than storing a copy, so "never changed" and "changed back" are the
    same thing and a later change of default reaches everyone who did not
    choose otherwise. An environment variable of the same name still wins over
    whatever is stored."""
    d = need(key)
    if why := may_change(d, who):
        raise Problem(403, why)
    v = value.strip()
    if why := await check(d, v):
        raise Problem(422, why)
    user, instance = await rows(s, who.id)
    fallback = current(d, {}, instance).default
    if d.scope == "user":
        if not v or v == fallback:
            await s.execute(
                delete(UserSetting).where(UserSetting.owner_id == who.id, UserSetting.key == key)
            )
            user.pop(key, None)
        else:
            await s.execute(
                insert(UserSetting)
                .values(owner_id=who.id, key=key, value=v)
                .on_conflict_do_update(
                    index_elements=[UserSetting.owner_id, UserSetting.key],
                    set_={"value": v, "updated_at": func.now()},
                )
            )
            user[key] = v
    elif not v or v == fallback:
        await s.execute(delete(InstanceSetting).where(InstanceSetting.key == key))
        instance.pop(key, None)
    else:
        await s.execute(
            insert(InstanceSetting)
            .values(key=key, value=v)
            .on_conflict_do_update(
                index_elements=[InstanceSetting.key],
                set_={"value": v, "updated_at": func.now()},
            )
        )
        instance[key] = v
    return current(d, user, instance)


# ── reading a value ───────────────────────────────────────────────────────────


def or_default(own: str, default: str) -> str:
    return own if own.strip() else default


def research_tier(v: str) -> str:
    """A stored depth as a tier research knows; anything else is standard."""
    return "quick" if v.strip() == "quick" else "standard"


def language_tag(name: str) -> str:
    """The BCP 47 tag for a language name, for the browser's recogniser."""
    return next((tag for n, tag in LANGUAGES if n == name), "en-US")


def language_rule(name: str) -> str:
    """A sentence for a system prompt that pins the output language, or
    nothing for English, which every prompt was already written for."""
    if not name or name == "English":
        return ""
    return f"Write everything you say in {name}, whatever language the material is in."


def parse_limit(v: str) -> float | None:
    """`"0.50"` is 0.5; `"off"` or anything unreadable is no limit."""
    try:
        x = float(v.strip())
    except ValueError:
        return None
    return x if x > 0 else None


def is_on(v: str) -> bool:
    return v != "off"


def session_minutes(v: str) -> int:
    """A stored narration length, brought into its range; 5 when unreadable."""
    v = v.strip()
    n = int(v) if _WHOLE_NUMBER.fullmatch(v) else 5
    return max(2, min(20, n))
