//! Takes the colour decisions back from a model-written slide.
//!
//! The prompt tells the model the kit owns every colour, background and
//! position, but a model does not always listen: one painted a full-slide
//! `<rect>` with its own near-black gradient over a light kit, and pinned the
//! figure over the whole slide with an inline `position:absolute`. So before
//! the kit goes on, [`clean`] removes what the model was told not to write:
//!
//! * colour attributes (`fill`, `stroke`, `stop-color`, `color`, `bgcolor`,
//!   `background`) and opacity, which is how a figure gets faded into a
//!   backdrop; `fill="none"` stays, it only says "no fill"
//! * gradients and patterns, which exist only to be painted
//! * a `<rect>` that covers most of its SVG, which is a background
//! * every CSS declaration, inline or in a `<style>`, outside the layout
//!   properties the prompt allows
//! * emoji, which the kits' fonts cannot draw
//! * scripts, and stylesheet links: the kit's is the only one
//!
//! It is a tag scanner, not a parser: what it does not recognise it passes
//! through untouched.

/// The CSS properties a slide may set itself; everything else is the kit's.
const LAYOUT: &[&str] = &[
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
];

const COLOUR_ATTRS: &[&str] = &[
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
];

/// Elements dropped whole, contents and all.
const DROPPED: &[&str] = &["lineargradient", "radialgradient", "pattern", "script"];

/// A rect covering this much of its SVG in both directions is a background.
const BACKGROUND_SHARE: f64 = 0.9;

pub fn clean(html: &str) -> String {
    let mut out = String::with_capacity(html.len());
    // The viewBox of the SVG being read, as width and height.
    let mut view: Option<(f64, f64)> = None;
    let mut rest = html;
    while let Some(at) = rest.find('<') {
        out.push_str(&without_emoji(&rest[..at]));
        rest = &rest[at..];
        if rest.starts_with("<!--") {
            let end = rest.find("-->").map_or(rest.len(), |e| e + 3);
            out.push_str(&rest[..end]);
            rest = &rest[end..];
            continue;
        }
        let Some(end) = tag_end(rest) else {
            break;
        };
        let tag = &rest[..end];
        rest = &rest[end..];
        let name = tag_name(tag);
        if DROPPED.contains(&name.as_str()) {
            if !tag.ends_with("/>") {
                rest = skip_past_close(rest, &name);
            }
            continue;
        }
        match name.as_str() {
            // The kit brings the only stylesheet a slide loads.
            "link" => continue,
            "svg" => view = attr(tag, "viewbox").and_then(|v| view_size(&v)).or(view),
            "/svg" => view = None,
            "rect" if is_background(tag, view) => {
                if !tag.ends_with("/>") {
                    rest = skip_past_close(rest, "rect");
                }
                continue;
            }
            "style" if attr(tag, "data-kit").is_none() => {
                out.push_str(tag);
                let close = find_ci(rest, "</style").unwrap_or(rest.len());
                out.push_str(&layout_only_sheet(&rest[..close]));
                rest = &rest[close..];
                continue;
            }
            _ => {}
        }
        out.push_str(&clean_tag(tag, &name));
    }
    out.push_str(&without_emoji(rest));
    out
}

/// Text without pictographs: the kits' fonts have none, so an emoji draws as
/// an empty box.
fn without_emoji(text: &str) -> String {
    text.chars()
        .filter(|c| !matches!(*c as u32, 0x1F000..=0x1FAFF | 0x2600..=0x27BF | 0xFE0F | 0x200D))
        .collect()
}

/// The index just past the `>` closing the tag at the start of `s`, skipping
/// any `>` inside a quoted attribute value.
fn tag_end(s: &str) -> Option<usize> {
    let mut quote = None;
    for (i, c) in s.char_indices().skip(1) {
        match (quote, c) {
            (None, '"' | '\'') => quote = Some(c),
            (Some(q), c) if c == q => quote = None,
            (None, '>') => return Some(i + 1),
            _ => {}
        }
    }
    None
}

/// The tag's name lowercased, with a leading `/` for a closing tag.
fn tag_name(tag: &str) -> String {
    let s = &tag[1..];
    let (slash, s) = match s.strip_prefix('/') {
        Some(s) => ("/", s),
        None => ("", s),
    };
    let name: String = s
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | ':' | '!' | '?'))
        .collect();
    format!("{slash}{}", name.to_ascii_lowercase())
}

fn find_ci(hay: &str, needle: &str) -> Option<usize> {
    hay.to_ascii_lowercase().find(needle)
}

