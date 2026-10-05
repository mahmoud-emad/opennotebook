# ruff: noqa: E501
"""The style kits slides are drawn with. A port of
`opennotebook_build/src/kits.rs`.

Each style ships its fonts, its colours, its components and the way its
figures are drawn, and `apply` adds all of it to every slide after the model
writes it — the approach tools that keep a deck on-style take (Gamma's themes,
v0 and Figma Make's component kits): the model writes content and layout with
the kit's classes, so slides cannot drift from the style or from each other,
and text colour is never the model's choice.

The eight styles are NotebookLM's infographic styles, chosen in Slide Lab (the
comparison page) as the ones that read best as slides. Every palette holds
text, secondary text and the accent at 4.5:1 on the ground and on a panel; the
tests keep it that way.
"""

from dataclasses import dataclass

from opennotebook.build.clean import ascii_lower, clean


@dataclass(frozen=True)
class Kit:
    id: str
    # bg, text, muted, accent, panel, line.
    palette: tuple[str, str, str, str, str, str]
    # Three illustration colours, never used for text.
    ill: tuple[str, str, str]
    fonts: str
    display: str
    body: str
    # A background layer under everything (dotted paper and the like).
    under: str | None
    # A decoration over the ground (a frame, a top rule).
    after: str | None
    css: str
    # How this style's figures are drawn, for the prompt.
    recipe: str
    # A small figure in the style, for the prompt.
    example: str
    # What the style's layouts should be, for the prompt.
    layout_rule: str


