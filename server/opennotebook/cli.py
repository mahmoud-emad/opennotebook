"""`opennotebook`: run the api or the worker, or write the OpenAPI document."""

import argparse
import copy
import ipaddress
import json
import sys
from typing import Any

# One line per record, the same in the api and the worker.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def log_config() -> dict[str, Any]:
    """Uvicorn's own logging, plus the studio's: without a handler on the
    root logger, everything the app logs (a failed turn, a stalled job) is
    dropped. Applied by uvicorn in the process that serves, so `--reload`
    keeps it too."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg["formatters"]["studio"] = {"format": LOG_FORMAT}
    cfg["handlers"]["studio"] = {
        "class": "logging.StreamHandler",
        "formatter": "studio",
        "stream": "ext://sys.stderr",
    }
    cfg["root"] = {"handlers": ["studio"], "level": "INFO"}
    return cfg


def loopback(host: str) -> bool:
    """Whether `host` is reachable only from this machine."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def refuse_open_local(host: str, auth: str, allowed: bool) -> str | None:
    """Why serving on `host` is refused, or None. In `local` mode every
    request without a key is the owner, so the api on a network address
    hands the studio to anyone who can reach it."""
    if auth != "local" or allowed or loopback(host):
        return None
    return (
        f"Not serving on {host}: with OPENNOTEBOOK_AUTH=local every request is signed in as "
        "the studio's owner, so anyone who can reach this address could use it as you. "
        "Serve on 127.0.0.1, or set OPENNOTEBOOK_AUTH=keys, or, if only people you trust can "
        "reach it (behind a proxy that signs people in, say), set "
        "OPENNOTEBOOK_ALLOW_LOCAL_AUTH_ON_NETWORK=1."
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="opennotebook")
    sub = p.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the api")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    worker = sub.add_parser("worker", help="run the background worker: builds and research")
    worker.add_argument(
        "--concurrency", type=int, default=4, help="jobs at once; builds still run one at a time"
    )
    sub.add_parser("openapi", help="print the OpenAPI document")
    args = p.parse_args()

    if args.cmd == "serve":
        import uvicorn

        from opennotebook.config import settings
        from opennotebook.shutdown import CONNECTIONS_SECONDS

        c = settings()
        if why := refuse_open_local(args.host, c.auth, c.allow_local_auth_on_network):
            sys.stderr.write(why + "\n")
            sys.exit(2)
        uvicorn.run(
            "opennotebook.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_config=log_config(),
            timeout_graceful_shutdown=CONNECTIONS_SECONDS,
        )
    elif args.cmd == "worker":
        import asyncio
        import logging

        from opennotebook.jobs import worker as job_worker

        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        asyncio.run(job_worker.run(args.concurrency))
    elif args.cmd == "openapi":
        from opennotebook.main import app

        json.dump(app.openapi(), sys.stdout, indent=2)
        sys.stdout.write("\n")
