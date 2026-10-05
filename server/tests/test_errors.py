"""The wording rules every error goes through, ported from ui/src/errors.rs."""

import pytest

from opennotebook.errors import readable, tidy, validation_sentence


def test_out_of_credit_is_said_in_words_with_what_to_do() -> None:
    e = (
        "the sources could not be asked: the AI account is out of credit: HTTP 402: "
        '{"error":{"message": "Insufficient credits. Add more using '
        'https://openrouter.ai/settings/credits","type":"insufficient_quota"}}'
    )
    m = readable(e)
    assert m.startswith("The AI account is out of credit"), m
    # The tests call no real provider, so the sentence names none.
    assert "AI provider the studio uses" in m and "{" not in m
    # What the chat showed, cut in half, is still recognised.
    assert readable('credits","type":"insufficient_quota"}}') == m


def test_out_of_credit_names_openrouter_only_when_it_is_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opennotebook.config import settings

    monkeypatch.setenv("OPENNOTEBOOK_AI_BASE_URL", "https://openrouter.ai/api/v1")
    settings.cache_clear()
    try:
        assert "openrouter.ai/settings/credits" in readable("insufficient_quota")
    finally:
        monkeypatch.undo()
        settings.cache_clear()


@pytest.mark.parametrize(
    ("raw", "starts"),
    [
        ("the AI provider refused the key: HTTP 401: User not found", "The AI provider refused"),
        ("rate limited: HTTP 429: slow down", "The AI provider is busy"),
        ("not found (is the model id right?): HTTP 404", "The AI model chosen"),
        ("the AI provider is unavailable: HTTP 503", "The AI provider is not answering"),
        ("HTTP 500", "The studio ran into a problem"),
        ("HTTP 404", "That is no longer there"),
    ],
)
def test_the_common_failures_each_have_one_sentence(raw: str, starts: str) -> None:
    assert readable(raw).startswith(starts), readable(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "insufficient_quota",
        "invalid api key",
        "rate limited",
        "model not found",
        "upstream error",
        "HTTP 502",
        "HTTP 404",
        "HTTP 500",
        "map failed: the model said no",
    ],
)
def test_cleaning_twice_says_the_same(raw: str) -> None:
    once = readable(raw)
    assert readable(once) == once


def test_anything_else_is_tidied_not_reworded() -> None:
    assert readable("map failed: the model said no") == "Map failed: the model said no."
    assert readable('parse: {"error":{"message":"bad shape"}}') == "Parse: bad shape."
    assert readable('half: {"error":{"mess') == "Half."
    assert tidy("could not keep it: Os { code: 28, kind: StorageFull }") == "Could not keep it."
    t = tidy("word " * 100)
    assert len(t) <= 222 and t.endswith("…"), t
    assert tidy("   ") == "Something went wrong. Try again."


def test_a_refused_request_names_the_field() -> None:
    assert (
        validation_sentence([{"type": "missing", "loc": ("body", "text"), "msg": "Field required"}])
        == "Fill in text, then try again."
    )
    said = validation_sentence(
        [
            {
                "type": "string_too_long",
                "loc": ("body", "title"),
                "msg": "String should have at most 200 characters",
            }
        ]
    )
    assert said == "Title: string should have at most 200 characters.", said
