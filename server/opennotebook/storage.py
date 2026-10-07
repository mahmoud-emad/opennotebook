"""The files volume: bytes Postgres should not hold, such as original
uploads, narration audio and slide HTML.

One small interface, so object storage can replace the directory later
without touching the callers. Paths are relative and use `/`.
"""

import shutil
from pathlib import Path

from opennotebook.config import settings


def _root() -> Path:
    return settings().files_dir.resolve()


def _at(rel: str) -> Path:
    path = (_root() / rel).resolve()
    if not path.is_relative_to(_root()):
        raise ValueError(f"{rel!r} is outside the files volume")
    return path


def put(rel: str, data: bytes) -> str:
    """Write a file whole, through a temporary name, and return its path."""
    path = _at(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return rel


def put_file(rel: str, src: Path) -> str:
    """Move a finished local file in whole, through a temporary name, and
    return its path: for files too big to hold in memory, such as a video."""
    path = _at(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    shutil.move(src, tmp)
    tmp.replace(path)
    return rel


def read(rel: str) -> bytes:
    return _at(rel).read_bytes()


def local_path(rel: str) -> Path:
    """Where a file is on this machine, for a response that streams it from
    disk or a tool that reads it by name."""
    return _at(rel)


def remove_tree(rel: str) -> None:
    """Remove a directory and everything in it, if it is there."""
    shutil.rmtree(_at(rel), ignore_errors=True)


def remove_file(rel: str) -> None:
    _at(rel).unlink(missing_ok=True)


def exists(rel: str) -> bool:
    return _at(rel).exists()


def copy_new(src: str, dst: str) -> str:
    """Copy one file, refusing to replace one already there, and return the
    new path."""
    to = _at(dst)
    to.parent.mkdir(parents=True, exist_ok=True)
    with _at(src).open("rb") as f, to.open("xb") as t:
        shutil.copyfileobj(f, t)
    return dst


def copy_tree(src: str, dst: str) -> bool:
    """Copy a directory tree whose destination must not exist yet. False when
    there was nothing at `src` to copy."""
    frm = _at(src)
    if not frm.is_dir():
        return False
    to = _at(dst)
    to.parent.mkdir(parents=True, exist_ok=True)
    # `copytree` refuses a destination that is already there, so a copy never
    # writes over something it did not make.
    shutil.copytree(frm, to, symlinks=True)
    return True