fn skip_past_close<'a>(rest: &'a str, name: &str) -> &'a str {
    match find_ci(rest, &format!("</{name}")) {
        Some(at) => {
            let from = &rest[at..];
            &from[tag_end(from).unwrap_or(from.len())..]
        }
        None => "",
    }
}

/// The tag's attributes in order, values unquoted. A bare
/// attribute has no value.
fn attrs(tag: &str) -> Vec<(String, Option<String>)> {
    let inner = tag
        .trim_start_matches('<')
        .trim_end_matches('>')
        .trim_end_matches('/');
    let mut chars = inner.char_indices().peekable();
    // Skip the tag name.
    while chars.next_if(|(_, c)| !c.is_whitespace()).is_some() {}
    let mut out = Vec::new();
    loop {
        while chars.next_if(|(_, c)| c.is_whitespace()).is_some() {}
        let Some(&(start, _)) = chars.peek() else {
            break;
        };
        while chars
            .next_if(|(_, c)| !c.is_whitespace() && *c != '=')
            .is_some()
        {}
        let end = chars.peek().map_or(inner.len(), |(i, _)| *i);
        let name = inner[start..end].to_string();
        while chars.next_if(|(_, c)| c.is_whitespace()).is_some() {}
        if chars.next_if(|(_, c)| *c == '=').is_none() {
            out.push((name, None));
            continue;
        }
        while chars.next_if(|(_, c)| c.is_whitespace()).is_some() {}
        let value = match chars.next() {
            Some((i, q @ ('"' | '\''))) => {
                let from = i + 1;
                let mut to = inner.len();
                for (j, c) in chars.by_ref() {
                    if c == q {
                        to = j;
                        break;
                    }
                }
                inner[from..to].to_string()
            }
            Some((i, _)) => {
                while chars.next_if(|(_, c)| !c.is_whitespace()).is_some() {}
                let to = chars.peek().map_or(inner.len(), |(j, _)| *j);
                inner[i..to].to_string()
            }
            None => String::new(),
        };
        out.push((name, Some(value)));
    }
    out
}

fn attr(tag: &str, name: &str) -> Option<String> {
    attrs(tag)
        .into_iter()
        .find(|(n, _)| n.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.unwrap_or_default())
}

fn view_size(view_box: &str) -> Option<(f64, f64)> {
    let n: Vec<f64> = view_box
        .split(|c: char| c.is_whitespace() || c == ',')
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect();
    (n.len() == 4 && n[2] > 0.0 && n[3] > 0.0).then(|| (n[2], n[3]))
}

fn is_background(tag: &str, view: Option<(f64, f64)>) -> bool {
    let covers = |name: &str, full: Option<f64>| {
        let Some(v) = attr(tag, name) else {
            return false;
        };
        let v = v.trim();
        if let Some(pct) = v.strip_suffix('%') {
            return pct
                .trim()
                .parse::<f64>()
                .is_ok_and(|p| p >= BACKGROUND_SHARE * 100.0);
        }
        match (v.trim_end_matches("px").parse::<f64>(), full) {
            (Ok(n), Some(full)) => n >= BACKGROUND_SHARE * full,
            _ => false,
        }
    };
    covers("width", view.map(|v| v.0)) && covers("height", view.map(|v| v.1))
}

fn clean_tag(tag: &str, name: &str) -> String {
    if name.starts_with('/') || name.starts_with('!') || name.starts_with('?') {
        return tag.to_string();
    }
    let kept: Vec<String> = attrs(tag)
        .into_iter()
        .filter_map(|(n, v)| {
            let lower = n.to_ascii_lowercase();
            if COLOUR_ATTRS.contains(&lower.as_str()) {
                return (lower == "fill" && v.as_deref().map(str::trim) == Some("none"))
                    .then(|| "fill=\"none\"".to_string());
            }
            match (lower.as_str(), v) {
                ("style", Some(v)) => {
                    let v = layout_only(&v);
                    (!v.is_empty()).then(|| format!("style=\"{}\"", v.replace('"', "'")))
                }
                (_, None) => Some(n),
                (_, Some(v)) => Some(format!("{n}=\"{}\"", v.replace('"', "&quot;"))),
            }
        })
        .collect();
    let close = if tag.ends_with("/>") { "/>" } else { ">" };
    let head: String = tag[1..]
        .chars()
        .take_while(|c| !c.is_whitespace() && *c != '>' && *c != '/')
        .collect();
    let head = if head.is_empty() {
        name.to_string()
    } else {
        head
    };
    if kept.is_empty() {
        format!("<{head}{close}")
    } else {
        format!("<{head} {}{close}", kept.join(" "))
    }
}

