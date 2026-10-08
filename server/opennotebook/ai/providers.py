"""The AI providers the studio knows how to talk to, as presets.

Every provider here is reached over the OpenAI chat-completions protocol, at
its own OpenAI-compatible address. What differs is told by its preset: where
it lives, which environment variable holds its key, what it can do, how a key
is checked and a balance read, and which of its models suit each kind of work.

A model the studio uses is chosen by role (`Role`), so a preset can say which
of its models to suggest for writing slides or answering a spoken question.
Suggestions are tried in order against the provider's own model list, so a
model it no longer offers is skipped rather than chosen. A provider with no
suggestion for a role leaves that role to another provider, or to the person.
"""

from dataclasses import dataclass, field
from typing import Literal

# What a model is used for. Each model setting has one (`ROLE_OF`).
type Role = Literal[
    "text",  # outlines and spoken scripts: cheap and careful
    "strong",  # slides as HTML, whiteboard scenes
    "strongest",  # a scene written once more when repairs failed
    "vision",  # checks a finished board, so it reads images
    "long",  # reads every source whole: mind maps, study notes
    "chat",  # Ask, the add-sources assistant, naming: calls tools reliably
    "audio",  # hears a spoken question
    "search",  # searches the web and returns the pages it cites
    "image",  # paints a picture
    "embed",  # turns text into vectors for search
]

ROLES: tuple[Role, ...] = (
    "text",
    "strong",
    "strongest",
    "vision",
    "long",
    "chat",
    "audio",
    "search",
    "image",
    "embed",
)

# The roles one main model can stand in for when a provider suggests nothing
# better: every role that only reads and writes text.
TEXT_ROLES: tuple[Role, ...] = ("text", "strong", "strongest", "long", "chat")

ROLE_LABELS: dict[Role, str] = {
    "text": "Scripts",
    "strong": "Slides and videos",
    "strongest": "Hard video scenes",
    "vision": "Checking pictures",
    "long": "Mind maps and notes",
    "chat": "Chat and Ask",
    "audio": "Spoken questions",
    "search": "Web search",
    "image": "Pictures",
    "embed": "Search embeddings",
}

# How a key is checked without spending anything.
type KeyCheck = Literal["openrouter", "deepseek", "models", "none"]


@dataclass(frozen=True)
class Preset:
    kind: str
    label: str
    base_url: str
    # Where the operator puts the key; the first set wins.
    key_env: tuple[str, ...]
    # Where the operator moves the address, for a provider that runs anywhere.
    url_env: tuple[str, ...] = ()
    needs_key: bool = True
    # Where to get a key, said in the setup tour.
    key_page: str = ""
    blurb: str = ""
    key_check: KeyCheck = "models"
    # What the provider accepts on the wire.
    json_schema: bool = True  # strict `response_format: json_schema`
    usage_cost: bool = False  # `usage: {include: true}` reports the cost
    stream_usage: bool = True  # `stream_options: {include_usage: true}`
    images: bool = False  # `modalities: [image, text]` paints pictures
    # What the answer's length limit is called; OpenAI's reasoning models
    # refuse `max_tokens`.
    max_tokens_field: str = "max_tokens"
    # Models, by the start of their id, that take only the default
    # temperature: OpenAI's reasoning models refuse any other.
    fixed_temperature: tuple[str, ...] = ()
    # Prices are in its model list, OpenRouter's way.
    listed_prices: bool = False
    # Whether the setup tour offers it; `custom` is offered as its own form.
    listed: bool = True
    # Model ids to suggest for each role, best first.
    suggest: dict[Role, tuple[str, ...]] = field(default_factory=dict[Role, tuple[str, ...]])


