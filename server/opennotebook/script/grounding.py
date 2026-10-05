"""The parts of the sources that are about a question. A port of the excerpt
half of `opennotebook_script/src/grounding.rs`; its `retrieve`, which reads
the index and the extracted questions and answers, comes with the script.

The script is written from what was added, not from what the model already
knows, and every reader here (ask, study notes, the mind map past its size
limit) picks its material with these functions.
"""

# The size an excerpt grows to before a new one starts.
EXCERPT_CHARS = 900

# Words every sentence has, which say nothing about what a passage is about.
COMMON = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "how",
        "what",
        "are",
        "its",
        "from",
        "into",
        "that",
        "this",
        "why",
        "when",
        "does",
        "their",
        "your",
        "about",
    }
)


def terms(query: str) -> list[str]:
    """The words of a query worth matching: lowercased, three letters or
    more, and not one of the words every sentence has. Sorted, each once."""
    words: list[str] = []
    word: list[str] = []
    for c in [*query, " "]:
        if c.isalnum() or c == "_":
            word.append(c)
            continue
        if word:
            words.append("".join(word).lower())
            word = []
    return sorted({w for w in words if len(w) >= 3 and w not in COMMON})


def is_prose(para: str) -> bool:
    """Whether a paragraph reads as prose rather than as a menu.

    A site's navigation converts to Markdown as runs of one- and two-word
    paragraphs ("Courses", "Tutorials", "Interview Prep"). A heading is kept,
    because it names what follows; a lone short line is not.
    """
    if para.startswith("#"):
        return True
    return len(para.split()) >= 8


def pieces_of(doc: str) -> list[str]:
    """A document as paragraph-sized pieces of prose, with navigation left
    out."""
    out: list[str] = []
    current = ""
    for para in (p.strip() for p in doc.split("\n\n")):
        if not para or not is_prose(para):
            continue
        if current and len(current) + len(para) > EXCERPT_CHARS:
            out.append(current)
            current = ""
        if current:
            current += "\n\n"
        current += para
    if current:
        out.append(current)
    return out


def excerpts_from(documents: list[str], query: str, keep: int) -> list[tuple[int, str]]:
    """The parts of `documents` that are about `query`, with the index of the
    document each came from, so an answer can cite the source a passage was
    read in.

    A passage can still carry navigation and boilerplate from a web page, and
    grounding that sends all of it makes a call expensive and its answer
    vague, the relevant sentences buried in menus.

    So each document is cut into paragraph-sized pieces, pieces that look like
    navigation are dropped, and the pieces sharing the most words with the
    query are kept — at most `keep` in all, in document order. A document with
    nothing that matches contributes nothing. When no piece matches at all,
    the first pieces of the first document stand in, so a query phrased
    differently from the source still grounds on something.
    """
    words = terms(query)
    # (score, document rank, position, text)
    pieces: list[tuple[int, int, int, str]] = []
    for rank, doc in enumerate(documents):
        for pos, piece in enumerate(pieces_of(doc)):
            lower = piece.lower()
            pieces.append((sum(1 for t in words if t in lower), rank, pos, piece))
    best = max((p[0] for p in pieces), default=0)
    if best == 0:
        chosen = [p for p in pieces if p[1] == 0][:keep]
    else:
        # Highest score first; earlier documents and earlier pieces win ties.
        chosen = sorted((p for p in pieces if p[0] > 0), key=lambda p: (-p[0], p[1], p[2]))[:keep]
    chosen.sort(key=lambda p: (p[1], p[2]))
    return [(p[1], p[3]) for p in chosen]


def excerpts(documents: list[str], query: str, keep: int) -> list[str]:
    """`excerpts_from` without the document each piece came from."""
    return [text for _, text in excerpts_from(documents, query, keep)]


def passages(documents: list[str]) -> list[tuple[int, str]]:
    """Every prose passage of every document, in order, with the index of the
    document it came from. What a reader that cites the WHOLE of the sources
    numbers, where `excerpts_from` keeps only the pieces about one question."""
    return [(i, p) for i, d in enumerate(documents) for p in pieces_of(d)]
