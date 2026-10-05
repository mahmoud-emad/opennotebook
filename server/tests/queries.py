"""What the studio asks the database while a block runs: every statement, in
order, for tests that count queries or check which columns a read loads."""

import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event

from opennotebook.db.session import engine


class Recorded(list[str]):
    """The statements sent, as SQL text."""

    def selecting(self, table: str) -> list[str]:
        """The reads of `table`."""
        return [q for q in self if q.lstrip().upper().startswith("SELECT") and table in q]

    def loads(self, column: str) -> bool:
        """Whether any statement reads `column` (`table.column`) whole, not
        only through an expression such as `jsonb_array_length(…)` or
        `… -> 'key'`."""
        bare = re.compile(rf"(?<![(\w]){re.escape(column)}\b(?!\s*(->|\)))")
        return any(
            bare.search(q.split("FROM", 1)[0])
            for q in self
            if q.lstrip().upper().startswith("SELECT")
        )


@contextmanager
def recorded() -> Generator[Recorded]:
    """Every statement the studio's engine sends inside the block."""
    out = Recorded()

    def before(_c: Any, _cur: Any, statement: str, *_: Any) -> None:
        out.append(statement)

    sync = engine().sync_engine
    event.listen(sync, "before_cursor_execute", before)
    try:
        yield out
    finally:
        event.remove(sync, "before_cursor_execute", before)
