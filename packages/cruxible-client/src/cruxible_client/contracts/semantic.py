"""Canonical semantic subjects and exact source spans for Playbill."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, normalize_ledger_path
from cruxible_client.contracts.errors import CanonicalEncodingError


class _StrictSemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_whole_artifact(value: str) -> str:
    if value != "":
        raise ValueError("artifact-v1 selector value must be empty")
    return value


def _validate_claim_statement(value: str) -> str:
    if value != "":
        raise ValueError("claim-statement-v1 selector value must be empty")
    return value


_PROCEDURE_NODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PROCEDURE_ARM_RE = re.compile(
    r"^[a-z][a-z0-9_.-]{0,127}:(?:next|on_true|on_false):"
    r"(?:\$abort|[a-z][a-z0-9_.-]{0,127})$"
)


def _validate_empty_selector(value: str) -> str:
    if value != "":
        raise ValueError("whole semantic-unit selector value must be empty")
    return value


def _validate_procedure_node(value: str) -> str:
    if not _PROCEDURE_NODE_RE.fullmatch(value):
        raise ValueError("procedure-node-v1 selector must be a stable authored node_id")
    return value


def _validate_procedure_arm(value: str) -> str:
    if not _PROCEDURE_ARM_RE.fullmatch(value):
        raise ValueError("procedure-arm-v1 selector must be from_node_id:arm_label:target_node_id")
    return value


_SELECTOR_SCHEMES: dict[str, Callable[[str], str]] = {
    "artifact-v1": _validate_whole_artifact,
    "claim-statement-v1": _validate_claim_statement,
    "line-v1": _validate_empty_selector,
    "procedure-arm-v1": _validate_procedure_arm,
    "procedure-node-v1": _validate_procedure_node,
    "procedure-unit-v1": _validate_empty_selector,
}


def registered_selector_schemes() -> tuple[str, ...]:
    """Return the closed selector schemes understood by this compiler."""

    return tuple(sorted(_SELECTOR_SCHEMES, key=lambda value: value.encode("utf-8")))


class SemanticSelector(_StrictSemanticModel):
    scheme: str
    value: str

    @field_validator("scheme")
    @classmethod
    def _scheme(cls, value: str) -> str:
        if value not in _SELECTOR_SCHEMES:
            supported = ", ".join(registered_selector_schemes())
            raise ValueError(f"unknown semantic selector scheme {value!r}; supported: {supported}")
        return value

    @model_validator(mode="after")
    def _registered_value(self) -> "SemanticSelector":
        _SELECTOR_SCHEMES[self.scheme](self.value)
        return self


class SemanticAddress(_StrictSemanticModel):
    """Stable meaning identity, independent of source-line presentation."""

    tag: Literal["playbill-semantic-address-v1"] = "playbill-semantic-address-v1"
    artifact_path: str
    selector: SemanticSelector

    @field_validator("artifact_path")
    @classmethod
    def _artifact_path(cls, value: str) -> str:
        try:
            normalized = normalize_ledger_path(value)
        except CanonicalEncodingError as exc:
            raise ValueError("semantic artifact_path must be a canonical ledger path") from exc
        if normalized != value:
            raise ValueError("semantic artifact_path must already be NFC-normalized")
        return value

    @classmethod
    def whole_artifact(cls, artifact_path: str) -> "SemanticAddress":
        return cls(
            artifact_path=artifact_path,
            selector=SemanticSelector(scheme="artifact-v1", value=""),
        )

    @classmethod
    def claim_statement(cls, artifact_path: str) -> "SemanticAddress":
        return cls(
            artifact_path=artifact_path,
            selector=SemanticSelector(scheme="claim-statement-v1", value=""),
        )

    @classmethod
    def procedure_unit(cls, artifact_path: str) -> "SemanticAddress":
        return cls(
            artifact_path=artifact_path,
            selector=SemanticSelector(scheme="procedure-unit-v1", value=""),
        )

    @classmethod
    def procedure_node(cls, artifact_path: str, node_id: str) -> "SemanticAddress":
        return cls(
            artifact_path=artifact_path,
            selector=SemanticSelector(scheme="procedure-node-v1", value=node_id),
        )

    @classmethod
    def procedure_arm(
        cls,
        artifact_path: str,
        *,
        from_node_id: str,
        arm_label: Literal["next", "on_true", "on_false"],
        target_node_id: str,
    ) -> "SemanticAddress":
        return cls(
            artifact_path=artifact_path,
            selector=SemanticSelector(
                scheme="procedure-arm-v1",
                value=f"{from_node_id}:{arm_label}:{target_node_id}",
            ),
        )

    @classmethod
    def line(cls, artifact_path: str) -> "SemanticAddress":
        return cls(
            artifact_path=artifact_path,
            selector=SemanticSelector(scheme="line-v1", value=""),
        )


class ContentSpan(_StrictSemanticModel):
    """One exact zero-based, end-exclusive occurrence in immutable bytes."""

    tag: Literal["playbill-content-span-v1"] = "playbill-content-span-v1"
    content_digest: str
    start_byte: int
    end_byte: int

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _range(self) -> "ContentSpan":
        if self.start_byte < 0 or self.end_byte < self.start_byte:
            raise ValueError("content span must be a valid zero-based byte range")
        return self


class SourceMapping(_StrictSemanticModel):
    """Versioned relation from one semantic subject to exact byte occurrences."""

    tag: Literal["playbill-source-mapping-v1"] = "playbill-source-mapping-v1"
    subject: SemanticAddress
    spans: tuple[ContentSpan, ...]

    @field_validator("spans")
    @classmethod
    def _spans(cls, value: tuple[ContentSpan, ...]) -> tuple[ContentSpan, ...]:
        if not value:
            raise ValueError("source mapping requires at least one exact span")
        ordered = tuple(
            sorted(
                value,
                key=lambda span: (
                    span.content_digest.encode("ascii"),
                    span.start_byte,
                    span.end_byte,
                ),
            )
        )
        identities = {canonical_bytes(span.model_dump(mode="json")) for span in value}
        if value != ordered or len(identities) != len(value):
            raise ValueError("source mapping spans must be sorted and unique")
        return value


def whole_body_mapping(artifact_path: str, content_digest: str, byte_length: int) -> SourceMapping:
    """Construct the Family-1 whole-Document source mapping."""

    if byte_length < 0:
        raise ValueError("whole-body mapping byte length cannot be negative")
    return SourceMapping(
        subject=SemanticAddress.whole_artifact(artifact_path),
        spans=(
            ContentSpan(
                content_digest=content_digest,
                start_byte=0,
                end_byte=byte_length,
            ),
        ),
    )


__all__ = [
    "ContentSpan",
    "SemanticAddress",
    "SemanticSelector",
    "SourceMapping",
    "registered_selector_schemes",
    "whole_body_mapping",
]
