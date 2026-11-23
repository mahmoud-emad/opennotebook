"""Lists a page at a time. A list answers with its usual array, cut to
`limit`; when there is more after it, a header says where the next page
starts, so a client that reads only the first page sees exactly what it
always did, up to the limit.

* `X-Next-Offset`: the `offset` to ask for next, for lists in a fixed order.
* `X-Next-Before`: the `before` to ask for next, for a conversation read
  from its newest message back.
"""

from typing import Annotated

from fastapi import Query, Response

NEXT_OFFSET = "X-Next-Offset"
NEXT_BEFORE = "X-Next-Before"

# The furthest a page may start: past this, narrow the list instead.
OFFSET_MAX = 100_000

Offset = Annotated[
    int,
    Query(
        ge=0,
        le=OFFSET_MAX,
        description=f"Where this page starts: the `{NEXT_OFFSET}` header of the page before",
    ),
]


def limit(default: int, most: int, what: str) -> object:
    """A `limit` parameter: at most `most` of `what` in one answer."""
    return Query(ge=1, le=most, description=f"The most {what} in one answer; {default} unless set")


def cut[T](rows: list[T], limit: int, offset: int, response: Response) -> list[T]:
    """A page from rows read with `limit + 1`: the first `limit` of them, and
    the header that says where the next page starts when there is one."""
    if len(rows) > limit:
        response.headers[NEXT_OFFSET] = str(offset + limit)
    return rows[:limit]
