"""A model's reply read as JSON, as the motion page's prompts ask for it —
the whiteboard's reader, kept beside the motion modules so none of them
reaches across styles for one function (`whiteboard/reply.py`)."""

from opennotebook.build.whiteboard.reply import json_of

__all__ = ["json_of"]
