"""Small cross-platform per-launcher-run exclusive file lock."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


def _try_lock(stream: TextIO) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(stream: TextIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        if stream.tell() == 0:
            stream.write("0")
            stream.flush()
        deadline = time.monotonic() + timeout_seconds
        while not _try_lock(stream):
            if time.monotonic() >= deadline:
                raise TimeoutError("launcher diagnostic lock is busy")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(stream)


__all__ = ["exclusive_file_lock"]
