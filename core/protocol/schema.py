"""Envelope schema shared by every versioned protocol file.

A protocol file is a JSON object with exactly four top-level keys:

- ``name``       -- domain identifier, e.g. ``"prediction"`` or ``"design"``
- ``version``    -- ``MAJOR.MINOR`` string, e.g. ``"1.0"``
- ``parameters`` -- the scientific semantics; the ONLY input to the protocol
                    identity SHA-256, so metadata edits never invalidate
                    recorded evidence
- ``metadata``   -- free-form bookkeeping (description / author / comment);
                    changes must NOT invalidate evidence
"""

from __future__ import annotations

import re

from .errors import ProtocolError

# Major.Minor, e.g. "1.0".  A semver-style looser format is deliberately not
# accepted so a protocol cannot silently smuggle a version typo past import.
PROTOCOL_VERSION_RE = re.compile(r"^\d+\.\d+$")

ENVELOPE_KEYS = frozenset({"name", "version", "parameters", "metadata"})


def validate_envelope(data: object, path: object) -> dict:
    """Validate the shared protocol envelope and return ``parameters``."""
    if not isinstance(data, dict):
        raise ProtocolError(f"versioned protocol must be a JSON object: {path}")
    unknown = sorted(set(data) - ENVELOPE_KEYS)
    if unknown:
        raise ProtocolError(
            f"versioned protocol has unsupported fields {unknown}; allowed "
            f"envelope keys are {sorted(ENVELOPE_KEYS)}: {path}"
        )
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError(
            f"versioned protocol must declare a non-empty 'name': {path}"
        )
    version = data.get("version")
    if not isinstance(version, str) or not PROTOCOL_VERSION_RE.fullmatch(version):
        raise ProtocolError(
            f"versioned protocol 'version' must match "
            f"{PROTOCOL_VERSION_RE.pattern!r}, got {version!r}: {path}"
        )
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        raise ProtocolError(
            f"versioned protocol must declare a 'parameters' object: {path}"
        )
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ProtocolError(
            f"versioned protocol 'metadata' must be an object: {path}"
        )
    return parameters
