"""Shared versioned-protocol loader (Engineering Standard section 8 / Roadmap PR7).

Both Design and Prediction read their scientific parameters from versioned
JSON files under ``protocols/``.  This module is the single loader and
validator for that pattern: one place for the missing-file / malformed-JSON /
version contract, so the two sides cannot drift apart.

Every protocol returns ``(data, sha256)`` where ``sha256`` is the digest of
the raw file bytes; artifacts that bind the digest stay reproducible across a
parameter change (which must then bump ``version``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ProtocolError(ValueError):
    """A versioned protocol file is missing, malformed, or lacks a version."""


def load_protocol(path: str | Path) -> tuple[dict, str]:
    """Load a versioned protocol JSON and return ``(data, sha256)``.

    Validates the common contract shared by all protocol files: the file
    exists, decodes as a UTF-8 JSON object, and declares a non-empty
    ``version`` string.  Protocol-specific fields are validated by each
    consumer.
    """
    protocol_path = Path(path)
    if not protocol_path.is_file():
        raise ProtocolError(f"versioned protocol missing: {protocol_path}")
    raw = protocol_path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            f"versioned protocol is not valid UTF-8 JSON: {protocol_path}"
        ) from exc
    if not isinstance(data, dict):
        raise ProtocolError(
            f"versioned protocol must be a JSON object: {protocol_path}"
        )
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ProtocolError(
            f"versioned protocol must declare a non-empty 'version': {protocol_path}"
        )
    return data, hashlib.sha256(raw).hexdigest()
