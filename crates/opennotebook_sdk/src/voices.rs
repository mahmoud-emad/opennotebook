//! Who a speaker is called when nobody named them.
//!
//! A session's speakers used to reach the screen as "Host" and "Expert": role
//! words from the settings defaults, shown beside every line as if they were
//! names. A listener hears a person, so the page shows one. Every Kokoro voice
//! id already carries a first name (`af_bella`, `bm_george`), and a speaker left
//! with a role word for a name is called by their voice's name instead. A name
//! somebody actually chose is kept as it is.
//!
//! One place for the rule, because three surfaces show speakers — the build,
//! the voice answers and the player — and they must agree on who is talking.

/// Role words that are not names.
const GENERIC: &[&str] = &[
    "host",
    "expert",
    "narrator",
    "speaker",
    "speaker 1",
    "speaker 2",
    "voice",
    "presenter",
    "guest",
    "studio",
];

/// The first name a Kokoro voice id carries: `af_bella` is Bella. Empty for an
/// id that does not have one.
pub fn voice_name(voice_id: &str) -> String {
    let name = voice_id.split_once('_').map_or("", |(_, n)| n);
    let mut c = name.chars();
    match c.next() {
        Some(f) => f.to_uppercase().chain(c).collect(),
        None => String::new(),
    }
}

/// True for an empty name or a role word standing in for one.
pub fn is_generic(name: &str) -> bool {
    let n = name.trim().to_lowercase();
    n.is_empty() || GENERIC.contains(&n.as_str())
}

/// What to call a speaker: their own name, or their voice's when they have
/// only a role word. Falls back to what was given when the voice has no name.
pub fn display_name(name: &str, voice_id: &str) -> String {
    if is_generic(name) {
        let v = voice_name(voice_id);
        if !v.is_empty() {
            return v;
        }
    }
    name.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_role_word_becomes_the_voices_name_and_a_real_name_stays() {
        assert_eq!(display_name("Host", "af_bella"), "Bella");
        assert_eq!(display_name("expert", "am_adam"), "Adam");
        assert_eq!(display_name("", "bm_george"), "George");
        assert_eq!(display_name("Dr. Rana", "af_bella"), "Dr. Rana");
        // No name in the voice id: keep what there is.
        assert_eq!(display_name("Host", "custom"), "Host");
    }
}
