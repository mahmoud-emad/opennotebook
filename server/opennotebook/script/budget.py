"""Hard character budgets, enforced on what is emitted rather than requested.
A port of `opennotebook_script/src/budget.rs`.

Section 3 requires these because an earlier slide service reported
`status: succeeded` and `ok: true` on a slide whose title overflowed its box,
whose accent rule struck through a word, and which still carried the literal
placeholder `SUBHEAD`. `ok` is a liveness signal, not a quality one, so the
defence has to be upstream, and this module is the upstream.

Asking a model for "about 60 characters" is a request, not a guarantee. The
budget is applied to the string on its way out.
"""

# A slide title. Short enough not to wrap in the 980 pixel box the measured
# overflow came from.
TITLE = 60

# One narration line as spoken.
LINE = 320

# Total narration per slide. Kokoro was measured at 84 words in 23.22 s, so
# about 3.6 words per second; section 3 budgets roughly 30 s of audio per
# slide, which is about 108 words. At an English average near six characters
# per word that is close to 650.
SLIDE_NARRATION = 650

# Characters of narration Kokoro speaks per second, measured on the lab box on
# 2026-10-01 across eight built sessions: 7.2 minutes of audio at 17.2
# characters a second, no session below 16.4. Rounded down, so a session asked
# for N minutes comes out at N or a little over rather than short.
CHARS_PER_SECOND = 17

# A per-slide budget above this is written one slide per call.
#
# The whole-session call asks one reply for every slide's narration. Past
# about 1,500 characters a slide that reply runs toward the script model's
# output ceiling, and a truncated reply loses whole slides to the per-slide
# fallback anyway — so a long session goes straight there, where each call
# writes one slide and was measured to reach a 4,800 character target.
WHOLE_SESSION_MAX_SLIDE = 1_500

# The least room worth giving a line.
#
# A budget spent down to its last few characters used to emit the fragment
# that fitted: a real run ended a slide on the spoken line "Moshi util". The
# line is not shortened at that point, it is destroyed, and it is then
# synthesised and played. Below this, the line is dropped instead — about a
# second and a half of speech is the floor for something worth saying.
MIN_LINE = 40

# Total narration for a bookend — the welcome in front of the first slide, or
# the close behind the last.
#
# Its own budget rather than a share of the slide's, because a first slide
# legitimately runs longer than a middle one: it carries a welcome as well as
# its material. 320 is one `LINE`, about twelve seconds of Kokoro at the
# measured 3.6 words a second. A welcome is a breath, not a preamble.
BOOKEND_NARRATION = 320

# The longest one on-slide element may be.
#
# Short on purpose. A slide is read at a glance while something else is being
# said, so an element is a few words, not a sentence — and the renderer has to
# fit it in a box whose size the theme owns. Measured against the failure this
# replaces: narration lines of up to 320 characters were being sent as slide
# copy and arrived as text overflowing its box (phase 1 spec, section 3).
ELEMENT = 72

# How many elements one slide may carry beside its title.
#
# Four. A slide that lists six things is a document, and the narration is
# already carrying the detail — the printed copy is the anchor, not the record.
SLIDE_ELEMENTS = 4


