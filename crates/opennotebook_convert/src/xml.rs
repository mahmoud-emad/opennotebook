//! A small element tree over `quick-xml`, with namespaces resolved to fixed
//! prefixes.
//!
//! Office parts are walked in document order with a lot of looking around
//! (a paragraph's style, a cell's span, a shape's placeholder type), which is
//! far easier on a tree than on a stream of events. The parts are bounded by
//! the zip budget, so holding one in memory is fine.
//!
//! Names are stored as `prefix:local` with the prefix chosen from the
//! namespace URI, not copied from the file. Word itself always writes `w:`,
//! but other producers are free to bind the same namespace to `ns0:` or to a
//! default namespace, and the "strict" flavour of the format uses different
//! URIs altogether. Matching on a canonical prefix makes all of those read
//! the same.

use quick_xml::NsReader;
use quick_xml::events::Event;
use quick_xml::name::ResolveResult;

/// Deeper nesting than this is not a real document. The walkers in this crate
/// recurse, so the limit is what keeps a crafted file from overflowing the
/// stack.
const MAX_DEPTH: usize = 400;

#[derive(Debug, Clone)]
pub(crate) enum Node {
    El(El),
    Text(String),
}

#[derive(Debug, Clone, Default)]
pub(crate) struct El {
    pub name: String,
    pub attrs: Vec<(String, String)>,
    pub children: Vec<Node>,
}

impl El {
    /// The value of an attribute by its canonical name, e.g. `w:val` or `Id`.
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.as_str())
    }

    /// The element children, skipping text between them.
    pub fn elements(&self) -> impl Iterator<Item = &El> {
        self.children.iter().filter_map(|n| match n {
            Node::El(e) => Some(e),
            Node::Text(_) => None,
        })
    }

    pub fn child(&self, name: &str) -> Option<&El> {
        self.elements().find(|e| e.name == name)
    }

    pub fn children_named<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a El> + 'a {
        self.elements().filter(move |e| e.name == name)
    }

    /// Follow a path of child names, e.g. `["p:nvSpPr", "p:nvPr", "p:ph"]`.
    pub fn path(&self, names: &[&str]) -> Option<&El> {
        names.iter().try_fold(self, |el, name| el.child(name))
    }

    /// The first descendant with this name, depth first, including `self`.
    pub fn find(&self, name: &str) -> Option<&El> {
        if self.name == name {
            return Some(self);
        }
        self.elements().find_map(|e| e.find(name))
    }

    /// Every descendant with this name, outermost first, not looking inside a
    /// match.
    pub fn find_all<'a>(&'a self, name: &str, out: &mut Vec<&'a El>) {
        for e in self.elements() {
            if e.name == name {
                out.push(e);
            } else {
                e.find_all(name, out);
            }
        }
    }

    /// The text directly inside this element, e.g. the content of `<w:t>`.
    pub fn own_text(&self) -> String {
        let mut s = String::new();
        for n in &self.children {
            if let Node::Text(t) = n {
                s.push_str(t);
            }
        }
        s
    }
}

/// Map a namespace URI to the prefix the walkers match on. Unknown namespaces
/// keep their local name behind a `?:` so they can never collide with a known
/// one.
fn prefix_for(uri: &[u8]) -> &'static str {
    match uri {
        b"http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        | b"http://purl.oclc.org/ooxml/wordprocessingml/main" => "w",
        b"http://schemas.openxmlformats.org/drawingml/2006/main"
        | b"http://purl.oclc.org/ooxml/drawingml/main" => "a",
        b"http://schemas.openxmlformats.org/presentationml/2006/main"
        | b"http://purl.oclc.org/ooxml/presentationml/main" => "p",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        | b"http://purl.oclc.org/ooxml/officeDocument/relationships" => "r",
        b"http://schemas.openxmlformats.org/package/2006/relationships" => "pr",
        b"http://schemas.openxmlformats.org/markup-compatibility/2006" => "mc",
        b"http://schemas.microsoft.com/office/word/2010/wordprocessingShape" => "wps",
        b"urn:schemas-microsoft-com:vml" => "v",
        b"http://schemas.openxmlformats.org/drawingml/2006/diagram"
        | b"http://purl.oclc.org/ooxml/drawingml/diagram" => "dgm",
        b"http://schemas.openxmlformats.org/officeDocument/2006/math"
        | b"http://purl.oclc.org/ooxml/officeDocument/math" => "m",
        _ => "?",
    }
}

