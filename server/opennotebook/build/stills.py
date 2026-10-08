"""The pictures of a slides video: each chapter's slide, or a title card
where it has none, drawn by a browser as a PNG at the video's size. A
slide is an HTML document, so a browser is what draws it.
"""

import html
import logging
import os
from pathlib import Path

from playwright.async_api import Browser, Playwright, async_playwright

from opennotebook.build import slides
from opennotebook.build import timeline as tl
from opennotebook.build.encode import HEIGHT, WIDTH, ToolMissing
from opennotebook.db.models import Session
from opennotebook.domain.sessions import Part

log = logging.getLogger(__name__)

# A Playwright channel, such as `chrome` for the installed Google Chrome.
# Empty tries Playwright's own Chromium, then Chrome.
BROWSER_KEY = "OPENNOTEBOOK_BROWSER_CHANNEL"
# Seconds a slide may take to draw, fonts included, before it is taken as
# it is.
SLIDE_LOAD_S = 15

# A picture to show: its media type (`text/html` or `image/png`) and body.
Page = tuple[str, bytes]


def card(title: str, chapter: str, place: str, speakers: list[str]) -> str:
    """A title card for a part that has no slide: an audio overview's
    chapter, or a slide whose file is missing. A video never fails for one
    picture."""
    esc = html.escape
    who = esc(" · ".join(speakers))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:{WIDTH}px;height:{HEIGHT}px;background:#f7f5ef;color:#1f2937;
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{position:absolute;inset:120px 160px;display:flex;flex-direction:column;
justify-content:center}}
.title{{font-size:40px;color:#6b7280;letter-spacing:.02em;margin:0 0 28px}}
h1{{font-size:96px;line-height:1.08;margin:0 0 40px;font-weight:700}}
.place{{font-size:34px;color:#2563eb;font-weight:600;margin:0}}
.who{{position:absolute;left:160px;bottom:90px;font-size:30px;color:#6b7280}}
</style></head><body><main><p class="title">{esc(title)}</p><h1>{esc(chapter)}</h1>
<p class="place">{esc(place)}</p></main><div class="who">{who}</div></body></html>"""


async def launch(pw: Playwright) -> Browser:
    """The browser set by `BROWSER_KEY`, or the first that starts."""
    channel = os.environ.get(BROWSER_KEY, "").strip()
    tried: list[str] = []
    for ch in [channel] if channel else ["", "chrome"]:
        try:
            return await pw.chromium.launch(channel=ch or None)
        except Exception as e:
            tried.append(f"{ch or 'chromium'}: {str(e).splitlines()[0]}")
    raise ToolMissing(
        "a browser",
        "The studio cannot draw the slides for a video: it has no browser. Run `uv run "
        f"playwright install chromium` on its server, or set {BROWSER_KEY}=chrome to use "
        "Google Chrome, then try again.",
    ) from RuntimeError("; ".join(tried))


async def draw_stills(pages: list[Page], into: Path) -> list[Path]:
    """Each page as a PNG at the video's size: an HTML document drawn by the
    browser, a PNG taken as it is."""
    out: list[Path] = []
    async with async_playwright() as pw:
        browser = await launch(pw)
        try:
            page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            for i, (kind, body) in enumerate(pages):
                path = into / f"still-{i:03d}.png"
                if kind == "image/png":
                    path.write_bytes(body)
                else:
                    try:
                        await page.set_content(
                            body.decode("utf-8", "replace"),
                            wait_until="networkidle",
                            timeout=SLIDE_LOAD_S * 1000,
                        )
                    except Exception:
                        # Fonts from the web that never arrive: the slide is
                        # drawn with what loaded.
                        log.warning("slide %d did not settle; drawing it as it is", i)
                    await page.screenshot(path=str(path), type="png")
                out.append(path)
        finally:
            await browser.close()
    return out


def pages_of(o: Session, parts: list[Part], chapters: list[tl.Chapter]) -> list[Page]:
    """What is on screen for each chapter: its slide, or a title card."""
    by_ordinal = {p.ordinal: p for p in parts}
    speakers = [str(sp.get("display_name", "")) for sp in o.speakers if isinstance(sp, dict)]
    out: list[Page] = []
    for i, ch in enumerate(chapters):
        part = by_ordinal[ch.ordinal]
        found = None
        if o.kind == "slides":
            found = slides.render_of(
                {
                    "collection": part.collection,
                    "presentation": part.presentation,
                    "slide": part.slide,
                }
            )
        if found is not None and found[1]:
            out.append(found)
            continue
        place = f"Chapter {i + 1} of {len(chapters)}"
        out.append(("text/html", card(o.title, ch.title or o.title, place, speakers).encode()))
    return out