PRESETS: tuple[Preset, ...] = (
    Preset(
        kind="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        key_env=("OPENROUTER_API_KEY",),
        key_page="openrouter.ai/settings/keys",
        blurb="One key for hundreds of models. Every feature works with it, voice questions, "
        "web search and pictures included.",
        key_check="openrouter",
        usage_cost=True,
        images=True,
        listed_prices=True,
        suggest={
            "text": ("anthropic/claude-haiku-4.5",),
            "strong": ("anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5"),
            "strongest": ("anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5"),
            "vision": ("anthropic/claude-haiku-4.5",),
            "long": ("google/gemini-2.5-flash-lite",),
            "chat": ("google/gemini-2.5-flash-lite",),
            "audio": ("openai/gpt-audio-mini", "openai/gpt-audio"),
            "search": ("perplexity/sonar", "perplexity/sonar-pro"),
            "image": ("google/gemini-3.1-flash-image", "google/gemini-2.5-flash-image"),
        },
    ),
    Preset(
        kind="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        key_env=("OPENAI_API_KEY",),
        key_page="platform.openai.com/api-keys",
        blurb="GPT models, spoken questions and search embeddings.",
        max_tokens_field="max_completion_tokens",
        fixed_temperature=("gpt-5", "o1", "o3", "o4"),
        suggest={
            "text": ("gpt-5.4-mini", "gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"),
            "strong": ("gpt-5.4", "gpt-5", "gpt-4.1"),
            "strongest": ("gpt-5.4", "gpt-5", "gpt-4.1"),
            "vision": ("gpt-5.4-mini", "gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"),
            "long": ("gpt-5.4-mini", "gpt-5-mini", "gpt-4.1-mini"),
            "chat": ("gpt-5.4-mini", "gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"),
            "audio": ("gpt-audio-mini", "gpt-audio", "gpt-4o-mini-audio-preview"),
            "embed": ("text-embedding-3-small",),
        },
    ),
    Preset(
        kind="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com/v1",
        key_env=("ANTHROPIC_API_KEY",),
        key_page="console.anthropic.com/settings/keys",
        blurb="Claude models for writing, slides and chat.",
        # Its OpenAI-compatible layer ignores `response_format`: the schema is
        # asked for in words instead.
        json_schema=False,
        suggest={
            "text": ("claude-haiku-5-5", "claude-haiku-4-5"),
            "strong": ("claude-sonnet-5-5", "claude-sonnet-4-5", "claude-haiku-4-5"),
            "strongest": ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-5"),
            "vision": ("claude-haiku-5-5", "claude-haiku-4-5"),
            "long": ("claude-haiku-5-5", "claude-haiku-4-5"),
            "chat": ("claude-haiku-5-5", "claude-haiku-4-5"),
        },
    ),
    Preset(
        kind="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        key_page="aistudio.google.com/apikey",
        blurb="Gemini models with long context, spoken questions and search embeddings.",
        suggest={
            "text": ("gemini-2.5-flash-lite", "gemini-2.5-flash"),
            "strong": ("gemini-2.5-pro", "gemini-2.5-flash"),
            "strongest": ("gemini-2.5-pro",),
            "vision": ("gemini-2.5-flash", "gemini-2.5-flash-lite"),
            "long": ("gemini-2.5-flash-lite", "gemini-2.5-flash"),
            "chat": ("gemini-2.5-flash-lite", "gemini-2.5-flash"),
            "audio": ("gemini-2.5-flash", "gemini-2.5-flash-lite"),
            "embed": ("gemini-embedding-001",),
        },
    ),
    Preset(
        kind="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        key_env=("MISTRAL_API_KEY",),
        key_page="console.mistral.ai/api-keys",
        blurb="Mistral models and search embeddings.",
        suggest={
            "text": ("mistral-small-latest",),
            "strong": ("mistral-large-latest", "mistral-medium-latest"),
            "strongest": ("mistral-large-latest",),
            "vision": ("mistral-medium-latest", "mistral-small-latest"),
            "long": ("mistral-medium-latest", "mistral-small-latest"),
            "chat": ("mistral-small-latest",),
            "embed": ("mistral-embed",),
        },
    ),
    Preset(
        kind="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        key_env=("GROQ_API_KEY",),
        key_page="console.groq.com/keys",
        blurb="Open models served very fast.",
        # Strict schemas only on a few models, and never with tools.
        json_schema=False,
        suggest={
            "text": ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"),
            "strong": ("openai/gpt-oss-120b",),
            "strongest": ("openai/gpt-oss-120b",),
            "long": ("openai/gpt-oss-120b",),
            "chat": ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"),
        },
    ),
    Preset(
        kind="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        key_env=("DEEPSEEK_API_KEY",),
        key_page="platform.deepseek.com/api_keys",
        blurb="DeepSeek's models, with the balance shown here.",
        key_check="deepseek",
        json_schema=False,
        suggest={
            "text": ("deepseek-chat",),
            "strong": ("deepseek-chat",),
            "strongest": ("deepseek-reasoner", "deepseek-chat"),
            "long": ("deepseek-chat",),
            "chat": ("deepseek-chat",),
        },
    ),
    Preset(
        kind="together",
        label="Together AI",
        base_url="https://api.together.ai/v1",
        key_env=("TOGETHER_API_KEY",),
        key_page="api.together.ai/settings/api-keys",
        blurb="Open models and search embeddings.",
        json_schema=False,
    ),
    Preset(
        kind="xai",
        label="xAI",
        base_url="https://api.x.ai/v1",
        key_env=("XAI_API_KEY",),
        key_page="console.x.ai",
        blurb="Grok models.",
    ),
    Preset(
        kind="ollama",
        label="Ollama",
        base_url="http://localhost:11434/v1",
        key_env=("OLLAMA_API_KEY",),
        url_env=("OLLAMA_BASE_URL",),
        needs_key=False,
        blurb="Models running on your own machine. Free and private; no key needed.",
        json_schema=False,
        stream_usage=False,
    ),
    Preset(
        kind="custom",
        label="Other (OpenAI-compatible)",
        base_url="",
        key_env=(),
        needs_key=False,
        blurb="Any server that speaks the OpenAI API: LM Studio, vLLM, a LiteLLM proxy, or "
        "your own gateway.",
        json_schema=False,
        stream_usage=False,
        listed=False,
    ),
)

BY_KIND: dict[str, Preset] = {p.kind: p for p in PRESETS}


def preset(kind: str) -> Preset:
    """The preset for `kind`; an unknown kind is a custom server."""
    return BY_KIND.get(kind, BY_KIND["custom"])


def kind_of_url(url: str) -> str:
    """The preset an address belongs to, by its host: the legacy
    `OPENNOTEBOOK_AI_BASE_URL` names no provider, only where it is."""
    u = url.strip().rstrip("/").lower()
    for p in PRESETS:
        if p.base_url and p.kind not in ("ollama", "custom") and u == p.base_url.lower():
            return p.kind
    if "openrouter.ai" in u:
        return "openrouter"
    return "custom"