KITS: tuple[Kit, ...] = (
    Kit(
        id="sketchnote",
        palette=("#fdfcf8", "#1d1d1d", "#4a4a4a", "#c62828", "#ffffff", "#d6d3c9"),
        ill=("#1e66c9", "#ffd43b", "#2f9e44"),
        fonts=r"""https://fonts.googleapis.com/css2?family=Caveat:wght@600;700&family=Patrick+Hand&display=swap""",
        display=r"""'Caveat', 'Comic Sans MS', cursive""",
        body=r"""'Patrick Hand', 'Comic Sans MS', cursive""",
        under=r"""radial-gradient(circle, #cfcabd 1.7px, transparent 2.2px) 0 0/36px 36px""",
        after=None,
        css=r"""
html body{font-size:34px}
.k-title{font-weight:700;font-size:104px;line-height:.95}
.k-kicker{font-family:var(--k-display);font-size:38px;text-transform:none;letter-spacing:0;color:var(--k-accent);transform:rotate(-1.5deg);align-self:flex-start}
.k-card{background:var(--k-panel);position:relative}
.k-card::before{content:"";position:absolute;inset:0;border:3px solid var(--k-text);border-radius:14px 22px 12px 20px;filter:url(#k-wobble);pointer-events:none}
.k-points li::before{content:"✓";left:0;color:var(--k-i3);font-weight:700}
.k-hl{box-shadow:inset 0 -.5em 0 color-mix(in srgb,var(--k-i2) 70%,transparent)}
.k-figure svg .k-ink{stroke-width:5;filter:url(#k-wobble)}
.k-figure svg [class*="k-f"]{filter:url(#k-wobble)}
.k-figure svg .k-lab{font-family:var(--k-body);font-size:22px}""",
        recipe=r"""Sketch notes: doodled icons and diagrams in k-ink marker lines with a few flat colour fills (k-f2 blue, k-f3 yellow, k-f4 green, k-f1 red for one emphasis), a banner or ribbon shape behind one k-lab heading, boxes, arrows (head as a short V path) and small icons for each idea. Loose, friendly, hand-made; the kit adds the wobble.""",
        example=r"""<svg viewBox="0 0 800 600"><path class="k-f3" d="M120 90 H470 L440 130 L470 170 H120 L150 130Z"/><text class="k-lab" x="295" y="142" text-anchor="middle">the idea</text><rect class="k-ink" x="140" y="260" width="200" height="140" rx="14"/><circle class="k-f2" cx="240" cy="330" r="38"/><path class="k-ink" d="M360 330 C420 300 470 300 520 330 M498 312 L522 332 L496 346"/><circle class="k-f4" cx="610" cy="335" r="70"/><path class="k-ink" d="M580 335 L602 357 L645 308"/></svg>""",
        layout_rule=r"""Mix k-split, k-row cards and k-steps, and give every slide a doodled figure.""",
    ),
    Kit(
        id="professional",
        palette=("#ffffff", "#13213c", "#4a5568", "#1f4e8c", "#f3f6fa", "#d5dde8"),
        ill=("#6b8fbf", "#a9bcd6", "#13213c"),
        fonts=r"""https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;800&display=swap""",
        display=r"""'Public Sans', 'Helvetica Neue', Arial, sans-serif""",
        body=r"""'Public Sans', 'Helvetica Neue', Arial, sans-serif""",
        under=None,
        after=r"""html body::after{content:"";position:absolute;left:80px;right:80px;top:0;height:10px;background:var(--k-accent);pointer-events:none;z-index:0}""",
        css=r"""
.k-title{font-weight:800;font-size:84px;letter-spacing:-.02em}
.k-kicker{color:var(--k-muted);letter-spacing:.16em}
.k-card{border-radius:6px;border:1px solid var(--k-line);background:var(--k-panel)}
.k-card h3{font-size:32px}
.k-stat .k-num{font-size:110px;letter-spacing:-.03em}
.k-points li::before{content:"";width:12px;height:12px;background:var(--k-accent);top:.6em}
.k-figure svg .k-ink{stroke-width:3}
.k-figure svg .k-lab{font-weight:600;font-size:18px}""",
        recipe=r"""Business graphics: a clean chart (bars, a stacked bar, a line or a donut) or a tidy flow of labelled boxes, drawn in k-f1 (blue), k-f2, k-f3 and k-f4 (navy), with thin k-ink axes and gridlines and k-lab value labels. Precise, aligned, no decoration.""",
        example=r"""<svg viewBox="0 0 800 600"><path class="k-ink" d="M100 500 H720 M100 500 V110"/><rect class="k-f3" x="150" y="380" width="90" height="120"/><rect class="k-f2" x="290" y="300" width="90" height="200"/><rect class="k-f1" x="430" y="200" width="90" height="300"/><rect class="k-f4" x="570" y="150" width="90" height="350"/><text class="k-lab" x="615" y="135" text-anchor="middle">+42%</text></svg>""",
        layout_rule=r"""Use a chart figure on at least two slides and k-row cards on another.""",
    ),
    Kit(
        id="bento",
        palette=("#f2f2f0", "#161616", "#555555", "#c2410c", "#ffffff", "#e3e3e0"),
        ill=("#ffb84d", "#7cc6fe", "#c3f0a6"),
        fonts=r"""https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=swap""",
        display=r"""'Outfit', 'Helvetica Neue', Arial, sans-serif""",
        body=r"""'Outfit', 'Helvetica Neue', Arial, sans-serif""",
        under=None,
        after=None,
        css=r"""
.k-title{font-weight:800;font-size:80px;letter-spacing:-.02em}
.k-card,.k-tile{border-radius:30px}
.k-tile:nth-child(4n+2){background:color-mix(in srgb,var(--k-i1) 35%,var(--k-panel))}
.k-tile:nth-child(4n+3){background:color-mix(in srgb,var(--k-i2) 35%,var(--k-panel))}
.k-tile:nth-child(4n+4){background:color-mix(in srgb,var(--k-i3) 40%,var(--k-panel))}
.k-points li::before{content:"";width:14px;height:14px;border-radius:50%;background:var(--k-accent);top:.55em}""",
        recipe=r"""Put the content in a k-bento grid: 4–7 k-tile elements of mixed sizes (k-tile-wide spans two columns, k-tile-tall two rows), each holding one fact — an h3 and one short line, a k-stat, or a small inline svg icon (k-f1..k-f4 shapes, 6–10 elements). The grid itself is the illustration; use a k-figure only inside a tile.""",
        example=r"""<svg viewBox="0 0 200 160"><rect class="k-f2" x="20" y="40" width="70" height="90" rx="14"/><circle class="k-f1" cx="140" cy="70" r="34"/><rect class="k-f4" x="105" y="115" width="75" height="18" rx="9"/></svg>""",
        layout_rule=r"""Use k-bento on most content slides, with a different arrangement of tile sizes on each.""",
    ),
    Kit(
        id="editorial",
        palette=("#f7f6f2", "#1f1f1c", "#5c5b55", "#8a3324", "#ffffff", "#d9d6ce"),
        ill=("#8a3324", "#5c5b55", "#d9d6ce"),
        fonts=r"""https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Source+Sans+3:wght@400;600&display=swap""",
        display=r"""'Fraunces', Georgia, serif""",
        body=r"""'Source Sans 3', 'Helvetica Neue', Arial, sans-serif""",
        under=None,
        after=None,
        css=r"""
.k-title{font-weight:600;font-size:100px;letter-spacing:-.02em}
.k-kicker{color:var(--k-text);border-bottom:2px solid var(--k-text);padding-bottom:12px;align-self:flex-start}
.k-card{background:transparent;border-top:3px solid var(--k-text);border-radius:0;padding:26px 0 0}
.k-points li::before{content:"—";color:var(--k-accent);left:0}
.k-stat .k-num{font-family:var(--k-display);font-weight:500}
.k-figure svg .k-ink{stroke-width:3}""",
        recipe=r"""No pictorial illustration. The figure is a quiet line diagram: thin k-ink strokes (rules, a timeline, boxes and arrows), k-lab labels, and at most one small k-f1 accent shape. Most of the slide is whitespace and large type.""",
        example=r"""<svg viewBox="0 0 800 520"><path class="k-ink" d="M60 260 H740"/><circle class="k-f1" cx="220" cy="260" r="14"/><circle class="k-ink" cx="420" cy="260" r="14"/><circle class="k-ink" cx="620" cy="260" r="14"/><text class="k-lab" x="220" y="320" text-anchor="middle">fork()</text><text class="k-lab" x="420" y="320" text-anchor="middle">exec()</text><text class="k-lab" x="620" y="320" text-anchor="middle">exit()</text></svg>""",
        layout_rule=r"""Vary the layout: use k-row cards on at least one slide, k-steps or k-bento on another, and k-fig-wide on another.""",
    ),
    Kit(
        id="instructional",
        palette=("#fbfaf7", "#1d1d1b", "#57564f", "#2b6cb0", "#ffffff", "#d9d6cc"),
        ill=("#f6ad55", "#68d391", "#90cdf4"),
        fonts=r"""https://fonts.googleapis.com/css2?family=Lexend:wght@400;600;800&display=swap""",
        display=r"""'Lexend', 'Helvetica Neue', Arial, sans-serif""",
        body=r"""'Lexend', 'Helvetica Neue', Arial, sans-serif""",
        under=None,
        after=None,
        css=r"""
html body{font-size:28px}
.k-title{font-weight:800;font-size:80px;letter-spacing:-.015em}
.k-card,.k-step{border-radius:20px;border:2px solid var(--k-line)}
.k-step .k-n{background:var(--k-accent);color:var(--k-panel)}
.k-points li::before{content:counter(k-item);counter-increment:k-item;font-weight:800;color:var(--k-accent);left:0}
.k-points{counter-reset:k-item}""",
        recipe=r"""A storyboard: a k-steps row of 3–5 k-step panels, in order, each with a k-n number, a small inline svg pictogram (k-f1..k-f4 shapes and k-ink, 5–10 elements, viewBox 0 0 200 140), an h3 of two to four words and one short line. The kit draws the arrows between steps.""",
        example=r"""<svg viewBox="0 0 200 140"><rect class="k-f3" x="30" y="40" width="80" height="70" rx="12"/><path class="k-ink" d="M120 75 H165 M152 63 L166 75 L152 87"/><circle class="k-f1" cx="70" cy="75" r="18"/></svg>""",
        layout_rule=r"""Use k-steps on most content slides; one slide may be a k-split with a single figure.""",
    ),
    Kit(
        id="scientific",
        palette=("#fcfcfa", "#1b2a33", "#4b5d68", "#1f6f78", "#ffffff", "#c9d3d6"),
        ill=("#7fb7be", "#b5c99a", "#d9c5a0"),
        fonts=r"""https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:wght@500;700&family=IBM+Plex+Sans:wght@400;600&display=swap""",
        display=r"""'IBM Plex Serif', Georgia, serif""",
        body=r"""'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif""",
        under=None,
        after=None,
        css=r"""
html body{font-size:28px}
.k-title{font-weight:700;font-size:80px}
.k-kicker{color:var(--k-muted)}
.k-card{border-radius:4px;border:1px solid var(--k-line)}
.k-figure{background:linear-gradient(var(--k-line) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(90deg,var(--k-line) 1px,transparent 1px) 0 0/40px 40px;background-color:var(--k-panel);border:1px solid var(--k-line);padding:20px;align-content:center}
.k-figure svg .k-ink{stroke-width:2.5}
.k-figure svg [class*="k-f"]{stroke:var(--k-text);stroke-width:2;fill-opacity:.85}
.k-figure svg .k-lab{font-weight:400;font-size:18px}
.k-points li::before{content:"▸";left:0;color:var(--k-accent)}""",
        recipe=r"""A textbook diagram: the subject drawn precisely in thin lines (a cross-section, a cycle, a structure, a labelled apparatus or a plotted curve), parts filled lightly with k-f2..k-f4, every important part labelled with a k-lab at the end of a straight k-ink leader line. Add a <figcaption class="k-figcap"> inside the k-figure: "Fig. N — what it shows".""",
        example=r"""<svg viewBox="0 0 800 560"><circle class="k-f2" cx="330" cy="280" r="170"/><circle class="k-f3" cx="330" cy="280" r="70"/><path class="k-ink" d="M380 230 L560 140"/><text class="k-lab" x="570" y="146">nucleus</text><path class="k-ink" d="M470 360 L600 420"/><text class="k-lab" x="610" y="428">membrane</text></svg>""",
        layout_rule=r"""Every content slide has a labelled k-figure with a k-figcap; use k-fig-wide on at least two.""",
    ),
    Kit(
        id="clay",
        palette=("#f4eee8", "#2b2321", "#5e524d", "#b5441f", "#fffaf5", "#e2d6cc"),
        ill=("#f2a65a", "#7fb7a4", "#9ec5f8"),
        fonts=r"""https://fonts.googleapis.com/css2?family=Baloo+2:wght@600;800&family=Nunito:wght@400;700&display=swap""",
        display=r"""'Baloo 2', 'Arial Rounded MT Bold', sans-serif""",
        body=r"""'Nunito', Arial, sans-serif""",
        under=None,
        after=None,
        css=r"""
.k-title{font-weight:800;font-size:92px;line-height:.98}
.k-card{border-radius:34px;box-shadow:inset 0 -8px 0 rgba(0,0,0,.05),0 14px 26px rgba(80,50,30,.14)}
.k-points li::before{content:"";width:20px;height:20px;border-radius:50%;background:var(--k-accent);box-shadow:inset -4px -4px 0 rgba(0,0,0,.18);top:.4em}
.k-figure svg [class*="k-f"]{filter:url(#k-clay)}""",
        recipe=r"""Claymation: 4–8 chunky, rounded, slightly lumpy shapes (blobby paths, fat rounded rects, spheres) in k-f1..k-f4 and k-f0, as if modelled from plasticine and stood on a ground shape; no thin parts, no outlines, no small details. The kit adds the matte clay highlight and soft shadow.""",
        example=r"""<svg viewBox="0 0 800 600"><path class="k-f3" d="M80 470 C200 430 600 430 720 470 C730 520 70 520 80 470Z"/><rect class="k-f2" x="260" y="230" width="260" height="210" rx="70"/><circle class="k-f1" cx="390" cy="200" r="78"/><path class="k-f4" d="M560 330 C620 300 680 340 660 400 C640 450 560 440 550 400 C545 370 540 345 560 330Z"/></svg>""",
        layout_rule=r"""Vary the layout: use k-row cards on at least one slide, k-steps or k-bento on another, and k-fig-wide on another.""",
    ),
    Kit(
        id="bricks",
        palette=("#f5f5f2", "#1a1a1a", "#4d4d4d", "#c40d0f", "#ffffff", "#dadada"),
        ill=("#0055bf", "#f2cd37", "#237841"),
        fonts=r"""https://fonts.googleapis.com/css2?family=Rubik:wght@500;800&display=swap""",
        display=r"""'Rubik', 'Arial Black', sans-serif""",
        body=r"""'Rubik', Arial, sans-serif""",
        under=None,
        after=None,
        css=r"""
.k-title{font-weight:800;font-size:90px;letter-spacing:-.01em}
.k-card{border-radius:10px;border:3px solid var(--k-line)}
.k-points li::before{content:"";width:22px;height:14px;background:var(--k-accent);border-radius:3px;box-shadow:inset 0 -3px 0 rgba(0,0,0,.25);top:.55em}
.k-figure svg [class*="k-f"]{filter:url(#k-brick)}""",
        recipe=r"""A toy-brick build: the subject assembled from bricks — rects whose width is a multiple of 40 and height 48 — stacked with staggered joints, in k-f1 (red), k-f2 (blue), k-f3 (yellow), k-f4 (green) and k-f0 (white). Every brick top that is not covered gets studs: small rects of the same class, 24 wide, 12 tall, rx 3, one every 40px starting 8px in, sitting directly on top. The kit adds the plastic sheen.""",
        example=r"""<svg viewBox="0 0 800 600"><rect class="k-f2" x="240" y="440" width="320" height="48"/><rect class="k-f3" x="280" y="392" width="240" height="48"/><rect class="k-f1" x="320" y="344" width="160" height="48"/><rect class="k-f1" x="328" y="332" width="24" height="12" rx="3"/><rect class="k-f1" x="368" y="332" width="24" height="12" rx="3"/><rect class="k-f1" x="408" y="332" width="24" height="12" rx="3"/><rect class="k-f1" x="448" y="332" width="24" height="12" rx="3"/><rect class="k-f4" x="560" y="440" width="80" height="48"/><rect class="k-f4" x="568" y="428" width="24" height="12" rx="3"/><rect class="k-f4" x="608" y="428" width="24" height="12" rx="3"/></svg>""",
        layout_rule=r"""Vary the layout: use k-row cards on at least one slide, k-steps or k-bento on another, and k-fig-wide on another.""",
    ),
)


