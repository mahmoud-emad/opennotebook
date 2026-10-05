"""DEPRECATED: the old JSON-RPC 2.0 API, kept for one release after cutover.

`POST /api/{session,sources,mindmap,notes,settings}/rpc` answer the 47
methods of the Rust server's `oschema/` with the same framing it had
(`crates/opennotebook_api/src/jsonrpc.rs`), so agents written against it
keep working while they move to REST. Each method forwards to the REST
handler that does the same work (`rpc_methods.py`). Every response carries a
`Deprecation` header. The release after cutover deletes this file,
`rpc_methods.py`, `rpc_openrpc/` and the line in `main.py` that includes the
router (docs/stack-migration-plan.md §4).

The wire, as before: `{"jsonrpc": "2.0", "id": 1, "method": "notes_get",
"params": {"req": {...}}}`. Params are an object keyed by parameter name (an
array is taken in the parameters' order); a method with none takes `{}`,
`null` or no params. A request without an `id` (or with a null one) is a
notification and gets no answer; a body of only notifications is a 204. A
batch holds 1 to 100 requests. An error is HTTP 200 with `{"error": {"code",
"message", "data"}}`, and its message is a sentence a person can act on.

Codes: -32700 the body is not JSON; -32600 not a JSON-RPC 2.0 request;
-32601 no such method; -32602 the params, or what they ask for, cannot be
done (`data.status` carries the HTTP status REST answers with); -32603 the
studio or a service it calls failed.

Sign-in is REST's: an API key, or nothing in local mode.
"""

import json
import logging
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from opennotebook.api.rpc_methods import METHODS, Ctx, Method
from opennotebook.auth import current_user
from opennotebook.db.models import User
from opennotebook.db.session import sessionmaker
from opennotebook.errors import SERVER_FAULT, Problem, validation_sentence

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["rpc"], include_in_schema=False)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

BATCH_MAX = 100

# What every answer says about this API: it is going away, and what replaces it.
DEPRECATED = {"Deprecation": "true", "Link": '</docs>; rel="successor-version"'}

DOCS = Path(__file__).with_name("rpc_openrpc")

# HTTP statuses that mean the caller asked for something that cannot be
# done; any other failure is the studio's or a service's.
_CALLERS = {400, 403, 404, 409, 410, 413, 422}


@cache
def openrpc(domain: str) -> dict[str, Any]:
    """The domain's OpenRPC document, as the Rust build generated it from
    `oschema/`."""
    doc: dict[str, Any] = json.loads((DOCS / f"{domain}.json").read_text())
    return doc


