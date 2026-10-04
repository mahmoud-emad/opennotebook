//! The design tokens both pages draw from: `assets/theme.css`.
//!
//! The app embeds it at compile time and the server puts it in the player, so
//! a colour changed here changes everywhere at once. Its tests read the sheet
//! itself and hold every pair of colours the app puts together to WCAG AA, in
//! both themes, so a token tweak that makes some text unreadable fails here
//! rather than in front of someone.

/// The token sheet: Bootstrap 5.3 names, dark by default, light under
/// `data-bs-theme="light"`.
pub const CSS: &str = include_str!("../assets/theme.css");

/// The localStorage key that holds the viewer's choice: "light", or "dark" /
/// absent for the default. One key, read by the app and the player.
pub const STORAGE_KEY: &str = "opennotebook.theme";

/// A script for a page's `<head>`: applies the stored choice before the first
/// paint, so a page never flashes the other theme while it loads.
pub const BOOT_SCRIPT: &str = r#"try{if(localStorage.getItem("opennotebook.theme")==="light")document.documentElement.setAttribute("data-bs-theme","light")}catch(e){}"#;

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    /// The `--name: value` pairs of the block that starts at `head`.
    fn block(head: &str) -> HashMap<String, String> {
        let start = CSS.find(head).unwrap_or_else(|| panic!("no block {head}")) + head.len();
        let body = &CSS[start..start + CSS[start..].find('}').unwrap()];
        body.split(';')
            .filter_map(|decl| {
                let decl = decl.trim();
                let decl = decl.rsplit("*/").next().unwrap_or(decl).trim();
                let (k, v) = decl.split_once(':')?;
                k.trim()
                    .starts_with("--")
                    .then(|| (k.trim().to_string(), v.trim().to_string()))
            })
            .collect()
    }

    fn dark() -> HashMap<String, String> {
        block(":root {")
    }
    fn light() -> HashMap<String, String> {
        block(":root[data-bs-theme=\"light\"] {")
    }

    fn lum(hex: &str) -> f64 {
        let h = hex.trim_start_matches('#');
        assert_eq!(h.len(), 6, "not a #rrggbb colour: {hex}");
        let v = u32::from_str_radix(h, 16).unwrap();
        let c = |s: u32| {
            let x = ((v >> s) & 0xff) as f64 / 255.0;
            if x <= 0.03928 {
                x / 12.92
            } else {
                ((x + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * c(16) + 0.7152 * c(8) + 0.0722 * c(0)
    }

    fn ratio(a: &str, b: &str) -> f64 {
        let (x, y) = (lum(a), lum(b));
        (x.max(y) + 0.05) / (x.min(y) + 0.05)
    }

    /// Every failing pair, so one run names all of them.
    fn check(t: &HashMap<String, String>, theme: &str, pairs: &[(&str, &str, f64)]) {
        let mut bad = Vec::new();
        for (fg, bg, min) in pairs {
            let (f, b) = (&t[*fg], &t[*bg]);
            let r = ratio(f, b);
            if r < *min {
                bad.push(format!(
                    "{theme}: {fg} {f} on {bg} {b} is {r:.2}:1, needs {min}"
                ));
            }
        }
        assert!(bad.is_empty(), "\n{}", bad.join("\n"));
    }

    fn pairs() -> Vec<(&'static str, &'static str, f64)> {
        let mut p = Vec::new();
        let surfaces = [
            "--bs-body-bg",
            "--bs-secondary-bg",
            "--bs-tertiary-bg",
            "--st-hover-bg",
            "--st-overlay-bg",
        ];
        for s in surfaces {
            for t in [
                "--bs-body-color",
                "--bs-emphasis-color",
                "--bs-secondary-color",
            ] {
                p.push((t, s, 4.5));
            }
        }
        // The quietest text is kept off the hover and overlay layers.
        for s in ["--bs-body-bg", "--bs-secondary-bg", "--bs-tertiary-bg"] {
            for t in [
                "--bs-tertiary-color",
                "--bs-primary-text-emphasis",
                "--bs-link-color",
                "--bs-success-text-emphasis",
                "--bs-danger-text-emphasis",
                "--bs-warning-text-emphasis",
            ] {
                p.push((t, s, 4.5));
            }
        }
        for s in ["--bs-primary", "--st-primary-hover", "--st-primary-pressed"] {
            p.push(("--st-on-primary", s, 4.5));
        }
        for d in 0..5 {
            let fill: &'static str = [
                "--st-mm-d0",
                "--st-mm-d1",
                "--st-mm-d2",
                "--st-mm-d3",
                "--st-mm-d4",
            ][d];
            let text: &'static str = [
                "--st-mm-t0",
                "--st-mm-t1",
                "--st-mm-t2",
                "--st-mm-t3",
                "--st-mm-t4",
            ][d];
            p.push((text, fill, 4.5));
        }
        // Non-text: a focus ring, the map's links and a voice's rule are seen,
        // not read (WCAG 1.4.11, 3:1).
        p.push(("--st-mm-link", "--bs-body-bg", 3.0));
        for s in ["--bs-body-bg", "--bs-secondary-bg"] {
            p.push(("--st-focus", s, 3.0));
        }
        for v in ["--st-voice-a", "--st-voice-b"] {
            p.push((v, "--bs-tertiary-bg", 3.0));
        }
        p
    }

    #[test]
    fn dark_reads_at_aa() {
        check(&dark(), "dark", &pairs());
    }

    #[test]
    fn light_reads_at_aa() {
        check(&light(), "light", &pairs());
    }

    /// Light is the viewer's choice only: nothing in the sheet follows the
    /// device's setting, so the studio looks the same until they change it.
    #[test]
    fn light_is_only_ever_chosen() {
        assert!(!CSS.contains("prefers-color-scheme"));
        assert_eq!(CSS.matches("[data-bs-theme=\"light\"]").count(), 1);
    }

    /// Every colour token a theme sets, the other sets too: a token only one
    /// theme defines would fall back to the dark value in light.
    #[test]
    fn both_themes_set_the_same_colours() {
        let d = dark();
        let l = light();
        let mut missing: Vec<&String> = d
            .keys()
            .filter(|k| {
                !k.starts_with("--bs-font")
                    && !k.starts_with("--bs-border-radius")
                    && !k.starts_with("--st-dur")
                    && !k.starts_with("--st-ease")
            })
            .filter(|k| !l.contains_key(*k))
            .collect();
        missing.sort();
        assert!(missing.is_empty(), "light does not set: {missing:?}");
        assert!(
            l.keys().all(|k| d.contains_key(k)),
            "light sets a token dark does not"
        );
    }

    #[test]
    fn the_boot_script_reads_the_same_key() {
        assert!(BOOT_SCRIPT.contains(STORAGE_KEY));
    }
}
