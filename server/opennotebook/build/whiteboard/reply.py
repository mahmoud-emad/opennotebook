"""A model's answer read as JSON, as the whiteboard's prompts ask for it."""

import json
from typing import Any


def json_of(text: str) -> Any:
    """The first whole JSON object in a reply, fenced or not, whatever
    follows it: a model sometimes adds a note, or a second object.
    `ValueError` when there is none."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value
