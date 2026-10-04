//! The visual styles a session's deck can be drawn in.
//!
//! One table, read by both sides: the create page renders `label`/`blurb` as the
//! picker, and the server hands `brief` to the slide model alongside the style's
//! kit (`opennotebook_build::kits`), which supplies every font, colour and
//! texture. The eight are NotebookLM's infographic styles, picked in Slide Lab
//! as the ones that read best as slides.
//!
//! It lives in the SDK because that is the only crate both the wasm app and the
//! native server can depend on. `id` is what crosses the wire. The earlier
//! styles (vector, painted, watercolour…) were retired with the earlier slide
//! service; sessions built in them keep their slides, and a page still
//! sending one of those ids gets the default style.

/// One selectable look.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SlideStyle {
    /// Wire value. Append-only.
    pub id: &'static str,
    pub label: &'static str,
    /// One line under the label in the picker.
    pub blurb: &'static str,
    /// What the style looks like, for the slide model.
    pub brief: &'static str,
}

/// Every style, in the order the picker shows them. The first is the default.
pub const STYLES: &[SlideStyle] = &[
    SlideStyle {
        id: "editorial",
        label: "Editorial",
        blurb: "Magazine type, thin rules",
        brief: "Magazine editorial: near-white page, deep charcoal type, one restrained accent, a high-contrast serif for headlines, thin rules rather than boxes, generous whitespace, one idea per slide set large.",
    },
    SlideStyle {
        id: "professional",
        label: "Professional",
        blurb: "Report grid, crisp charts",
        brief: "Clean business report: a structured grid, navy and grey with restrained blue, crisp charts and line icons, a neutral sans.",
    },
    SlideStyle {
        id: "bento",
        label: "Bento grid",
        blurb: "A mosaic of tiles",
        brief: "A bento grid: each slide is a mosaic of rounded tiles of different sizes, each holding one fact, stat or small illustration, with a bold modern sans and bright tile colours.",
    },
    SlideStyle {
        id: "instructional",
        label: "Instructional",
        blurb: "Numbered steps, arrows",
        brief: "Step-by-step storyboard: numbered panels in sequence joined by arrows, one small pictogram per step, clear and friendly.",
    },
    SlideStyle {
        id: "scientific",
        label: "Scientific",
        blurb: "Labelled textbook figures",
        brief: "Textbook figure: precise thin-line diagrams with leader-line labels and numbered figure captions, a muted blue-green palette, serif headings.",
    },
    SlideStyle {
        id: "sketchnote",
        label: "Sketch note",
        blurb: "Doodles on dotted paper",
        brief: "Hand-drawn sketch notes on dotted paper: marker doodles in two or three colours, hand-lettered headings, banners, boxes, arrows and simple icons.",
    },
    SlideStyle {
        id: "clay",
        label: "Clay",
        blurb: "Soft plasticine shapes",
        brief: "Claymation: soft matte plasticine shapes with rounded edges, gentle highlights and soft shadows, warm friendly colours.",
    },
    SlideStyle {
        id: "bricks",
        label: "Bricks",
        blurb: "Built from toy bricks",
        brief: "Toy bricks: objects built from chunky interlocking plastic bricks with studs on top, primary colours, bold rounded type.",
    },
];

/// The style a session gets when the caller names none.
pub const DEFAULT_STYLE: &SlideStyle = &STYLES[0];

/// Look one up by wire id. `None` for an id this build does not know, which is
/// what an older page sending a retired id looks like — the caller decides
/// whether that is a fallback or a refusal.
pub fn style(id: &str) -> Option<&'static SlideStyle> {
    STYLES.iter().find(|s| s.id == id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_unique_and_every_style_carries_a_brief() {
        for (i, s) in STYLES.iter().enumerate() {
            assert!(!s.brief.is_empty(), "{} has no brief", s.id);
            assert!(
                STYLES[..i].iter().all(|o| o.id != s.id),
                "duplicate id {}",
                s.id
            );
            assert_eq!(style(s.id).map(|f| f.label), Some(s.label));
        }
        assert_eq!(style("nothing-like-this"), None);
    }
}
