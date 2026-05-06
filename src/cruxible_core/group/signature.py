"""Deterministic group signature hashing."""

from __future__ import annotations

import hashlib
from typing import Any

from cruxible_core.primitives import canonical_json


def compute_group_signature(
    relationship_type: str,
    thesis_facts: dict[str, Any],
) -> str:
    """Versioned SHA-256 of relationship_type + canonical JSON of thesis_facts.

    Only thesis_facts is hashed, not analysis_state. This ensures signature
    stability — LLM rationales and varying centroids don't break auto-resolve.
    """
    payload = canonical_json(
        {"relationship_type": relationship_type, "thesis_facts": thesis_facts}
    )
    return f"sigv1:{hashlib.sha256(payload.encode()).hexdigest()}"