def slide_narration(minutes: int, slides: int) -> int:
    """The narration budget per slide for a session of `minutes` over
    `slides`.

    The welcome and the close have their own budget (`BOOKEND_NARRATION`
    each), so they are taken off first. Never below `SLIDE_NARRATION`, which
    is what a slide had before the length was a setting: a short session over
    many slides keeps the depth a slide always had.
    """
    total = minutes * 60 * CHARS_PER_SECOND
    body = max(total - 2 * BOOKEND_NARRATION, 0)
    return max(body // max(slides, 1), SLIDE_NARRATION)


def room(spent: int, total: int) -> int | None:
    """How much of a budget the next line may have, or None when what is
    left is too small to say anything in.

    The whole point is the None. Spending a budget down to its last few
    characters used to emit whatever fitted, and a real run ended a slide on
    the spoken line "Moshi util" — not a shortened line, a destroyed one,
    synthesised and played. This says "drop it" instead. It caps at `LINE` on
    the way past, so a caller has one rule rather than two.
    """
    left = min(max(total - spent, 0), LINE)
    return left if left >= MIN_LINE else None


def fit(text: str, max_chars: int) -> str:
    """Trim to `max_chars` at a word boundary, or return the string unchanged.

    For text that is read, not spoken: titles, on-slide elements, map labels.
    Narration goes through `fit_spoken`, which never ends mid-sentence.

    Truncation is visible in the result rather than silent: the caller
    compares lengths to know it happened. There is no ellipsis, because this
    text can be spoken and an ellipsis is not a sound.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    cut = max((i for i, c in enumerate(truncated) if c.isspace()), default=-1)
    if cut > max_chars // 2:
        return truncated[:cut].rstrip()
    return truncated.rstrip()


def _followed_by_space(head: str, k: int) -> bool:
    return k + 1 < len(head) and head[k + 1].isspace()


def fit_spoken(text: str, max_chars: int) -> str:
    """Trim a SPOKEN line to `max_chars` at a sentence boundary, or return it
    unchanged.

    Whole sentences, never a word boundary. Cutting at the last word that
    fits produced a spoken line ending "…making this session an" —
    synthesised, played, and heard as the narrator stopping mid-thought. A
    line that is too long now loses its last sentences instead, so what is
    said is finished.

    When not even the first sentence fits, the cut falls at the last clause
    break (`,` `;` `:`) and becomes a full stop; failing that, the last word,
    also closed with a full stop. One character is kept back for that stop,
    so the budget holds either way.
    """
    text = text.strip()
    if len(text) <= max_chars or max_chars < 2:
        return fit(text, max_chars)
    head = text[:max_chars]
    sentence_end: int | None = None
    clause_end: int | None = None
    for k, c in enumerate(head):
        if not _followed_by_space(head, k):
            continue
        if c in ".!?":
            sentence_end = k + 1
        elif c in ",;:":
            clause_end = k
    if sentence_end is not None:
        return head[:sentence_end].rstrip()
    # Room for the full stop that closes it.
    head = text[: max_chars - 1]
    cut: int | None = None
    if clause_end is not None and max_chars // 3 < clause_end < len(head):
        cut = clause_end
    else:
        space = max((i for i, c in enumerate(head) if c.isspace()), default=-1)
        if space > max_chars // 3:
            cut = space
    if cut is None:
        cut = len(head)
    body = head[:cut].rstrip().rstrip(",;:-—").rstrip()
    return f"{body}."


def whole_sentences(text: str, max_chars: int) -> str | None:
    """The whole sentences of a spoken line that fit in `max_chars`, or None
    when not even its first sentence does.

    `fit_spoken` always returns something, and when no sentence fits it
    closes a clause with a full stop it made up. That is how a real session
    said "How does the 'Inner Monologue' method contribute to the." — a
    question with its object cut off, said aloud and then never answered. In
    a conversation a line that does not fit is better dropped than mangled,
    so the script uses this.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text or None
    head = text[:max_chars]
    end: int | None = None
    for k, c in enumerate(head):
        if c in ".!?" and _followed_by_space(head, k):
            end = k + 1
    return None if end is None else head[:end].rstrip()


def exceeds(text: str, max_chars: int) -> bool:
    """True when `fit` would shorten this string. Lets a caller report a
    budget breach rather than discover a short line later."""
    return len(text.strip()) > max_chars


def speakable(text: str) -> str:
    """The same words, punctuated so the voice actually pauses where the
    writing says to.

    A dash is a held beat in writing, and the script model uses it
    constantly: "what Moshi is — let's break it down". Kokoro does not hear
    it. Sent as written, the two halves run together into one breathless
    phrase, which is the opposite of what the dash was for.

    Measured against `af_bella`, same words each time: only a full stop buys
    a real hold — about 280 ms — and only when the next word is capitalised.
    A lowercase continuation after the period is worth nothing at all
    (1.884 s against a 1.886 s baseline). That one detail is the whole
    implementation, and it is why this is not a one-line `replace`.

    Ranges are left alone. An en dash between digits — `2014–2015`, `pages
    3–7` — is not a pause, and turning it into a sentence break would say
    "two thousand fourteen. Two thousand fifteen."
    """
    chars = list(text)
    out: list[str] = []
    i = 0
    while i < len(chars):
        c = chars[i]
        if c not in ("—", "–"):
            out.append(c)
            i += 1
            continue
        # A dash with digits hard against both sides is a range, not a beat.
        so_far = "".join(out)
        prev = next((p for p in reversed(so_far) if not p.isspace()), None)
        nxt = next((n for n in chars[i + 1 :] if not n.isspace()), None)
        tight = (
            i > 0
            and chars[i - 1].isascii()
            and chars[i - 1].isdigit()
            and i + 1 < len(chars)
            and chars[i + 1].isascii()
            and chars[i + 1].isdigit()
        )
        if tight or (
            prev is not None
            and prev.isascii()
            and prev.isdigit()
            and nxt is not None
            and nxt.isascii()
            and nxt.isdigit()
        ):
            out.append(c)
            i += 1
            continue
        # Drop any space the dash was sitting on, end the sentence, and start
        # the next one — capitalised, or the stop is not heard.
        so_far = so_far.rstrip(" ")
        out = list(so_far)
        i += 1
        while i < len(chars) and chars[i].isspace():
            i += 1
        # Nothing to end yet: a line that opens on a dash just loses it.
        if not so_far.rstrip():
            continue
        if not so_far.endswith((".", "!", "?")):
            out.append(".")
        out.append(" ")
        if i < len(chars):
            out.append(chars[i].upper())
            i += 1
    return "".join(out)
