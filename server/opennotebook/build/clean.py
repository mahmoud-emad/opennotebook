"""Takes the colour decisions back from a model-written slide. A port of
`opennotebook_build/src/clean.rs`.

The prompt tells the model the kit owns every colour, background and
position, but a model does not always listen: one painted a full-slide
`<rect>` with its own near-black gradient over a light kit, and pinned the
figure over the whole slide with an inline `position:absolute`. So before the
kit goes on, `clean` removes what the model was told not to write:

* colour attributes (`fill`, `stroke`, `stop-color`, `color`, `bgcolor`,
  `background`) and opacity, which is how a figure gets faded into a
  backdrop; `fill="none"` stays, it only says "no fill"
* gradients and patterns, which exist only to be painted
* a `<rect>` that covers most of its SVG, which is a background
* every CSS declaration, inline or in a `<style>`, outside the layout
  properties the prompt allows
* emoji, which the kits' fonts cannot draw
* scripts, and stylesheet links: the kit's is the only one

It is a tag scanner, not a parser: what it does not recognise it passes
through untouched.
"""

import re

# The CSS properties a slide may set itself; everything else is the kit's.
LAYOUT = frozenset(
    {
        "display",
        "grid-template-columns",
        "grid-template-rows",
        "grid-template-areas",
        "grid-column",
        "grid-row",
        "grid-area",
        "flex",
        "flex-direction",
        "flex-wrap",
        "flex-grow",
        "flex-shrink",
        "flex-basis",
        "align-items",
        "align-self",
        "align-content",
        "justify-content",
        "justify-items",
        "justify-self",
        "place-items",
        "place-content",
        "place-self",
        "gap",
        "row-gap",
        "column-gap",
        "width",
        "height",
        "min-width",
        "min-height",
        "max-width",
        "max-height",
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "order",
    }
)

COLOUR_ATTRS = frozenset(
    {
        "fill",
        "stroke",
        "stop-color",
        "color",
        "bgcolor",
        "background",
        "opacity",
        "fill-opacity",
        "stroke-opacity",
        "stop-opacity",
    }
)

# Elements dropped whole, contents and all.
DROPPED = frozenset({"lineargradient", "radialgradient", "pattern", "script"})

# A rect covering this much of its SVG in both directions is a background.
BACKGROUND_SHARE = 0.9

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def ascii_lower(s: str) -> str:
    """Lowercase ASCII letters only, so every index into the result is an
    index into `s` too."""
    return s.translate(_ASCII_LOWER)


def without_emoji(text: str) -> str:
    """Text without pictographs: the kits' fonts have none, so an emoji draws
    as an empty box."""
    return "".join(
        c
        for c in text
        if not (
            0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF or ord(c) in (0xFE0F, 0x200D)
        )
    )


def _tag_end(s: str) -> int | None:
    """The index just past the `>` closing the tag at the start of `s`,
    skipping any `>` inside a quoted attribute value."""
    quote: str | None = None
    for i in range(1, len(s)):
        c = s[i]
        if quote is None and c in "\"'":
            quote = c
        elif quote is not None and c == quote:
            quote = None
        elif quote is None and c == ">":
            return i + 1
    return None


def _tag_name(tag: str) -> str:
    """The tag's name lowercased, with a leading `/` for a closing tag."""
    s = tag[1:]
    slash = ""
    if s.startswith("/"):
        slash, s = "/", s[1:]
    m = re.match(r"[A-Za-z0-9\-:!?]*", s)
    return slash + ascii_lower(m.group(0) if m else "")


def _skip_past_close(rest: str, name: str) -> str:
    at = ascii_lower(rest).find(f"</{name}")
    if at < 0:
        return ""
    after = rest[at:]
    end = _tag_end(after)
    return after[len(after) if end is None else end :]