/// A declaration list with only the layout properties left.
fn layout_only(decls: &str) -> String {
    decls
        .split(';')
        .filter_map(|d| {
            let (prop, value) = d.split_once(':')?;
            let prop = prop.trim().to_ascii_lowercase();
            LAYOUT
                .contains(&prop.as_str())
                .then(|| format!("{prop}:{}", value.trim()))
        })
        .collect::<Vec<_>>()
        .join(";")
}

/// A stylesheet with every rule body reduced to its layout properties; a rule
/// left empty is dropped. `@import` and `@font-face` go entirely.
fn layout_only_sheet(css: &str) -> String {
    let mut out = String::new();
    let mut rest = css;
    while let Some(open) = rest.find('{') {
        let selector = rest[..open].trim();
        let after = &rest[open + 1..];
        // A block holding blocks (@media, @supports): keep its wrapper and
        // clean what is inside.
        let inner_open = after.find('{');
        let close = after.find('}').unwrap_or(after.len());
        if inner_open.is_some_and(|i| i < close) {
            let mut depth = 1;
            let mut end = after.len();
            for (i, c) in after.char_indices() {
                match c {
                    '{' => depth += 1,
                    '}' => {
                        depth -= 1;
                        if depth == 0 {
                            end = i;
                            break;
                        }
                    }
                    _ => {}
                }
            }
            let body = layout_only_sheet(&after[..end]);
            if !body.trim().is_empty() && !selector.to_ascii_lowercase().starts_with("@font-face") {
                out.push_str(&format!("{selector}{{{body}}}"));
            }
            rest = after.get(end + 1..).unwrap_or("");
            continue;
        }
        let body = layout_only(&after[..close]);
        let selector = selector.rsplit(';').next().unwrap_or("").trim();
        if !body.is_empty() && !selector.starts_with('@') {
            out.push_str(&format!("{selector}{{{body}}}"));
        }
        rest = after.get(close + 1..).unwrap_or("");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::clean;

    #[test]
    fn a_painted_background_rect_and_its_gradient_go() {
        let html = r#"<figure class="k-figure" style="position:absolute;top:0;width:100%"><svg viewBox="0 0 1920 1080"><defs><linearGradient id="g"><stop offset="0" style="stop-color:#1a1a1a"/></linearGradient></defs><rect width="1920" height="1080" fill="url(#g)"/><circle class="k-ink" cx="9" cy="9" r="4" stroke-width="2"/></svg></figure>"#;
        let out = clean(html);
        assert!(!out.contains("linearGradient"), "{out}");
        assert!(!out.contains("<rect"), "{out}");
        assert!(!out.contains("position"), "{out}");
        assert!(out.contains(r#"style="width:100%""#), "{out}");
        assert!(
            out.contains(r#"<circle class="k-ink" cx="9" cy="9" r="4" stroke-width="2"/>"#),
            "{out}"
        );
    }

    #[test]
    fn colours_go_and_fill_none_stays() {
        let out = clean(
            r##"<svg viewBox="0 0 800 600"><path class="k-ink" d="M0 0" fill="none" stroke="#000"/><text fill="#111" x="4">hi</text><rect class="k-f1" x="1" y="1" width="80" height="60" fill="black"/></svg>"##,
        );
        assert_eq!(
            out,
            r#"<svg viewBox="0 0 800 600"><path class="k-ink" d="M0 0" fill="none"/><text x="4">hi</text><rect class="k-f1" x="1" y="1" width="80" height="60"/></svg>"#
        );
    }

    #[test]
    fn a_slide_stylesheet_keeps_only_layout() {
        let out = clean(
            "<style>body{background:#000;color:#fff} .a{display:grid;gap:20px;color:red} .b{font-size:9px} @media (x){.c{width:50%;fill:red}}</style><p>x</p>",
        );
        assert_eq!(
            out,
            "<style>.a{display:grid;gap:20px}@media (x){.c{width:50%}}</style><p>x</p>"
        );
    }

    #[test]
    fn the_kits_own_style_and_plain_text_pass_through() {
        let html = r#"<!doctype html><html><head><style data-kit>body{color:red}</style></head><body><!-- a > b --><p class="k-sub">5 > 3 & "so"</p></body></html>"#;
        assert_eq!(clean(html), html);
    }
}
