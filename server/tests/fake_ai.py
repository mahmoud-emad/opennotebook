"""A model endpoint for tests: answers each chat call with the next reply
given, and keeps what it was asked. Nothing leaves the process."""

import json
from typing import Any

import httpx2

from opennotebook.ai.client import Ai


class FakeModel:
    def __init__(self, *replies: str | int) -> None:
        # A string is the answer's text; a number is an HTTP error status.
        self.replies = list(replies)
        self.asked: list[dict[str, Any]] = []
        self.http = httpx2.AsyncClient(transport=httpx2.MockTransport(self._answer))
        self.client = Ai("http://ai.test/v1", "k", http=self.http, retries=0)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": []})
        self.asked.append(json.loads(request.content))
        reply = self.replies.pop(0) if self.replies else 503
        if isinstance(reply, int):
            return httpx2.Response(reply, json={"error": {"message": "down"}})
        return httpx2.Response(
            200,
            json={
                "model": "m/test",
                "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
            },
        )

    def __call__(self) -> Ai:
        return self.client
