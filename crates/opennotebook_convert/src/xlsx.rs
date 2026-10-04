//! Excel (`.xlsx`) to Markdown, one GFM table per sheet, through `calamine`.
//!
//! `calamine` already resolves shared strings, cell types and which number
//! formats are dates, so this module only decides how each value reads: whole
//! numbers without a trailing `.0`, fractions without binary noise, dates in
//! ISO form. The first non-empty row is taken as the header, since that is
//! where a sheet's column names nearly always are.

use std::io::Cursor;

use calamine::{Data, ExcelDateTime, Reader, SheetType, Xlsx};

use crate::markdown;
use crate::ooxml::Package;
use crate::{Error, InputKind};

const KIND: InputKind = InputKind::Excel;

/// Data rows written per sheet. A sheet past this is a dataset rather than a
/// document; the rows written are enough to show what it holds, and the
/// closing line says how much more there is.
pub(crate) const MAX_ROWS: usize = 2000;

pub(crate) fn convert(bytes: &[u8]) -> Result<String, Error> {
    // `calamine` reads parts without a size limit, so the whole archive is
    // inflated once under the budget first. It costs a second pass over the
    // file, which is cheap next to holding a zip bomb in memory.
    Package::open(bytes, KIND)?.check_budget()?;

    let mut book: Xlsx<_> = Xlsx::new(Cursor::new(bytes)).map_err(|_| Error::NotValid(KIND))?;
    let sheets: Vec<String> = book
        .sheets_metadata()
        .iter()
        .filter(|s| s.typ == SheetType::WorkSheet)
        .map(|s| s.name.clone())
        .collect();

    let mut sections = Vec::new();
    for name in sheets {
        let range = book
            .worksheet_range(&name)
            .map_err(|_| Error::Damaged(KIND))?;
        let rows: Vec<Vec<String>> = range
            .rows()
            .map(|row| row.iter().map(render).collect())
            .collect();
        if let Some(table) = sheet_table(rows) {
            sections.push(format!("## {}\n\n{table}", markdown::one_line(&name)));
        }
    }
    Ok(sections.join("\n\n"))
}

/// A sheet's rendered cells as a table, or `None` for a sheet with no values.
/// Leading and trailing empty rows go, as do columns that are empty in every
/// row at the right-hand edge; empty rows between data stay, because a gap
/// in a sheet often separates one block of data from the next.
fn sheet_table(mut rows: Vec<Vec<String>>) -> Option<String> {
    let has_value = |r: &Vec<String>| r.iter().any(|c| !c.is_empty());
    let first = rows.iter().position(has_value)?;
    let last = rows.iter().rposition(has_value)?;
    rows.truncate(last + 1);
    rows.drain(..first);

    let width = rows
        .iter()
        .filter_map(|r| r.iter().rposition(|c| !c.is_empty()))
        .max()
        .map_or(0, |i| i + 1);
    // A blank column on the left is trimmed as well, as Excel users often
    // start a table in column B.
    let left = rows
        .iter()
        .filter_map(|r| r.iter().position(|c| !c.is_empty()))
        .min()
        .unwrap_or(0);
    for row in &mut rows {
        row.truncate(width);
        row.drain(..left.min(row.len()));
    }

    let data_rows = rows.len() - 1;
    let omitted = data_rows.saturating_sub(MAX_ROWS);
    rows.truncate(MAX_ROWS + 1);
    let mut out = markdown::table(&rows);
    if omitted > 0 {
        let noun = if omitted == 1 { "row was" } else { "rows were" };
        out.push_str(&format!(
            "\n\n{omitted} more {noun} left out of this sheet."
        ));
    }
    Some(out)
}

fn render(value: &Data) -> String {
    let text = match value {
        Data::Empty => String::new(),
        Data::String(s) => s.clone(),
        Data::Int(i) => i.to_string(),
        Data::Float(f) => number(*f),
        Data::Bool(b) => if *b { "TRUE" } else { "FALSE" }.to_string(),
        Data::DateTime(d) => date(d),
        Data::DateTimeIso(s) | Data::DurationIso(s) => s.clone(),
        Data::Error(e) => e.to_string(),
    };
    markdown::cell(&text.replace("\r\n", "\n"))
}

/// A number as a person would type it. Excel stores every number as a double,
/// so `0.1 + 0.2` is stored as `0.30000000000000004`; Excel itself shows at
/// most 15 significant digits, and rounding to the same precision gives back
/// the number the author saw.
pub(crate) fn number(f: f64) -> String {
    if !f.is_finite() {
        return f.to_string();
    }
    if f.fract() == 0.0 && f.abs() < 1e15 {
        return format!("{}", f as i64);
    }
    let rounded: f64 = format!("{f:.14e}").parse().unwrap_or(f);
    let s = rounded.to_string();
    if s == "-0" { "0".to_string() } else { s }
}

/// A date, time or both, in ISO 8601. A serial under one day has no date
/// part, which is how Excel stores a time on its own. Durations (`[h]:mm`)
/// are written as hours, minutes and seconds, since they are not a point in
/// time.
pub(crate) fn date(d: &ExcelDateTime) -> String {
    let serial = d.as_f64();
    if d.is_duration() || (0.0..1.0).contains(&serial) {
        let total = (serial * 86_400.0).round() as i64;
        let sign = if total < 0 { "-" } else { "" };
        let total = total.abs();
        return format!(
            "{sign}{:02}:{:02}:{:02}",
            total / 3600,
            total / 60 % 60,
            total % 60
        );
    }
    let (y, mo, day, h, mi, s, ms) = d.to_ymd_hms_milli();
    if (h, mi, s, ms) == (0, 0, 0, 0) {
        format!("{y:04}-{mo:02}-{day:02}")
    } else if s == 0 && ms == 0 {
        format!("{y:04}-{mo:02}-{day:02} {h:02}:{mi:02}")
    } else {
        format!("{y:04}-{mo:02}-{day:02} {h:02}:{mi:02}:{s:02}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use calamine::ExcelDateTimeType;

    #[test]
    fn numbers_read_as_typed() {
        assert_eq!(number(3.0), "3");
        assert_eq!(number(0.1 + 0.2), "0.3");
        assert_eq!(number(3.0000000001), "3.0000000001");
        assert_eq!(number(-12.5), "-12.5");
        assert_eq!(number(1e20), "100000000000000000000");
    }

    #[test]
    fn dates_are_iso() {
        let at = |v| ExcelDateTime::new(v, ExcelDateTimeType::DateTime, false);
        assert_eq!(date(&at(45_356.0)), "2024-03-05");
        assert_eq!(date(&at(45_356.5)), "2024-03-05 12:00");
        assert_eq!(date(&at(0.75)), "18:00:00");
    }

    #[test]
    fn long_sheets_are_capped() {
        let mut rows = vec![vec!["n".to_string()]];
        rows.extend((0..MAX_ROWS + 5).map(|i| vec![i.to_string()]));
        let table = sheet_table(rows).unwrap();
        assert!(table.ends_with("5 more rows were left out of this sheet."));
    }
}
