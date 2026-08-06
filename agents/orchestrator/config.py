"""Orchestrator constants and version markers (PR6)."""

from __future__ import annotations

import re

ORCHESTRATOR_VERSION = "1.1.1"
RUN_SCHEMA_VERSION = 2
LEGACY_RUN_SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^orchestrator_[0-9a-f]{12}$")