class RpcError(Exception):
    def __init__(self, code: int, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_json(self) -> dict[str, Any]:
        e: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.status is not None:
            e["data"] = {"status": self.status}
        return e


def _error(id_: Any, e: RpcError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": e.as_json()}


def _invalid(e: ValidationError) -> RpcError:
    said = validation_sentence([dict(x) for x in e.errors()])
    if not said.endswith((".", "…", "?", "!")):
        said += "."
    return RpcError(INVALID_PARAMS, said, 422)


def _params(m: Method, raw: Any) -> Any:
    """The method's params as its input type; `null` is no params, and an
    array is taken in the parameters' order."""
    if raw is None:
        raw = {}
    elif isinstance(raw, list):
        raw = dict(zip(m.params.model_fields, raw, strict=False))
    if not isinstance(raw, dict):
        raise RpcError(
            INVALID_PARAMS,
            'Params must be an object keyed by parameter name, such as {"sid": "…"}.',
        )
    try:
        return m.params.model_validate(raw)
    except ValidationError as e:
        raise _invalid(e) from e


def _not_found(domain: str, name: str) -> RpcError:
    elsewhere = next((d for d, ms in METHODS.items() if name in ms), None)
    where = (
        f" It is served at /api/{elsewhere}/rpc."
        if elsewhere
        else " Call rpc.discover for the methods there are."
    )
    return RpcError(METHOD_NOT_FOUND, f"There is no method named {name!r} here.{where}")


async def _call(m: Method, params: Any, me: User, tasks: BackgroundTasks) -> Any:
    """One method in a transaction of its own, so one failed call in a batch
    undoes only itself."""
    async with sessionmaker()() as s:
        try:
            out = await m.run(Ctx(s, me, tasks), params)
            await s.commit()
            return out
        except BaseException:
            await s.rollback()
            raise


async def _outcome(domain: str, name: str, raw: Any, me: User, tasks: BackgroundTasks) -> Any:
    doc = openrpc(domain)
    if name == "rpc.health":
        return {"status": "ok", "service": doc["info"]["title"], "version": doc["info"]["version"]}
    if name == "rpc.discover":
        return doc
    m = METHODS[domain].get(name)
    if m is None:
        raise _not_found(domain, name)
    params = _params(m, raw)
    try:
        return await _call(m, params, me, tasks)
    except Problem as e:
        code = INVALID_PARAMS if e.status in _CALLERS else INTERNAL_ERROR
        raise RpcError(code, e.detail, e.status) from e
    except ValidationError as e:
        # A REST body the method built from the params refused them.
        raise _invalid(e) from e
    except Exception as e:
        log.exception("rpc %s failed", name, exc_info=e)
        raise RpcError(INTERNAL_ERROR, SERVER_FAULT, 500) from e


async def _one(domain: str, req: Any, me: User, tasks: BackgroundTasks) -> dict[str, Any] | None:
    """Answer one request; None for a notification."""
    if not isinstance(req, dict) or not isinstance(req.get("method"), str):
        return _error(
            None,
            RpcError(
                INVALID_REQUEST,
                'Each request is a JSON object with jsonrpc "2.0", a method, and an id '
                "unless it is a notification.",
            ),
        )
    id_: Any = req.get("id")
    if req.get("jsonrpc") != "2.0":
        return _error(id_, RpcError(INVALID_REQUEST, 'Send jsonrpc "2.0" with every request.'))
    try:
        result = await _outcome(domain, req["method"], req.get("params"), me, tasks)
        answer = {"jsonrpc": "2.0", "id": id_, "result": result}
    except RpcError as e:
        answer = _error(id_, e)
    return None if id_ is None else answer


async def handle(domain: str, body: bytes, me: User, tasks: BackgroundTasks) -> Any:
    """Answer a request body: one request or a batch. None when there is
    nothing to send back (only notifications)."""
    try:
        parsed: Any = json.loads(body)
    except ValueError:
        return _error(
            None,
            RpcError(
                PARSE_ERROR,
                "The request is not valid JSON, so it could not be read. Send one JSON-RPC "
                "2.0 request, or an array of them.",
            ),
        )
    if isinstance(parsed, list):
        if not 1 <= len(parsed) <= BATCH_MAX:
            return _error(
                None,
                RpcError(
                    INVALID_REQUEST,
                    f"A batch holds 1 to {BATCH_MAX} requests; this one held {len(parsed)}.",
                ),
            )
        out = [a for r in parsed if (a := await _one(domain, r, me, tasks)) is not None]
        return out or None
    return await _one(domain, parsed, me, tasks)


def _route(domain: str) -> None:
    async def rpc(request: Request) -> Response:
        # Signed in on a session of its own, closed at once: a call such as
        # deep_research takes minutes and holds no transaction meanwhile.
        async with sessionmaker()() as s, s.begin():
            me = await current_user(request, s)
        tasks = BackgroundTasks()
        out = await handle(domain, await request.body(), me, tasks)
        if out is None:
            return Response(status_code=204, headers=DEPRECATED, background=tasks)
        return JSONResponse(out, headers=DEPRECATED, background=tasks)

    async def doc() -> JSONResponse:
        return JSONResponse(openrpc(domain), headers=DEPRECATED)

    router.add_api_route(f"/{domain}/rpc", rpc, methods=["POST"], name=f"rpc_{domain}")
    router.add_api_route(f"/{domain}/openrpc.json", doc, methods=["GET"], name=f"openrpc_{domain}")


for _domain in METHODS:
    _route(_domain)


@router.get("/domains.json")
async def domains() -> JSONResponse:
    """The old domains and their methods, as the Rust server listed them."""
    return JSONResponse(
        [
            {
                "name": d,
                "service": openrpc(d)["info"]["title"],
                "rpc": f"/api/{d}/rpc",
                "openrpc": f"/api/{d}/openrpc.json",
                "methods": [m["name"] for m in openrpc(d)["methods"]],
            }
            for d in sorted(METHODS)
        ],
        headers=DEPRECATED,
    )
