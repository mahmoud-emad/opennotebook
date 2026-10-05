"""What every handler takes: a database session and the person asking."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.auth import current_user
from opennotebook.db.models import User
from opennotebook.db.session import db

# Function scope: the session is committed when the handler returns, before
# the answer is sent, so a 2xx means the work is saved and what runs after
# the answer (removing files) runs only once it is.
Db = Annotated[AsyncSession, Depends(db, scope="function")]
Me = Annotated[User, Depends(current_user)]

# Headers on every HTML page the studio serves from its own origin (a cover,
# a slide): run as a sandbox with no scripts and an origin of its own, and
# never read as anything but what it says it is. A page opened directly is as
# harmless as one in the sandboxed frame the web app puts it in.
SANDBOXED = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