def structure(k: Kit) -> str:
    """The slide skeleton the prompt shows for a style: its signature layout,
    so a model that skims the rules still sees the shape the style is made
    of."""
    if k.id == "bento":
        return r"""<body>
  <div class="k-kicker">…</div>
  <h1 class="k-title">…</h1>
  <div class="k-bento">
    <div class="k-tile k-tile-wide k-tile-tall"><svg viewBox="0 0 400 300">…</svg><h3>…</h3><p>…</p></div>
    <div class="k-tile"><div class="k-num">…</div><p>…</p></div>
    <div class="k-tile"><h3>…</h3><p>…</p></div>
    <div class="k-tile k-tile-wide"><h3>…</h3><p>…</p></div>
  </div>
</body>"""
    if k.id == "instructional":
        return r"""<body>
  <div class="k-kicker">…</div>
  <h1 class="k-title">…</h1>
  <div class="k-steps">
    <div class="k-step"><div class="k-n">1</div><svg viewBox="0 0 200 140">…</svg><h3>…</h3><p>…</p></div>
    <div class="k-step"><div class="k-n">2</div><svg viewBox="0 0 200 140">…</svg><h3>…</h3><p>…</p></div>
    <div class="k-step"><div class="k-n">3</div><svg viewBox="0 0 200 140">…</svg><h3>…</h3><p>…</p></div>
  </div>
</body>"""
    return r"""<body>
  <div class="k-kicker">…</div>
  <h1 class="k-title">…</h1>
  <div class="k-split">
    <div class="k-stack"><ul class="k-points"><li>…</li><li>…</li><li>…</li></ul><div class="k-card k-stat"><div class="k-num">…</div><div class="k-cap">…</div></div></div>
    <figure class="k-figure"><svg viewBox="0 0 800 600">…</svg></figure>
  </div>
</body>"""


