"""The visual styles a deck can be drawn in.

One table, read by both sides: the create page shows `label` and `blurb` as
the picker, and the build hands `brief` to the slide model alongside the
style's kit, which supplies every font, colour and texture. The eight are
NotebookLM's infographic styles, picked in Slide Lab as the ones that read
best as slides.

`id` is what crosses the wire and is append-only. The earlier styles (vector,
painted, watercolour…) were retired with the earlier slide service; outputs
built in them keep their slides, and a request still naming one of those ids
gets the default style.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SlideStyle:
    id: str
    label: str
    # One line under the label in the picker.
    blurb: str
    # What the style looks like, for the slide model.
    brief: str


# Every style, in the order the picker shows them. The first is the default.
STYLES: tuple[SlideStyle, ...] = (
    SlideStyle(
        "editorial",
        "Editorial",
        "Magazine type, thin rules",
        "Magazine editorial: near-white page, deep charcoal type, one restrained accent, "
        "a high-contrast serif for headlines, thin rules rather than boxes, generous "
        "whitespace, one idea per slide set large.",
    ),
    SlideStyle(
        "professional",
        "Professional",
        "Report grid, crisp charts",
        "Clean business report: a structured grid, navy and grey with restrained blue, "
        "crisp charts and line icons, a neutral sans.",
    ),
    SlideStyle(
        "bento",
        "Bento grid",
        "A mosaic of tiles",
        "A bento grid: each slide is a mosaic of rounded tiles of different sizes, each "
        "holding one fact, stat or small illustration, with a bold modern sans and bright "
        "tile colours.",
    ),
    SlideStyle(
        "instructional",
        "Instructional",
        "Numbered steps, arrows",
        "Step-by-step storyboard: numbered panels in sequence joined by arrows, one small "
        "pictogram per step, clear and friendly.",
    ),
    SlideStyle(
        "scientific",
        "Scientific",
        "Labelled textbook figures",
        "Textbook figure: precise thin-line diagrams with leader-line labels and numbered "
        "figure captions, a muted blue-green palette, serif headings.",
    ),
    SlideStyle(
        "sketchnote",
        "Sketch note",
        "Doodles on dotted paper",
        "Hand-drawn sketch notes on dotted paper: marker doodles in two or three colours, "
        "hand-lettered headings, banners, boxes, arrows and simple icons.",
    ),
    SlideStyle(
        "clay",
        "Clay",
        "Soft plasticine shapes",
        "Claymation: soft matte plasticine shapes with rounded edges, gentle highlights and "
        "soft shadows, warm friendly colours.",
    ),
    SlideStyle(
        "bricks",
        "Bricks",
        "Built from toy bricks",
        "Toy bricks: objects built from chunky interlocking plastic bricks with studs on "
        "top, primary colours, bold rounded type.",
    ),
)

# The style an output gets when the caller names none.
DEFAULT_STYLE = STYLES[0]


def style(id: str) -> SlideStyle | None:
    """A style by its wire id. None for an id this build does not know, which
    is what an older client sending a retired id looks like: the caller
    decides whether that is a fallback or a refusal."""
    return next((s for s in STYLES if s.id == id), None)
