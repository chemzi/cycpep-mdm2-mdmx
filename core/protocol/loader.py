"""Shared versioned-protocol loader (Engineering Standard section 8 / Roadmap PR7).

Both Design and Prediction read their scientific parameters from versioned
JSON files under ``protocols/``.  This module is the single loader and
validator for that pattern.

The protocol identity SHA-256 is computed from the CANONICALIZED
``parameters`` object only.  Metadata (description / author / comment) is part
of the file but not of the identity, so editing an explanation never
invalidates recorded evidence; only a scientific-parameter change forces a
protocol version bump and a re-run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import ProtocolError
from .schema import validate_envelope


def canonical_parameters_sha256(parameters: dict) -> str:
    """Return the identity digest of a protocol's scientific parameters.

    Canonical form is compact JSON with sorted keys, so equivalent parameter
    objects (different key order / whitespace) share one digest.
    """
    canonical = json.dumps(
        parameters,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_protocol(
    path: str | Path,
    *,
    required_sections: dict[str, type] | None = None,
) -> tuple[dict, str]:
    """Load a versioned protocol JSON and return ``(data, sha256)``.

    Validates the common envelope (name / version format / parameters /
    metadata / unknown-key rejection) and optionally pins the ``parameters``
    sections a consumer actually reads (name -> expected type).
    ``sha256`` is the identity digest of ``parameters`` only, so metadata
    edits never change the protocol identity.
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
    parameters = validate_envelope(data, protocol_path)
    for section, expected in (required_sections or {}).items():
        value = parameters.get(section)
        if not isinstance(value, expected):
            raise ProtocolError(
                f"versioned protocol parameter section {section!r} must be "
                f"{expected.__name__}, got {type(value).__name__}: {protocol_path}"
            )
    return data, canonical_parameters_sha256(parameters)