def kit(id: str) -> Kit | None:
    """The kit for a style id; None for a style the studio does not ship."""
    return next((k for k in KITS if k.id == id), None)


# Everything every kit shares: the slide box, the components, and the classes
# figures are drawn with. A kit's own `css` follows and refines it.
BASE = r"""
:root{--k-bg:@BG@;--k-text:@TEXT@;--k-muted:@MUTED@;--k-accent:@ACCENT@;--k-panel:@PANEL@;--k-line:@LINE@;--k-i1:@I1@;--k-i2:@I2@;--k-i3:@I3@;--k-display:@DISPLAY@;--k-body:@BODY@}
html,html body{margin:0;width:1920px;height:1080px;overflow:hidden}
html body{box-sizing:border-box;padding:80px;display:flex;flex-direction:column;gap:34px;background:var(--k-bg);color:var(--k-text);font-family:var(--k-body);font-size:30px;line-height:1.42;position:relative}
@UNDER@
@AFTER@
html body>*{position:relative;z-index:1}
html body *{box-sizing:border-box}
.k-kicker{font-family:var(--k-body);font-weight:700;font-size:22px;letter-spacing:.14em;text-transform:uppercase;color:var(--k-accent)}
.k-title{font-family:var(--k-display);line-height:1.02;margin:0;color:var(--k-text);text-wrap:balance}
.k-sub{font-size:38px;color:var(--k-muted);margin:0;max-width:30ch}
.k-split{display:grid;grid-template-columns:1fr 1fr;gap:72px;align-items:center;flex:1;min-height:0}
.k-split.k-fig-wide{grid-template-columns:5fr 7fr}
.k-row{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:32px}
.k-stack{display:flex;flex-direction:column;gap:26px;min-width:0}
.k-center{flex:1;display:grid;place-items:center;text-align:center;min-height:0}
.k-center .k-stack{align-items:center}
.k-points{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:22px}
.k-points li{position:relative;padding-left:46px}
.k-points li::before{position:absolute;left:4px;top:.1em}
.k-card{background:var(--k-panel);color:var(--k-text);padding:34px 38px;border-radius:16px;min-width:0}
.k-card h3{font-family:var(--k-display);font-size:38px;margin:0 0 10px}
.k-stat .k-num{font-family:var(--k-display);font-weight:800;font-size:124px;line-height:1;color:var(--k-accent)}
.k-stat .k-cap{font-size:26px;color:var(--k-muted);margin-top:8px}
.k-hl{box-shadow:inset 0 -.38em 0 color-mix(in srgb,var(--k-accent) 22%,transparent)}
.k-bento{flex:1;min-height:0;display:grid;grid-template-columns:repeat(4,1fr);grid-auto-rows:1fr;grid-auto-flow:dense;gap:22px}
.k-tile{background:var(--k-panel);color:var(--k-text);border-radius:22px;padding:28px 30px;min-width:0;min-height:0;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-end;gap:8px}
.k-tile-wide{grid-column:span 2} .k-tile-tall{grid-row:span 2}
.k-tile h3{font-family:var(--k-display);font-size:34px;margin:0;line-height:1.1}
.k-tile p{margin:0;font-size:26px;color:var(--k-muted)}
.k-tile svg{flex:1;min-height:0;width:100%}
.k-tile .k-num{font-family:var(--k-display);font-weight:800;font-size:88px;line-height:1;color:var(--k-accent)}
.k-steps{flex:1;min-height:0;display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:64px;align-items:center}
.k-step{position:relative;background:var(--k-panel);color:var(--k-text);border-radius:18px;padding:30px 28px;display:flex;flex-direction:column;gap:12px;min-width:0}
.k-step+.k-step::before{content:"→";position:absolute;left:-52px;top:50%;transform:translateY(-50%);font-size:44px;font-weight:800;color:var(--k-accent)}
.k-step .k-n{width:56px;height:56px;border-radius:50%;display:grid;place-items:center;font:800 30px/1 var(--k-display);background:var(--k-text);color:var(--k-panel)}
.k-step h3{font-family:var(--k-display);font-size:34px;margin:0;line-height:1.1}
.k-step p{margin:0;font-size:25px;color:var(--k-muted)}
.k-step svg{width:100%;height:180px}
.k-figure{margin:0;min-width:0;min-height:0;height:100%;max-height:740px;display:grid;place-items:center;overflow:hidden;position:relative}
.k-figure svg{width:100%;height:100%;max-height:740px;overflow:hidden}
.k-figcap{font-size:22px;color:var(--k-muted);margin-top:12px;font-style:italic;justify-self:start}
svg .k-ink{fill:none;stroke:var(--k-text);stroke-width:5;stroke-linecap:round;stroke-linejoin:round}
svg .k-f0{fill:var(--k-panel)} svg .k-f1{fill:var(--k-accent)} svg .k-f2{fill:var(--k-i1)} svg .k-f3{fill:var(--k-i2)} svg .k-f4{fill:var(--k-i3)}
svg .k-shade{fill:var(--k-text);opacity:.14}
svg :is(rect,circle,ellipse,path,polygon,polyline,line):not([class*="k-"]){fill:none;stroke:var(--k-text);stroke-width:3}
svg text:not([class*="k-"]){fill:var(--k-text)}
html body>div:has(.k-title){display:flex;flex-direction:column;gap:34px;flex:1;min-height:0;width:auto;max-width:none}
html body>.k-figure{flex:1;height:auto}
svg .k-lab{font-family:var(--k-body);font-weight:700;font-size:20px;fill:var(--k-text);paint-order:stroke;stroke:var(--k-panel);stroke-width:5px;stroke-linejoin:round}
"""


