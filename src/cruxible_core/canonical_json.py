"""Shared canonical JSON serialization helpers."""

from __future__ import annotations

import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value with deterministic key and separator rules."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
