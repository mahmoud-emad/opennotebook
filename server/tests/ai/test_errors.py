"""Ported from opennotebook_ai/src/error.rs."""

import json

from opennotebook.ai.errors import AiError, Kind, message_of


def test_a_relayed_upstream_body_is_unwrapped_to_its_message() -> None:
    upstream = json.dumps(
        {
            "error": {
                "message": "You exceeded your current quota, please check your plan.",
                "type": "insufficient_quota",
                "code": "insufficient_quota",
            }
        }
    )
    body = json.dumps({"error": {"message": upstream, "code": 402}})
    e = AiError.from_status(402, body)
    assert e.kind == Kind.QUOTA
    assert e.detail == "HTTP 402: You exceeded your current quota, please check your plan."
    assert "{" not in str(e)
    assert e.sentence.startswith("The AI account is out of credit")


def test_raw_metadata_is_read_when_the_message_is_generic() -> None:
    raw = json.dumps({"error": {"message": "Insufficient credits on the account"}})
    body = json.dumps({"error": {"metadata": {"raw": raw}}})
    assert message_of(body) == "Insufficient credits on the account"


def test_out_of_credit_is_quota_whatever_status_it_came_with() -> None:
    inside_200 = {"message": "insufficient_quota", "code": "insufficient_quota"}
    assert AiError.from_body(inside_200).kind == Kind.QUOTA
    as_502 = json.dumps({"error": {"message": "Upstream: insufficient credits"}})
    e = AiError.from_status(502, as_502)
    assert e.kind == Kind.QUOTA and not e.retryable
    # A per-minute limit is still a rate limit, and retried.
    per_minute = json.dumps({"error": {"message": "Quota exceeded for requests per minute"}})
    e = AiError.from_status(429, per_minute)
    assert e.kind == Kind.RATE_LIMITED and e.retryable
    # A bad key stays a bad key.
    assert AiError.from_status(401, '{"error":{"message":"No auth"}}').kind == Kind.AUTH


def test_a_body_that_is_not_json_is_kept_short() -> None:
    assert str(AiError.from_status(503, "x" * 1000)).count("x") == 300


def test_each_kind_reads_as_a_sentence() -> None:
    for kind, starts in [
        (Kind.AUTH, "The AI provider refused"),
        (Kind.RATE_LIMITED, "The AI provider is busy"),
        (Kind.NOT_FOUND, "The AI model chosen"),
        (Kind.UNAVAILABLE, "The AI provider is not answering"),
    ]:
        assert AiError(kind, "HTTP 500: boom").sentence.startswith(starts)


def test_a_price_reads_as_the_settings_page_shows_it() -> None:
    from opennotebook.ai.prices import Price, hint

    assert hint(Price(0.000001, 0.000005)) == "$1 / $5 per M tokens"
    assert hint(Price(0.0000001, 0.0000004)) == "$0.1 / $0.4 per M tokens"
    assert hint(Price(0.00000002, 0.000015)) == "$0.02 / $15 per M tokens"
    assert hint(Price(0, 0)) == "free"
