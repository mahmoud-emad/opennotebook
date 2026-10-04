//! The zip container shared by `.docx`, `.pptx` and `.xlsx`: reading parts
//! under a decompression budget, and following relationships between them.

use std::collections::HashMap;
use std::io::{Cursor, Read};

use zip::ZipArchive;

use crate::xml::{self, El};
use crate::{Error, InputKind};

/// The most this crate will decompress out of one file, across all the parts
/// it reads. Real documents are nowhere near it; a zip bomb is far past it,
/// and stopping at the budget is what keeps a 50 KB upload from becoming
/// gigabytes of memory.
pub(crate) const MAX_DECOMPRESSED: u64 = 200 * 1024 * 1024;

/// The signature of an OLE compound file. A password-protected Office file is
/// one of these rather than a zip, as is the older binary format, and saying
/// so is more useful than "not a valid document".
const OLE_SIGNATURE: [u8; 8] = [0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1];

/// One relationship out of a `.rels` part.
#[derive(Debug, Clone)]
pub(crate) struct Rel {
    /// The relationship type URI. Matched on its last segment, since the
    /// strict format uses different prefixes for the same types.
    pub kind: String,
    /// For an internal target, the part's full path in the zip; for an
    /// external one, the target as written (usually a URL).
    pub target: String,
    pub external: bool,
}

impl Rel {
    /// Whether the type URI ends in this segment, e.g. `"hyperlink"`.
    pub fn is(&self, kind: &str) -> bool {
        self.kind.rsplit('/').next() == Some(kind)
    }
}

pub(crate) type Rels = HashMap<String, Rel>;

pub(crate) struct Package<'a> {
    archive: ZipArchive<Cursor<&'a [u8]>>,
    /// Lower-cased part name to its index: part names are case-insensitive
    /// in the packaging rules, and producers do not all agree on case.
    names: HashMap<String, usize>,
    budget: u64,
    pub kind: InputKind,
}

impl<'a> Package<'a> {
    pub fn open(bytes: &'a [u8], kind: InputKind) -> Result<Self, Error> {
        if bytes.starts_with(&OLE_SIGNATURE) {
            return Err(Error::Encrypted(kind));
        }
        let archive = ZipArchive::new(Cursor::new(bytes)).map_err(|_| Error::NotValid(kind))?;
        let names = archive
            .file_names()
            .map(str::to_ascii_lowercase)
            .collect::<Vec<_>>();
        let mut index = HashMap::new();
        for (i, name) in names.into_iter().enumerate() {
            index.entry(name).or_insert(i);
        }
        Ok(Self {
            archive,
            names: index,
            budget: MAX_DECOMPRESSED,
            kind,
        })
    }

    /// Decompress one part, or `None` when the package has no such part.
    /// Every byte read counts against the budget, so a single oversized part
    /// fails as soon as it crosses it rather than after it has been inflated.
    pub fn read_bytes(&mut self, part: &str) -> Result<Option<Vec<u8>>, Error> {
        let key = part.trim_start_matches('/').to_ascii_lowercase();
        let Some(&index) = self.names.get(&key) else {
            return Ok(None);
        };
        let kind = self.kind;
        let file = self
            .archive
            .by_index(index)
            .map_err(|_| Error::Damaged(kind))?;
        let mut out = Vec::new();
        let read = file
            .take(self.budget + 1)
            .read_to_end(&mut out)
            .map_err(|_| Error::Damaged(kind))? as u64;
        if read > self.budget {
            return Err(Error::TooLarge(kind));
        }
        self.budget -= read;
        Ok(Some(out))
    }

    /// Read and parse an XML part.
    pub fn read_xml(&mut self, part: &str) -> Result<Option<El>, Error> {
        let Some(bytes) = self.read_bytes(part)? else {
            return Ok(None);
        };
        let text = decode(&bytes).ok_or(Error::Damaged(self.kind))?;
        xml::parse(&text)
            .map(Some)
            .map_err(|_| Error::Damaged(self.kind))
    }