def css(k: Kit) -> str:
    """The kit's stylesheet: the shared base with this kit's values, then its
    own rules."""
    bg, text, muted, accent, panel, line = k.palette
    under = (
        ""
        if k.under is None
        else 'html body::before{content:"";position:absolute;inset:0;background:'
        + k.under
        + ";pointer-events:none;z-index:0}"
    )
    out = BASE
    for key, value in (
        ("@BG@", bg),
        ("@TEXT@", text),
        ("@MUTED@", muted),
        ("@ACCENT@", accent),
        ("@PANEL@", panel),
        ("@LINE@", line),
        ("@I1@", k.ill[0]),
        ("@I2@", k.ill[1]),
        ("@I3@", k.ill[2]),
        ("@DISPLAY@", k.display),
        ("@BODY@", k.body),
        ("@UNDER@", under),
        ("@AFTER@", k.after or ""),
    ):
        out = out.replace(key, value)
    return out + k.css


# The SVG filters kits refer to: the marker wobble (sketch note), the clay
# sheen and the brick sheen. Placed first in `<body>`.
DEFS = r"""<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>
<filter id="k-wobble" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="turbulence" baseFrequency="0.03" numOctaves="2" seed="3" result="t"/><feDisplacementMap in="SourceGraphic" in2="t" scale="5" xChannelSelector="R" yChannelSelector="G"/></filter>
<filter id="k-clay" x="-15%" y="-15%" width="130%" height="140%"><feGaussianBlur in="SourceAlpha" stdDeviation="9" result="b"/><feSpecularLighting in="b" surfaceScale="6" specularConstant=".5" specularExponent="14" lighting-color="#ffffff" result="s"><feDistantLight azimuth="235" elevation="38"/></feSpecularLighting><feComposite in="s" in2="SourceAlpha" operator="in" result="sp"/><feComposite in="SourceGraphic" in2="sp" operator="arithmetic" k1="0" k2="1" k3=".7" k4="0" result="lit"/><feDropShadow in="lit" dx="0" dy="12" stdDeviation="9" flood-color="#4a2f1f" flood-opacity=".22"/></filter>
<filter id="k-brick" x="-10%" y="-10%" width="120%" height="130%"><feGaussianBlur in="SourceAlpha" stdDeviation="2.5" result="b"/><feSpecularLighting in="b" surfaceScale="3" specularConstant=".75" specularExponent="24" lighting-color="#ffffff" result="s"><feDistantLight azimuth="225" elevation="45"/></feSpecularLighting><feComposite in="s" in2="SourceAlpha" operator="in" result="sp"/><feComposite in="SourceGraphic" in2="sp" operator="arithmetic" k1="0" k2="1" k3=".9" k4="0" result="lit"/><feDropShadow in="lit" dx="0" dy="5" stdDeviation="2.5" flood-opacity=".28"/></filter>
</defs></svg>"""


