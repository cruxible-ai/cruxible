"""Client-side convenience lowering for the built-in knowledge.brief ClaimType."""

from __future__ import annotations

import base64
import json
import unicodedata
from typing import Any, Literal, Mapping, Sequence


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def prepare_playbill_brief(
    *,
    subject: Mapping[str, Any],
    purpose: str,
    kind: Literal["brief", "guidance", "faq"] = "brief",
    claim_refs: Sequence[Mapping[str, Any]] = (),
    query_refs: Sequence[Mapping[str, Any]] = (),
    prose: str = "",
    audience: Literal["agent", "human", "both"] | None = None,
    rationale: str,
    claim_ref: str | None = None,
    existing_claim_dispositions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Lower a Brief to the ordinary Claim authoring payload; no third payload kind exists."""

    if unicodedata.normalize("NFC", purpose) != purpose or not 1 <= len(purpose) <= 500:
        raise ValueError("Brief purpose must be NFC and contain 1..500 Unicode scalars")
    if unicodedata.normalize("NFC", prose) != prose or len(prose.encode("utf-8")) > 8192:
        raise ValueError("Brief prose must be NFC and fit its 8192-byte budget")
    value: dict[str, Any] = {
        "tag": "playbill-knowledge-brief-value-v1",
        "purpose": purpose,
        "kind": kind,
        "claim_refs": sorted(
            (dict(item) for item in claim_refs),
            key=_canonical_json,
        ),
        "query_refs": sorted(
            (dict(item) for item in query_refs),
            key=_canonical_json,
        ),
        "prose": prose,
    }
    if audience is not None:
        value["audience"] = audience
    if not value["claim_refs"] and not value["query_refs"] and not prose:
        raise ValueError("Brief requires at least one reference or nonempty prose")
    retained_body = prose.encode("utf-8") if prose else _canonical_json(value)
    return {
        "tag": "playbill-claim-authoring-payload-v1",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": dict(subject),
            "predicate": "knowledge.brief",
            "qualifier": None,
            "object": {"kind": "literal", "value": value},
            "role": "normative",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": rationale,
        "source": {
            "tag": "playbill-self-source-body-v1",
            "content_base64": base64.b64encode(retained_body).decode("ascii"),
        },
        "citation_role": None,
        "claim_ref": claim_ref,
        "existing_claim_dispositions": [dict(item) for item in existing_claim_dispositions],
        "insertion_target": None,
    }


__all__ = ["prepare_playbill_brief"]
