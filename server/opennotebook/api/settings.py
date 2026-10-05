"""The studio's settings, and the slide styles."""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from opennotebook.ai.client import ai
from opennotebook.ai.prices import Price, hint
from opennotebook.api.deps import Db, Me
from opennotebook.db.session import release
from opennotebook.domain import settings
from opennotebook.domain.settings import TAB_INFO, Current
from opennotebook.domain.styles import STYLES

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


@router.get("/styles")
async def list_styles(me: Me) -> list[Style]:
    """The slide styles a deck can be built in, the default first."""
    return [Style(id=s.id, label=s.label, blurb=s.blurb) for s in STYLES]
