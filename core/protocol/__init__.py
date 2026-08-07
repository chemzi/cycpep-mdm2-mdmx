"""Shared versioned-protocol layer: loader, schema, errors.

Every domain protocol module (``prediction_pipeline.protocol``,
``agents.design.config``) imports from this package, so there is exactly one
``ProtocolError`` and one loader contract in the repository.
"""

from .errors import ProtocolError
from .loader import (
    canonical_parameters_sha256,
    load_protocol,
    protocol_identity_sha256,
)
from .schema import ENVELOPE_KEYS, PROTOCOL_VERSION_RE

__all__ = [
    "ENVELOPE_KEYS",
    "PROTOCOL_VERSION_RE",
    "ProtocolError",
    "canonical_parameters_sha256",
    "load_protocol",
    "protocol_identity_sha256",
]
