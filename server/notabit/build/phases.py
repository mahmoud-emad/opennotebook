"""The phases a deck or an audio overview is made in, and how a person
reads each: the outputs list and the player say them as the server words
them.

Every phase of a prep, in order, and the number a progress bar is drawn
against. They are one list because they are one job: reporting only the last
few left `steps_total` at 0 through ingest and the script, which is the job
row's way of saying "this job does not report". Research is always a phase,
run or not, so the bar has the same steps whichever way it was asked.
"""

PHASES = ("research", "ingest", "script", "deck", "validate")

LABELS = {
    "research": "Researching the web",
    "ingest": "Reading your sources",
    "script": "Writing the script",
    "deck": "Recording the voices, and drawing any slides",
    "validate": "Checking it renders",
}

# Before the first phase has started.
STARTING = "Starting"


def label(step: str) -> str:
    """A step as a person reads it: its phase's words, the step itself for
    one that is not a phase, and "Starting" before there is one."""
    return LABELS.get(step, step) or STARTING