    /// The relationships of a part (`""` for the package itself), with
    /// internal targets resolved to full part paths. A part with no `.rels`
    /// simply has no relationships.
    pub fn rels(&mut self, part: &str) -> Result<Rels, Error> {
        let (dir, file) = match part.rfind('/') {
            Some(i) => (&part[..i + 1], &part[i + 1..]),
            None => ("", part),
        };
        let Some(root) = self.read_xml(&format!("{dir}_rels/{file}.rels"))? else {
            return Ok(Rels::new());
        };
        let mut rels = Rels::new();
        for rel in root.elements() {
            let (Some(id), Some(kind), Some(target)) =
                (rel.attr("Id"), rel.attr("Type"), rel.attr("Target"))
            else {
                continue;
            };
            let external = rel.attr("TargetMode") == Some("External");
            let target = if external {
                target.to_string()
            } else {
                resolve(dir, target)
            };
            rels.insert(
                id.to_string(),
                Rel {
                    kind: kind.to_string(),
                    target,
                    external,
                },
            );
        }
        Ok(rels)
    }

    /// The package's main part, found the way the format intends (through the
    /// package relationships) and falling back to the conventional path for
    /// producers that leave the relationship out.
    pub fn main_part(&mut self, conventional: &str) -> Result<String, Error> {
        let rels = self.rels("")?;
        let found = rels
            .values()
            .find(|r| r.is("officeDocument") && !r.external)
            .map(|r| r.target.clone());
        let part = found.unwrap_or_else(|| conventional.to_string());
        if self.names.contains_key(&part.to_ascii_lowercase()) {
            Ok(part)
        } else {
            Err(Error::NotValid(self.kind))
        }
    }

    /// Decompress every entry once, counting against the budget, and throw
    /// the bytes away. Used before handing the archive to a library that does
    /// its own unbounded reading.
    pub fn check_budget(&mut self) -> Result<(), Error> {
        let kind = self.kind;
        for i in 0..self.archive.len() {
            let file = self.archive.by_index(i).map_err(|_| Error::Damaged(kind))?;
            let read = std::io::copy(&mut file.take(self.budget + 1), &mut std::io::sink())
                .map_err(|_| Error::Damaged(kind))?;
            if read > self.budget {
                return Err(Error::TooLarge(kind));
            }
            self.budget -= read;
        }
        Ok(())
    }
}

/// XML parts are UTF-8 in practice, but the format allows UTF-16 with a byte
/// order mark, so both are accepted.
fn decode(bytes: &[u8]) -> Option<String> {
    if let Some(rest) = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        return String::from_utf8(rest.to_vec()).ok();
    }
    let utf16 = |rest: &[u8], le: bool| {
        let units = rest.chunks_exact(2).map(|c| {
            if le {
                u16::from_le_bytes([c[0], c[1]])
            } else {
                u16::from_be_bytes([c[0], c[1]])
            }
        });
        char::decode_utf16(units)
            .collect::<Result<String, _>>()
            .ok()
    };
    if let Some(rest) = bytes.strip_prefix(&[0xFF, 0xFE]) {
        return utf16(rest, true);
    }
    if let Some(rest) = bytes.strip_prefix(&[0xFE, 0xFF]) {
        return utf16(rest, false);
    }
    String::from_utf8(bytes.to_vec()).ok()
}

/// Resolve a relationship target against the directory of the part that
/// holds the relationship. Targets may be absolute (`/word/media/x.png`) or
/// relative with `..` segments (`../slides/slide1.xml`).
fn resolve(dir: &str, target: &str) -> String {
    let joined = if let Some(abs) = target.strip_prefix('/') {
        abs.to_string()
    } else {
        format!("{dir}{target}")
    };
    let mut parts: Vec<&str> = Vec::new();
    for seg in joined.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            s => parts.push(s),
        }
    }
    parts.join("/")
}

#[cfg(test)]
mod tests {
    use super::resolve;

    #[test]
    fn targets_resolve_against_the_part_directory() {
        assert_eq!(
            resolve("ppt/", "slides/slide1.xml"),
            "ppt/slides/slide1.xml"
        );
        assert_eq!(
            resolve("ppt/slides/", "../notesSlides/notesSlide1.xml"),
            "ppt/notesSlides/notesSlide1.xml"
        );
        assert_eq!(resolve("", "word/document.xml"), "word/document.xml");
        assert_eq!(
            resolve("word/", "/word/footnotes.xml"),
            "word/footnotes.xml"
        );
    }
}
