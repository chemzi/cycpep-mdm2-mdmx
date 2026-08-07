"""Shared versioned-protocol loader (Engineering Standard section 8 / Roadmap PR7).

Both Design and Prediction read their scientific parameters from versioned
JSON files under ``protocols/``.  This module is the single loader and
validator for that pattern.

Two digests are separated:

- ``canonical_parameters_sha256`` -- the CANONICALIZED ``parameters`` object
  only.  Metadata (description / author / comment) is part of the file but
  not of this digest, so editing an explanation never invalidates recorded
  evidence; only a scientific-parameter change moves it.

- ``protocol_identity_sha256`` -- the CANONICALIZED ``{name, version,
  parameters}`` object.  This is the digest recorded in bundle bindings and
  manifests: two protocols that differ in name/version OR parameters are
  different protocols even if their parameters coincide, so a copied file
  with a bumped version but unchanged parameters still gets a new identity.

``load_protocol`` returns the identity digest.  A protocol file may declare
its required parameter sections itself via ``metadata.required_sections``
(list of section names, all required to be objects); the optional
``required_sections`` argument adds type constraints from code and both are
merged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import ProtocolError
from .schema import validate_envelope


def canonical_parameters_sha256(parameters: dict) -> str:
    """Return the digest of a protocol's scientific parameters only.

    Canonical form is compact JSON with sorted keys, so equivalent parameter
    objects (different key order / whitespace) share one digest.
    """
    return _canonical_sha256(parameters)


def protocol_identity_sha256(name: str, version: str, parameters: dict) -> str:
    """Return the digest of the full protocol identity (name+version+parameters).

    Bundles and manifests bind THIS digest: it distinguishes protocols that
    share scientific parameters but differ in name/version, so copying a
    protocol file, bumping its version and forgetting to change the
    parameters still yields a new identity.
    """
    return _canonical_sha256({
        "name": name,
        "version": version,
        "parameters": parameters,
    })


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _file_declared_sections(data: dict, protocol_path: Path) -> list[str]:
    """Return ``metadata.required_sections`` from the protocol file itself."""
    metadata = data.get("metadata")
    declared = metadata.get("required_sections") if isinstance(metadata, dict) else None
    if declared is None:
        return []
    if not isinstance(declared, list) or not all(
        isinstance(item, str) and item for item in declared
    ):
        raise ProtocolError(
            f"versioned protocol metadata.required_sections must be a list of "
            f"non-empty strings: {protocol_path}"
        )
    return declared


def load_protocol(
    path: str | Path,
    *,
    required_sections: dict[str, type] | None = None,
) -> tuple[dict, str]:
    """Load a versioned protocol JSON and return ``(data, identity_sha256)``.

    Validates the common envelope (name / version format / parameters /
    metadata / unknown-key rejection).  Parameter sections are checked two
    ways: sections the file itself declares in
    ``metadata.required_sections`` must be present and be objects, and the
    optional ``required_sections`` argument pins additional name -> type
    constraints from the consuming code.  ``identity_sha256`` binds
    name + version + parameters, so metadata edits never change it.
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
    for section in _file_declared_sections(data, protocol_path):
        value = parameters.get(section)
        if not isinstance(value, dict):
            raise ProtocolError(
                f"versioned protocol parameter section {section!r} "
                f"(declared in metadata.required_sections) must be an object, "
                f"got {type(value).__name__}: {protocol_path}"
            )
    for section, expected in (required_sections or {}).items():
        value = parameters.get(section)
        if not isinstance(value, expected):
            raise ProtocolError(
                f"versioned protocol parameter section {section!r} must be "
                f"{expected.__name__}, got {type(value).__name__}: {protocol_path}"
            )
    return data, protocol_identity_sha256(
        str(data["name"]), str(data["version"]), parameters
    )