def _attrs(tag: str) -> list[tuple[str, str | None]]:
    """The tag's attributes in order, values unquoted. A bare attribute has
    no value."""
    inner = tag.lstrip("<").rstrip(">").rstrip("/")
    i, n = 0, len(inner)
    # Skip the tag name.
    while i < n and not inner[i].isspace():
        i += 1
    out: list[tuple[str, str | None]] = []
    while True:
        while i < n and inner[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not inner[i].isspace() and inner[i] != "=":
            i += 1
        name = inner[start:i]
        while i < n and inner[i].isspace():
            i += 1
        if i >= n or inner[i] != "=":
            out.append((name, None))
            continue
        i += 1
        while i < n and inner[i].isspace():
            i += 1
        if i >= n:
            out.append((name, ""))
            break
        if inner[i] in "\"'":
            q = inner[i]
            close = inner.find(q, i + 1)
            to = n if close < 0 else close
            out.append((name, inner[i + 1 : to]))
            i = n if close < 0 else close + 1
        else:
            start = i
            while i < n and not inner[i].isspace():
                i += 1
            out.append((name, inner[start:i]))
    return out


def _attr(tag: str, name: str) -> str | None:
    for n, v in _attrs(tag):
        if ascii_lower(n) == ascii_lower(name):
            return v or ""
    return None


def _view_size(view_box: str) -> tuple[float, float] | None:
    nums: list[float] = []
    for p in re.split(r"[\s,]+", view_box.strip()):
        try:
            nums.append(float(p))
        except ValueError:
            continue
    if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
        return nums[2], nums[3]
    return None


def _is_background(tag: str, view: tuple[float, float] | None) -> bool:
    def covers(name: str, full: float | None) -> bool:
        v = _attr(tag, name)
        if v is None:
            return False
        v = v.strip()
        if v.endswith("%"):
            try:
                return float(v[:-1].strip()) >= BACKGROUND_SHARE * 100
            except ValueError:
                return False
        try:
            x = float(v.removesuffix("px"))
        except ValueError:
            return False
        return full is not None and x >= BACKGROUND_SHARE * full

    return covers("width", view[0] if view else None) and covers(
        "height", view[1] if view else None
    )


def layout_only(decls: str) -> str:
    """A declaration list with only the layout properties left."""
    out: list[str] = []
    for d in decls.split(";"):
        prop, sep, value = d.partition(":")
        if not sep:
            continue
        prop = ascii_lower(prop.strip())
        if prop in LAYOUT:
            out.append(f"{prop}:{value.strip()}")
    return ";".join(out)


def layout_only_sheet(css: str) -> str:
    """A stylesheet with every rule body reduced to its layout properties; a
    rule left empty is dropped. `@import` and `@font-face` go entirely."""
    out: list[str] = []
    rest = css
    while (opened := rest.find("{")) >= 0:
        selector = rest[:opened].strip()
        after = rest[opened + 1 :]
        # A block holding blocks (@media, @supports): keep its wrapper and
        # clean what is inside.
        inner_open = after.find("{")
        close = after.find("}")
        if close < 0:
            close = len(after)
        if 0 <= inner_open < close:
            depth, end = 1, len(after)
            for i, c in enumerate(after):
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            body = layout_only_sheet(after[:end])
            if body.strip() and not ascii_lower(selector).startswith("@font-face"):
                out.append(f"{selector}{{{body}}}")
            rest = after[end + 1 :]
            continue
        body = layout_only(after[:close])
        selector = selector.rsplit(";", 1)[-1].strip()
        if body and not selector.startswith("@"):
            out.append(f"{selector}{{{body}}}")
        rest = after[close + 1 :]
    return "".join(out)


def _clean_tag(tag: str, name: str) -> str:
    if name.startswith(("/", "!", "?")):
        return tag
    kept: list[str] = []
    for n, v in _attrs(tag):
        lower = ascii_lower(n)
        if lower in COLOUR_ATTRS:
            if lower == "fill" and v is not None and v.strip() == "none":
                kept.append('fill="none"')
            continue
        if lower == "style" and v is not None:
            v = layout_only(v)
            if v:
                kept.append('style="' + v.replace('"', "'") + '"')
        elif v is None:
            kept.append(n)
        else:
            kept.append(f'{n}="' + v.replace('"', "&quot;") + '"')
    close = "/>" if tag.endswith("/>") else ">"
    m = re.match(r"[^\s>/]*", tag[1:])
    head = (m.group(0) if m else "") or name
    if not kept:
        return f"<{head}{close}"
    return f"<{head} {' '.join(kept)}{close}"


def clean(html: str) -> str:
    out: list[str] = []
    # The viewBox of the SVG being read, as width and height.
    view: tuple[float, float] | None = None
    rest = html
    while (at := rest.find("<")) >= 0:
        out.append(without_emoji(rest[:at]))
        rest = rest[at:]
        if rest.startswith("<!--"):
            e = rest.find("-->")
            end = len(rest) if e < 0 else e + 3
            out.append(rest[:end])
            rest = rest[end:]
            continue
        end = _tag_end(rest)
        if end is None:
            break
        tag, rest = rest[:end], rest[end:]
        name = _tag_name(tag)
        if name in DROPPED:
            if not tag.endswith("/>"):
                rest = _skip_past_close(rest, name)
            continue
        if name == "link":
            # The kit brings the only stylesheet a slide loads.
            continue
        if name == "svg":
            vb = _attr(tag, "viewbox")
            view = (_view_size(vb) if vb is not None else None) or view
        elif name == "/svg":
            view = None
        elif name == "rect" and _is_background(tag, view):
            if not tag.endswith("/>"):
                rest = _skip_past_close(rest, "rect")
            continue
        elif name == "style" and _attr(tag, "data-kit") is None:
            out.append(tag)
            close = ascii_lower(rest).find("</style")
            if close < 0:
                close = len(rest)
            out.append(layout_only_sheet(rest[:close]))
            rest = rest[close:]
            continue
        out.append(_clean_tag(tag, name))
    out.append(without_emoji(rest))
    return "".join(out)
