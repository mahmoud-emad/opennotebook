"""`opennotebook`: run the api or the worker, or write the OpenAPI document."""

import argparse
import json
import sys


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

        uvicorn.run("opennotebook.main:app", host=args.host, port=args.port, reload=args.reload)
    elif args.cmd == "worker":
        import asyncio
        import logging

        from opennotebook.jobs import worker as job_worker

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
        asyncio.run(job_worker.run(args.concurrency))
    elif args.cmd == "openapi":
        from opennotebook.main import app

        json.dump(app.openapi(), sys.stdout, indent=2)
        sys.stdout.write("\n")
