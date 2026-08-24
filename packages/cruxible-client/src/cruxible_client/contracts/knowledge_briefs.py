"""Built-in knowledge.brief contract, canonical value, and slot identity."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.claim_types import (
    ClaimSlotPolicyV1,
    ClaimSubjectScopeV1,
    ClaimType,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.semantic import SemanticAddress

KNOWLEDGE_BRIEF_PREDICATE = "knowledge.brief"
KNOWLEDGE_BRIEF_PROFILE_ID = "knowledge-brief-v1"
KNOWLEDGE_BRIEF_PROFILE_DOMAIN = "playbill-knowledge-brief-profile-v1"
KNOWLEDGE_BRIEF_PURPOSE_DOMAIN = "playbill-knowledge-brief-purpose-v1"

_CLAIM_ID_RE = re.compile(r"^CLM-[0-9a-f]{32}$")
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})+$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


class KnowledgeBriefFormatError(ValueError):
    """A knowledge.brief literal does not satisfy its frozen profile."""


class _StrictBriefModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _nfc(value: str, *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be NFC-normalized")
    return value


class KnowledgeBriefClaimExpectationV1(_StrictBriefModel):
    tag: Literal["playbill-knowledge-brief-claim-expectation-v1"] = (
        "playbill-knowledge-brief-claim-expectation-v1"
    )
    resolution: Literal["accepted"] = "accepted"
    subject: SemanticAddress | None = None
    claim_type: str | None = None

    @model_serializer(mode="wrap")
    def _optional_constraints(self, handler: Any) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        if self.subject is None:
            payload.pop("subject", None)
        if self.claim_type is None:
            payload.pop("claim_type", None)
        return payload

    @field_validator("claim_type")
    @classmethod
    def _claim_type(cls, value: str | None) -> str | None:
        if value is not None and not _PREDICATE_RE.fullmatch(value):
            raise ValueError("Brief claim expectation claim_type is not canonical")
        return value


class KnowledgeBriefClaimRefV1(_StrictBriefModel):
    tag: Literal["playbill-knowledge-brief-claim-ref-v1"] = "playbill-knowledge-brief-claim-ref-v1"
    claim_id: str
    statement_digest: str
    expect: KnowledgeBriefClaimExpectationV1

    @field_validator("claim_id")
    @classmethod
    def _claim_id(cls, value: str) -> str:
        if not _CLAIM_ID_RE.fullmatch(value):
            raise ValueError("Brief claim_ref ID is not canonical")
        return value

    @field_validator("statement_digest")
    @classmethod
    def _statement_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class KnowledgeBriefQueryRefV1(_StrictBriefModel):
    tag: Literal["playbill-knowledge-brief-query-ref-v1"] = "playbill-knowledge-brief-query-ref-v1"
    query_id: str
    definition_digest: str
    parameters: dict[str, object]
    render_field: str

    @field_validator("query_id", "render_field")
    @classmethod
    def _identifier(cls, value: str) -> str:
        _nfc(value, label="Brief query reference identifier")
        if not value or value.strip() != value or any(char in value for char in "{}"):
            raise ValueError("Brief query reference identifier is not canonical")
        return value

    @field_validator("definition_digest")
    @classmethod
    def _definition_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("parameters")
    @classmethod
    def _parameters(cls, value: dict[str, object]) -> dict[str, object]:
        normalized = normalize_canonical(value)
        if not isinstance(normalized, dict):  # pragma: no cover - field type proves this
            raise ValueError("Brief query parameters must be a canonical object")
        return cast(dict[str, object], normalized)


class KnowledgeBriefValueV1(_StrictBriefModel):
    tag: Literal["playbill-knowledge-brief-value-v1"] = "playbill-knowledge-brief-value-v1"
    purpose: str
    kind: Literal["brief", "guidance", "faq"]
    claim_refs: tuple[KnowledgeBriefClaimRefV1, ...] = ()
    query_refs: tuple[KnowledgeBriefQueryRefV1, ...] = ()
    prose: str = ""
    audience: Literal["agent", "human", "both"] | None = None

    @model_serializer(mode="wrap")
    def _optional_audience(self, handler: Any) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        if self.audience is None:
            payload.pop("audience", None)
        return payload

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        _nfc(value, label="Brief purpose")
        if not 1 <= len(value) <= 500:
            raise ValueError("Brief purpose must contain 1..500 Unicode scalars")
        return value

    @field_validator("prose")
    @classmethod
    def _prose(cls, value: str) -> str:
        _nfc(value, label="Brief prose")
        if len(value.encode("utf-8")) > 8192:
            raise ValueError("Brief prose exceeds 8192 UTF-8 bytes")
        return value

    @field_validator("claim_refs")
    @classmethod
    def _claim_refs(
        cls,
        value: tuple[KnowledgeBriefClaimRefV1, ...],
    ) -> tuple[KnowledgeBriefClaimRefV1, ...]:
        if len(value) > 64:
            raise ValueError("Brief exceeds 64 direct Claim references")
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("Brief Claim references must be byte-sorted and unique")
        return value

    @field_validator("query_refs")
    @classmethod
    def _query_refs(
        cls,
        value: tuple[KnowledgeBriefQueryRefV1, ...],
    ) -> tuple[KnowledgeBriefQueryRefV1, ...]:
        if len(value) > 16:
            raise ValueError("Brief exceeds 16 direct Query references")
        encoded = tuple(canonical_bytes(item.model_dump(mode="json")) for item in value)
        if encoded != tuple(sorted(set(encoded))):
            raise ValueError("Brief Query references must be byte-sorted and unique")
        return value

    @model_validator(mode="after")
    def _complete(self) -> "KnowledgeBriefValueV1":
        if "audience" in self.model_fields_set and self.audience is None:
            raise ValueError("Brief audience is omitted or one of its closed values")
        if not self.claim_refs and not self.query_refs and not self.prose:
            raise ValueError("Brief requires at least one reference or nonempty prose")
        declared = {f"{item.query_id}.{item.render_field}" for item in self.query_refs}
        for placeholder in _PLACEHOLDER_RE.findall(self.prose):
            if placeholder not in declared:
                raise ValueError("Brief prose contains an undeclared query placeholder")
        if len(declared) != len(self.query_refs):
            raise ValueError("Brief query placeholders are ambiguous")
        return self


def knowledge_brief_purpose_digest(purpose: str) -> str:
    checked = KnowledgeBriefValueV1(
        purpose=purpose,
        kind="brief",
        prose="purpose-validation",
    ).purpose
    return typed_digest(
        Sha256Value,
        KNOWLEDGE_BRIEF_PURPOSE_DOMAIN,
        {"purpose": checked},
    ).tagged


def parse_knowledge_brief_value(value: object) -> KnowledgeBriefValueV1:
    try:
        parsed = KnowledgeBriefValueV1.model_validate(value)
    except ValueError as exc:
        raise KnowledgeBriefFormatError("knowledge.brief value failed its frozen profile") from exc
    if normalize_canonical(value) != parsed.model_dump(mode="json"):
        raise KnowledgeBriefFormatError("knowledge.brief value is not in canonical profile form")
    return parsed


KNOWLEDGE_BRIEF_LITERAL_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["tag", "purpose", "kind", "claim_refs", "query_refs", "prose"],
    "properties": {
        "tag": {"const": "playbill-knowledge-brief-value-v1"},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
        "kind": {"enum": ["brief", "faq", "guidance"], "type": "string"},
        "claim_refs": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": True,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tag", "claim_id", "statement_digest", "expect"],
                "properties": {
                    "tag": {"const": "playbill-knowledge-brief-claim-ref-v1"},
                    "claim_id": {"type": "string", "pattern": "^CLM-[0-9a-f]{32}$"},
                    "statement_digest": {
                        "type": "string",
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                    "expect": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["tag", "resolution"],
                        "properties": {
                            "tag": {"const": "playbill-knowledge-brief-claim-expectation-v1"},
                            "resolution": {"const": "accepted"},
                            "subject": {"$ref": "#/$defs/semantic_address"},
                            "claim_type": {
                                "type": "string",
                                "pattern": ("^[a-z][a-z0-9_]{0,63}(?:\\.[a-z][a-z0-9_]{0,63})+$"),
                            },
                        },
                    },
                },
            },
        },
        "query_refs": {
            "type": "array",
            "maxItems": 16,
            "uniqueItems": True,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "tag",
                    "query_id",
                    "definition_digest",
                    "parameters",
                    "render_field",
                ],
                "properties": {
                    "tag": {"const": "playbill-knowledge-brief-query-ref-v1"},
                    "query_id": {"type": "string", "minLength": 1},
                    "definition_digest": {
                        "type": "string",
                        "pattern": "^sha256:[0-9a-f]{64}$",
                    },
                    "parameters": {"type": "object"},
                    "render_field": {"type": "string", "minLength": 1},
                },
            },
        },
        "prose": {"type": "string"},
        "audience": {"enum": ["agent", "both", "human"], "type": "string"},
    },
    "$defs": {
        "semantic_selector": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scheme", "value"],
            "properties": {
                "scheme": {"type": "string"},
                "value": {"type": "string"},
            },
        },
        "semantic_address": {
            "type": "object",
            "additionalProperties": False,
            "required": ["tag", "artifact_path", "selector"],
            "properties": {
                "tag": {"const": "playbill-semantic-address-v1"},
                "artifact_path": {"type": "string"},
                "selector": {"$ref": "#/$defs/semantic_selector"},
            },
        },
    },
}


KNOWLEDGE_BRIEF_CLAIM_TYPE = ClaimType(
    artifact_format="playbill-claim-type-v2",
    identity=ArtifactIdentity(kind="ClaimType", name=KNOWLEDGE_BRIEF_PREDICATE),
    predicate=KNOWLEDGE_BRIEF_PREDICATE,
    allowed_subject_kinds=(),
    subject_scope=ClaimSubjectScopeV1(),
    slot_policy=ClaimSlotPolicyV1(),
    object_kind="literal",
    literal_schema=KNOWLEDGE_BRIEF_LITERAL_SCHEMA,
    cardinality="many",
    permitted_roles=("normative",),
    referent_sensitivity="identity",
    evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
    admission_policy=ClaimAdmissionPolicyV1(),
    resolution_policy=ClaimResolutionPolicyV1(
        cardinality="many",
        eligible_verdicts=("supported", "uncovered"),
        require_current=True,
        selector="all",
        conflict_result="unresolved",
    ),
    authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
)

KNOWLEDGE_BRIEF_PROFILE_DIGEST = typed_digest(
    Sha256Value,
    KNOWLEDGE_BRIEF_PROFILE_DOMAIN,
    {
        "profile_id": KNOWLEDGE_BRIEF_PROFILE_ID,
        "claim_type": KNOWLEDGE_BRIEF_CLAIM_TYPE.model_dump(mode="json"),
    },
).tagged


__all__ = [
    "KNOWLEDGE_BRIEF_CLAIM_TYPE",
    "KNOWLEDGE_BRIEF_LITERAL_SCHEMA",
    "KNOWLEDGE_BRIEF_PREDICATE",
    "KNOWLEDGE_BRIEF_PROFILE_DIGEST",
    "KNOWLEDGE_BRIEF_PROFILE_DOMAIN",
    "KNOWLEDGE_BRIEF_PROFILE_ID",
    "KNOWLEDGE_BRIEF_PURPOSE_DOMAIN",
    "KnowledgeBriefClaimExpectationV1",
    "KnowledgeBriefClaimRefV1",
    "KnowledgeBriefFormatError",
    "KnowledgeBriefQueryRefV1",
    "KnowledgeBriefValueV1",
    "knowledge_brief_purpose_digest",
    "parse_knowledge_brief_value",
]
