"""What every handler takes: a database session and the person asking."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.auth import current_user
from opennotebook.db.models import User
from opennotebook.db.session import db
from opennotebook.errors import Problem

Db = Annotated[AsyncSession, Depends(db)]
Me = Annotated[User, Depends(current_user)]


def not_yet() -> Problem:
    """For a route whose contract is published but whose work is still being
    moved over from the Rust server."""
    return Problem(501, "This is not available in the new studio yet.")
