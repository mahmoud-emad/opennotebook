"""Sources: add pages, notes or files to a collection, list them, read one,
remove one; search the web; research a topic."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, File, Response, UploadFile
from pydantic import BaseModel, Field, HttpUrl, computed_field
from sqlalchemy import Select, delete, select
from sqlalchemy.orm import defer

from opennotebook import jobs, research, storage
from opennotebook.ai import ledger
from opennotebook.ai.errors import AiError
from opennotebook.api import paging
from opennotebook.api.deps import Db, Me
from opennotebook.api.sessions import JobOut
from opennotebook.db.models import Source
from opennotebook.db.session import release
from opennotebook.domain import collections, refresh, sources
from opennotebook.domain import settings as config
from opennotebook.errors import Problem, not_found
from opennotebook.jobs.app import WORK_QUEUE
from opennotebook.jobs.tasks import RESEARCH_TASK
from opennotebook.script.errors import problem

router = APIRouter(prefix="/api", tags=["sources"])


class SourceOut(BaseModel):
    name: str = Field(description="Stable within its collection; address the source by it")
    kind: Literal["url", "text", "file", "research"]
    title: str
    url: str = Field(description="Where it was read from; empty for a note, a file or a report")
    chars: int = Field(description="Length of the readable text")
    created_at: datetime

    @computed_field(description="About how many words it holds")
    @property
    def words(self) -> int:
        return sources.described(self.name, self.url, self.chars)[0]

    @computed_field(
        description="The line under its name: where it came from and how long it is, "
        "`example.org · 1200 words` or `PDF · 300 words`"
    )
    @property
    def detail(self) -> str:
        return sources.described(self.name, self.url, self.chars)[1]

    @computed_field(
        description="The picture to lead its row with: its site's icon, loaded from the site "
        "itself; empty for a note, a file or a report"
    )
    @property
    def icon(self) -> str:
        return sources.described(self.name, self.url, self.chars)[2]

    @classmethod
    def of(cls, src: Source) -> SourceOut:
        return cls.model_validate(src, from_attributes=True)


class SourceText(SourceOut):
    text: str = Field(description="The source verbatim, as Markdown")


class AddResult(BaseModel):
    """What happened to one thing asked to be added."""

    url: str = Field(description="The page asked for; empty for a note or a file")
    ok: bool
    source: SourceOut | None = None
    error: str = Field(description="Why it was not added, in a sentence; empty when ok")


class AddUrls(BaseModel):
    kind: Literal["urls"]
    urls: list[HttpUrl] = Field(min_length=1, max_length=8)


class AddNote(BaseModel):
    kind: Literal["text"]
    text: str = Field(max_length=2_000_000, description="The note, in plain text or Markdown")
    title: str = Field(default="", max_length=200)


class WebHit(BaseModel):
    title: str
    url: str
    snippet: str


class SearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=400)


class ResearchReq(BaseModel):
    topic: str = Field(min_length=1, max_length=400)


# How many sources a page of the list holds unless asked for fewer, and at
# most.
LIST_DEFAULT = 500
LIST_MAX = 1000


@router.get("/collections/{cid}/sources")
async def list_sources(
    cid: uuid.UUID,
    s: Db,
    me: Me,
    response: Response,
    limit: Annotated[int, paging.limit(LIST_DEFAULT, LIST_MAX, "sources")] = LIST_DEFAULT,
    offset: paging.Offset = 0,
) -> list[SourceOut]:
    """The sources in a collection, in the order they were added, a page at
    a time: `X-Next-Offset` says where the next page starts when there is
    one."""
    await collections.owned(s, me.id, cid)
    rows = await s.scalars(
        listed()
        .where(Source.collection_id == cid, Source.owner_id == me.id)
        .order_by(Source.created_at, Source.id)
        .limit(limit + 1)
        .offset(offset)
    )
    return [SourceOut.of(r) for r in paging.cut(list(rows), limit, offset, response)]


def listed() -> Select[Source]:
    """Sources as a list shows them: everything but their text, which a list
    never shows and which can be megabytes each."""
    return select(Source).options(defer(Source.text, raiseload=True))


@router.post("/collections/{cid}/sources", status_code=201)
async def add_sources(
    cid: uuid.UUID, body: Annotated[AddNote | AddUrls, Field(discriminator="kind")], s: Db, me: Me
) -> list[AddResult]:
    """Add a note, or read web pages, into a collection. One result per thing
    asked for, failures included, each saying why."""
    await collections.editable(s, me.id, cid)
    # Pages are read and text is embedded with no transaction open; each
    # source is then kept in a short one under the collection's lock.
    await release(s)
    if isinstance(body, AddNote):
        try:
            ready = await sources.prepare_note(body.text, body.title)
        except sources.Refused as e:
            raise Problem(422, str(e)) from e
        src = await sources.keep(s, me.id, cid, ready)
        await refresh.schedule(s, me.id, cid)
        return [AddResult(url="", ok=True, source=SourceOut.of(src), error="")]
    out: list[AddResult] = []
    async with sources.http_client() as http:
        for u in dict.fromkeys(str(u) for u in body.urls):
            try:
                ready = await sources.prepare_page(http, u)
            except sources.Refused as e:
                out.append(AddResult(url=u, ok=False, error=str(e)))
                continue
            src = await sources.keep(s, me.id, cid, ready)
            # Committed before the next page is read, so the lock is not
            # held while it is.
            await release(s)
            out.append(AddResult(url=u, ok=True, source=SourceOut.of(src), error=""))
    if any(r.ok for r in out):
        await refresh.schedule(s, me.id, cid)
    return out


@router.post("/collections/{cid}/sources/files", status_code=201)
async def add_files(
    cid: uuid.UUID,
    s: Db,
    me: Me,
    files: Annotated[list[UploadFile], File(description="pdf, docx, pptx, xlsx, md, txt or csv")],
) -> list[AddResult]:
    """Add documents to a collection, at most ten at a time. Each is read
    into text verbatim and titled by its file name; a result says why when
    one could not be read."""
    if len(files) > sources.MAX_FILES:
        raise Problem(
            422,
            f"That is {len(files)} files; at most {sources.MAX_FILES} can be added at once. "
            "Add the rest in another go.",
        )
    await collections.editable(s, me.id, cid)
    await release(s)
    out: list[AddResult] = []
    for f in files:
        name = f.filename or "document"
        data = await f.read(sources.MAX_UPLOAD_BYTES + 1)
        try:
            ready = await sources.prepare_file(name, data)
        except sources.Refused as e:
            out.append(AddResult(url="", ok=False, error=str(e)))
            continue
        src = await sources.keep(s, me.id, cid, ready)
        await release(s)
        out.append(AddResult(url="", ok=True, source=SourceOut.of(src), error=""))
    if any(r.ok for r in out):
        await refresh.schedule(s, me.id, cid)
    return out


@router.get("/collections/{cid}/sources/{name}")
async def read_source(cid: uuid.UUID, name: str, s: Db, me: Me) -> SourceText:
    """One source with its text, for reading it beside a citation."""
    src = await s.scalar(
        select(Source).where(
            Source.collection_id == cid, Source.owner_id == me.id, Source.name == name
        )
    )
    if src is None:
        raise not_found("That source")
    return SourceText.model_validate(src, from_attributes=True)


@router.delete("/collections/{cid}/sources/{name}", status_code=204)
async def remove_source(cid: uuid.UUID, name: str, s: Db, me: Me, after: BackgroundTasks) -> None:
    """Remove one source from a collection."""
    await collections.refuse_read_only(s, await collections.lock(s, me.id, cid))
    gone = (
        await s.execute(
            delete(Source)
            .where(Source.collection_id == cid, Source.owner_id == me.id, Source.name == name)
            .returning(Source.file_path)
        )
    ).first()
    if gone is None:
        raise not_found("That source")
    if gone.file_path:
        after.add_task(storage.remove_file, gone.file_path)
    await collections.touch(s, cid)
    await refresh.schedule(s, me.id, cid)


@router.post("/search")
async def web_search(body: SearchReq, s: Db, me: Me) -> list[WebHit]:
    """Search the web. Nothing is added: pass the best links to add_sources.
    A few seconds; charged to the person like any model call."""
    query = body.query.strip()
    if not query:
        raise Problem(422, "The search is empty. Write what to look for, then search again.")
    model = await config.value(s, me.id, config.SEARCH_MODEL_KEY)
    await release(s)
    try:
        async with ledger.spending(me.id, "web_search"):
            hits = await research.web_search(model, query)
    except AiError as e:
        raise problem(e) from e
    return [WebHit(title=h.title, url=h.url, snippet=h.snippet) for h in hits]


@router.post("/collections/{cid}/research", status_code=202)
async def deep_research(cid: uuid.UUID, body: ResearchReq, s: Db, me: Me) -> JobOut:
    """Research a topic in depth and add the written report as one source.
    Runs in the background, minutes; follow it on /api/jobs/{job_id}."""
    await collections.editable(s, me.id, cid)
    job = await jobs.create(s, me.id, "research", collection_id=cid, steps_total=1)
    args = {"job_id": str(job.id), "owner_id": str(me.id), "collection_id": str(cid)}
    await jobs.defer(s, job, RESEARCH_TASK, {**args, "topic": body.topic.strip()}, queue=WORK_QUEUE)
    return JobOut.of(job)