fn qualified(ns: &ResolveResult<'_>, local: &[u8]) -> String {
    let local = String::from_utf8_lossy(local);
    match ns {
        ResolveResult::Bound(uri) => format!("{}:{local}", prefix_for(uri.as_ref())),
        // Attributes without a prefix (`Id`, `Target`, `lvl`) are unbound by
        // the XML rules, and match on their bare name.
        ResolveResult::Unbound => local.into_owned(),
        ResolveResult::Unknown(_) => format!("?:{local}"),
    }
}

/// Why a part did not parse. The caller turns it into the document-level
/// [`crate::Error`], which knows the document kind.
#[derive(Debug)]
pub(crate) struct Malformed;

/// Parse a whole part into its root element.
pub(crate) fn parse(text: &str) -> Result<El, Malformed> {
    let mut reader = NsReader::from_str(text);
    let config = reader.config_mut();
    config.trim_text_start = false;
    config.trim_text_end = false;
    config.expand_empty_elements = false;

    // `stack[0]` is a synthetic document node whose first element child is
    // the root.
    let mut stack: Vec<El> = vec![El::default()];
    loop {
        let (ns, event) = reader.read_resolved_event().map_err(|_| Malformed)?;
        match event {
            Event::Start(ref e) | Event::Empty(ref e) => {
                let name = qualified(&ns, e.local_name().as_ref());
                let mut attrs = Vec::new();
                for attr in e.attributes().with_checks(false) {
                    let attr = attr.map_err(|_| Malformed)?;
                    let (ans, local) = reader.resolver().resolve_attribute(attr.key);
                    // Namespace declarations are how the names were resolved;
                    // the walkers never need them.
                    if attr.key.as_ref().starts_with(b"xmlns") {
                        continue;
                    }
                    let value = attr
                        .normalized_value(quick_xml::XmlVersion::Implicit1_0)
                        .map_err(|_| Malformed)?;
                    attrs.push((qualified(&ans, local.as_ref()), value.into_owned()));
                }
                let el = El {
                    name,
                    attrs,
                    children: Vec::new(),
                };
                if matches!(event, Event::Start(_)) {
                    if stack.len() > MAX_DEPTH {
                        return Err(Malformed);
                    }
                    stack.push(el);
                } else {
                    push_child(&mut stack, el);
                }
            }
            Event::End(_) => {
                if stack.len() < 2 {
                    return Err(Malformed);
                }
                let el = stack.pop().ok_or(Malformed)?;
                push_child(&mut stack, el);
            }
            Event::Text(t) => {
                let t = t.decode().map_err(|_| Malformed)?;
                push_text(&mut stack, &t);
            }
            Event::CData(t) => {
                let t = t.decode().map_err(|_| Malformed)?;
                push_text(&mut stack, &t);
            }
            Event::GeneralRef(r) => {
                let text = if let Some(c) = r.resolve_char_ref().map_err(|_| Malformed)? {
                    c.to_string()
                } else {
                    let name = r.decode().map_err(|_| Malformed)?;
                    quick_xml::escape::resolve_predefined_entity(&name)
                        .ok_or(Malformed)?
                        .to_string()
                };
                push_text(&mut stack, &text);
            }
            Event::Eof => break,
            _ => {}
        }
    }
    if stack.len() != 1 {
        return Err(Malformed);
    }
    let doc = stack.pop().ok_or(Malformed)?;
    doc.children
        .into_iter()
        .find_map(|n| match n {
            Node::El(e) => Some(e),
            Node::Text(_) => None,
        })
        .ok_or(Malformed)
}

/// Attach a finished element to its parent, resolving markup compatibility
/// on the way: an `mc:AlternateContent` holds the same content more than once
/// (a modern rendering and a fallback), and keeping both would print every
/// text box twice. The first `mc:Choice` wins, as it would in Word; the
/// `mc:Fallback` is used only when there is no choice at all.
fn push_child(stack: &mut [El], mut el: El) {
    let Some(parent) = stack.last_mut() else {
        return;
    };
    if el.name == "mc:AlternateContent" {
        let position = |name: &str| {
            el.children
                .iter()
                .position(|n| matches!(n, Node::El(e) if e.name == name))
        };
        let branch = position("mc:Choice").or_else(|| position("mc:Fallback"));
        if let Some(i) = branch
            && let Node::El(branch) = el.children.swap_remove(i)
        {
            parent.children.extend(branch.children);
        }
        return;
    }
    parent.children.push(Node::El(el));
}

fn push_text(stack: &mut [El], text: &str) {
    let Some(parent) = stack.last_mut() else {
        return;
    };
    // Entity references arrive as their own events, so `a &amp; b` is three
    // pieces; joining them keeps one text node per run of characters.
    if let Some(Node::Text(prev)) = parent.children.last_mut() {
        prev.push_str(text);
    } else {
        parent.children.push(Node::Text(text.to_string()));
    }
}