def apply(html: str, k: Kit) -> str:
    """Put the kit into a slide: its fonts and stylesheet last in `<head>`,
    so the kit wins over anything the model wrote, and its filters first in
    `<body>`."""
    if "data-kit" in html:
        return html
    html = clean(html)
    head = f'<link rel="stylesheet" href="{k.fonts}"><style data-kit>{css(k)}</style>'
    lower = ascii_lower(html)
    at = lower.find("</head>")
    if at >= 0:
        out = html[:at] + head + html[at:]
    else:
        at = lower.find("<body")
        out = (
            html[:at] + f"<head>{head}</head>" + html[at:]
            if at >= 0
            else f"<head>{head}</head><body>{html}</body>"
        )
    at = ascii_lower(out).find("<body")
    if at >= 0:
        close = out.find(">", at)
        if close >= 0:
            out = out[: close + 1] + DEFS + out[close + 1 :]
    return out


def sample(k: Kit) -> str:
    """A sample slide in a style, drawn with its kit: the style's own layout
    and its example figure. What the picker's thumbnail shows."""
    kicker = '<div class="k-kicker">How Linux runs processes</div>'
    ex = k.example
    if k.id == "bento":
        body = (
            f'{kicker}<h1 class="k-title">A process, at a glance</h1><div class="k-bento">'
            f'<div class="k-tile k-tile-wide k-tile-tall">{ex}<h3>Every program becomes a '
            "process</h3><p>The kernel tracks each one in a task_struct</p></div>"
            '<div class="k-tile"><div class="k-num">PID 1</div><p>init starts the rest</p></div>'
            f'<div class="k-tile">{ex}<h3>fork()</h3></div>'
            '<div class="k-tile k-tile-wide"><h3>Scheduler</h3><p>Shares CPU time between '
            "runnable processes</p></div></div>"
        )
    elif k.id == "instructional":
        steps = "".join(
            f'<div class="k-step"><div class="k-n">{i + 1}</div>{ex}<h3>{h}</h3><p>{p}</p></div>'
            for i, (h, p) in enumerate(
                (
                    ("fork()", "The parent is copied"),
                    ("exec()", "A new program loads"),
                    ("Run", "The scheduler gives it CPU"),
                    ("exit()", "The parent collects its status"),
                )
            )
        )
        body = (
            f'{kicker}<h1 class="k-title">From program to process</h1>'
            f'<div class="k-steps">{steps}</div>'
        )
    else:
        cap = (
            '<figcaption class="k-figcap">Fig. 1 — a process and its parts</figcaption>'
            if k.id == "scientific"
            else ""
        )
        body = (
            f'{kicker}<h1 class="k-title">Every program becomes a process</h1><div '
            'class="k-split"><div class="k-stack"><ul class="k-points"><li>The kernel gives '
            'each process a <span class="k-hl">PID</span></li><li>fork() copies the parent; '
            "exec() loads a new program</li><li>The scheduler shares the CPU between them</li>"
            '</ul><div class="k-card k-stat"><div class="k-num">PID 1</div><div class="k-cap">'
            "init starts every other process</div></div></div>"
            f'<figure class="k-figure">{ex}{cap}</figure></div>'
        )
    return apply(
        f'<!doctype html><html><head><meta charset="utf-8"><title>{k.id}</title></head>'
        f"<body>{body}</body></html>",
        k,
    )
