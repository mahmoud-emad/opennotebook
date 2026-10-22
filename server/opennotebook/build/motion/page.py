"""The page a motion video is recorded from: what the studio injects into
every page before it runs, and the fallback page for when no page could be
written (`docs/motion-style.md`).

A motion video's picture is one HTML page animated against the narration.
The page is written by a model under `write.SYSTEM`, and the contract it is
held to is fixed and small: the studio splices a prelude in just before its
`</head>`, which gives the page everything it needs and takes everything it
must not do:

- `window.__VIDEO` — the video's title, length and size (1920×1080);
- `window.__SCENES` — the plan's scenes, each with its span and the
  manifest the writer declared: what appears in it, on which word, and what
  it asserts;
- `window.__CHAPTERS` — the narration's chapters, from the same timeline
  the episode's audio was joined on;
- `window.__BEATS` — every word of the narration and when it is said;
- `window.__Prelude` — the frame clock: `onFrame(fn)` registers a callback,
  `at(ms)` runs it at an exact time. The recorder calls `at` once per
  frame, so the animation is stepped on the timeline rather than by the
  browser's own clock: frames land where the words are, at whatever rate
  the machine draws.

The page declares what it puts on screen with `data-mn="<kind>:<id>"` on
each visible thing. The lint measures those boxes in the recorded page —
the browser is the layout measurer here where the whiteboard's compiler is
for a board.

Nothing else is fixed: the design, the motion, and how scenes come and go
are the writer's own.
"""

import html
import json
import re
from typing import Any

from opennotebook.build import timeline as tl
from opennotebook.build.encode import HEIGHT, WIDTH

# The viewport every page is recorded at.
SIZE = (WIDTH, HEIGHT)

# The clock, spliced into every page just before `</head>` (at its very top
# when it has none, which puts a script before the doctype but still runs
# it). The page's own script runs after it and may register onFrame
# callbacks; nothing in here fetches anything.
RUNNER = """<script>
(function () {
  var listeners = [];
  var clockedAt = 0;
  window.__done = false;
  window.__Prelude = {
    now: function () { return clockedAt; },
    onFrame: function (fn) {
      listeners.push(fn);
      window.__Prelude.registered = listeners.length;
      return fn;
    },
    // How many callbacks the page registered, and the first one that threw
    // (kept, not rethrown: the recorder reads both after each scene).
    registered: 0,
    error: null,
    at: function (ms) {
      clockedAt = ms;
      window.__Prelude.registered = listeners.length;
      for (var i = 0; i < listeners.length; i++) {
        try { listeners[i](ms); }
        catch (e) {
          if (window.__Prelude.error === null)
            window.__Prelude.error = (e && e.message) ? e.message : String(e);
        }
      }
    }
  };
})();
</script>"""


def scenes_json(timing: tl.Timeline, scenes: list[dict[str, Any]]) -> str:
    """The plan's scenes with their windows and the manifest merged in, as
    the page reads them. `scenes` is `scene.manifest`'s own output, each
    entry already carrying `start_ms` and `end_ms`."""
    return json.dumps(scenes, ensure_ascii=False)


def chapters_json(timing: tl.Timeline) -> str:
    """The narration's chapters: id, title and span, from the timeline the
    episode's audio was joined on."""
    return json.dumps(
        [
            {"i": i, "title": c.title, "start_ms": c.start_ms, "end_ms": c.end_ms}
            for i, c in enumerate(timing.chapters)
        ],
        ensure_ascii=False,
    )


def beats_json(timing: tl.Timeline) -> str:
    """Every word of the narration and when it is said: line, part, word
    index, text, start and end."""
    return json.dumps(
        [
            {
                "line": sp.line_id,
                "part": sp.part_ordinal,
                "w": i,
                "text": w.text,
                "start_ms": w.start_ms,
                "end_ms": w.end_ms,
            }
            for sp in timing.lines
            for i, w in enumerate(sp.words)
        ],
        ensure_ascii=False,
    )


def video_json(timing: tl.Timeline, title: str) -> str:
    return json.dumps(
        {"title": title, "duration_ms": timing.total_ms, "width": WIDTH, "height": HEIGHT},
        ensure_ascii=False,
    )


def inject(html_text: str, timing: tl.Timeline, title: str, scenes: list[dict[str, Any]]) -> str:
    """The page with the studio's timings and the frame clock spliced in
    right after its `<head>` opens: before the page's own scripts, wherever
    they are, so `window.__Prelude` exists when the page first runs."""
    blob = (
        f"<script>window.__VIDEO = {video_json(timing, title)};"
        f"window.__CHAPTERS = {chapters_json(timing)};"
        f"window.__SCENES = {scenes_json(timing, scenes)};"
        f"window.__BEATS = {beats_json(timing)};</script>{RUNNER}"
    )
    open_head = re.search(r"<head[^>]*>", html_text, re.IGNORECASE)
    if open_head is not None:
        at = open_head.end()
        return html_text[:at] + blob + html_text[at:]
    if "</head>" in html_text:
        return html_text.replace("</head>", blob + "</head>", 1)
    return blob + html_text


def fallback_page(timing: tl.Timeline, title: str, scenes: list[dict[str, Any]]) -> str:
    """The plain page shown when no page could be written: the title, then
    each scene's name as its span comes on screen. A video whose writer
    failed at every try still makes one that is true, like the whiteboard's
    plain scenes do."""
    esc = html.escape
    rows = "".join(
        f'<p class="c" data-start="{s["start_ms"]}">{esc(str(s.get("title") or ""))}</p>'
        for s in scenes
    )
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:{WIDTH}px;height:{HEIGHT}px;background:#1e293b;color:#e2e8f0;
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{position:absolute;inset:120px 160px;display:flex;flex-direction:column;
justify-content:center}}
h1{{font-size:96px;line-height:1.08;margin:0 0 48px;font-weight:700}}
.c{{font-size:52px;color:#94a3b8;margin:0 0 28px;opacity:0}}
</style></head><body><main><h1>{esc(title)}</h1>{rows}</main></body></html>"""
    script = """<script>
(function () {
  var rows = document.querySelectorAll(".c");
  window.__Prelude.onFrame(function (t) {
    for (var i = 0; i < rows.length; i++)
      rows[i].style.opacity = t >= Number(rows[i].dataset.start) ? 1 : 0;
    if (t >= window.__VIDEO.duration_ms) window.__done = true;
  });
})();
</script></head>"""
    return doc.replace("</head>", script, 1)
