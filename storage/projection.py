"""One-way human-readable projections from the formal SQLite store."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence


def _temporary_path(destination: Path) -> tuple[int, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    return handle, Path(raw_path)


def write_json_projection(path: str | Path, payload: Mapping) -> None:
    destination = Path(path)
    handle, temporary = _temporary_path(destination)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, ensure_ascii=False, indent=2)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_csv_projection(
    path: str | Path,
    rows: Iterable[Mapping],
    columns: Sequence[str],
) -> None:
    destination = Path(path)
    handle, temporary = _temporary_path(destination)
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(
                {column: row.get(column, "") for column in columns}
                for row in rows
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_jsonl_projection(path: str | Path, events: Iterable[Mapping]) -> None:
    destination = Path(path)
    handle, temporary = _temporary_path(destination)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(dict(event), ensure_ascii=False) + "\n")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
