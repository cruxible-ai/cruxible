"""Load, canonicalize, and digest blueprint documents.

Canonicalization is the whole point of the digest: a blueprint is a portable
artifact that has to hash the same after a round trip through YAML, JSON, a
catalog database, and an editor that reordered the keys. The canonical form is
the validated model re-emitted in the wire shape with keys sorted, so two
documents that mean the same thing digest the same, and any semantic change
moves the digest.

Deployment facts are excluded *by construction*: the document carries slot
billing-mode constraints, never a payer, an account, a quota, or a bound
provider. There is nothing to strip.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_core.blueprint.errors import (
    BlueprintDigestError,
    BlueprintIssue,
    BlueprintValidationError,
)
from cruxible_core.blueprint.schema import (
    BLUEPRINT_FORMAT_VERSION,
    Blueprint,
    cross_reference_issues,
)
from cruxible_core.primitives import canonical_json

_DIGEST_PREFIX = b"cruxible-blueprint"

# Pydantic reports union membership by appending the matched member's tag to the
# error location. Those tags are implementation detail, not document structure.
_UNION_TAG_SEGMENTS = frozenset(
    {
        "WorkflowStepSchema",
        "ProcedureRepeatStepSchema",
        "ContractSchema",
        "NamedQuerySchema",
        "str",
        "int",
        "float",
        "bool",
        "dict",
        "list",
        "literal",
        "definition",
    }
)


class BlueprintAttachment(BaseModel):
    """One digest-pinned side file (docs, diagrams) shipped with a blueprint."""

    path: str
    digest: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class LoadedBlueprint(BaseModel):
    """A validated blueprint plus its canonical form, digest, and attachments."""

    blueprint: Blueprint
    digest: str
    attachments: list[BlueprintAttachment]

    model_config = ConfigDict(extra="forbid")

    @property
    def canonical_document(self) -> dict[str, Any]:
        return canonical_document(self.blueprint)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.blueprint)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_blueprint(data: Mapping[str, Any]) -> Blueprint:
    """Validate an already-decoded document into a :class:`Blueprint`.

    Raises :class:`BlueprintValidationError` carrying every field-pathed issue
    found -- shape problems first, then cross-reference problems.
    """
    if not isinstance(data, Mapping):
        raise BlueprintValidationError(
            "Blueprint document must be a mapping",
            [
                BlueprintIssue(
                    path="",
                    message=f"found {type(data).__name__}",
                    expected="a YAML/JSON mapping with a top-level 'blueprint:' block",
                )
            ],
        )
    try:
        blueprint = Blueprint.model_validate(dict(data))
    except ValidationError as exc:
        raise BlueprintValidationError(
            "Blueprint document failed schema validation",
            _issues_from_validation_error(exc),
        ) from None

    issues = cross_reference_issues(blueprint)
    if issues:
        raise BlueprintValidationError(
            f"Blueprint '{blueprint.coordinate}' failed cross-reference validation", issues
        )
    return blueprint


def load_blueprint_text(text: str) -> Blueprint:
    """Parse a YAML or JSON blueprint document from text."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BlueprintValidationError(
            "Blueprint document is not parseable YAML/JSON",
            [BlueprintIssue(path="", message=str(exc).replace("\n", " "))],
        ) from None
    if data is None:
        raise BlueprintValidationError(
            "Blueprint document is empty",
            [
                BlueprintIssue(
                    path="",
                    message="no document content",
                    expected="a mapping with a top-level 'blueprint:' block",
                )
            ],
        )
    return parse_blueprint(data)


def load_blueprint(
    path: str | Path,
    *,
    attachments: Iterable[str | Path] = (),
    attachment_root: str | Path | None = None,
) -> LoadedBlueprint:
    """Load a blueprint file, digest it, and return it with its attachments.

    ``attachments`` are ordered by their manifest path, not by argument order,
    so the digest cannot depend on how the caller happened to enumerate them.
    """
    document_path = Path(path)
    try:
        text = document_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BlueprintDigestError(
            f"Cannot read blueprint document '{document_path}': {exc}"
        ) from None
    blueprint = load_blueprint_text(text)
    root = Path(attachment_root) if attachment_root is not None else document_path.parent
    manifest = build_attachment_manifest(attachments, root=root)
    return LoadedBlueprint(
        blueprint=blueprint,
        digest=compute_blueprint_digest(blueprint, attachments=manifest),
        attachments=manifest,
    )


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def canonical_document(blueprint: Blueprint) -> dict[str, Any]:
    """Return the canonical wire-shaped document for a validated blueprint.

    Normalization is deliberate: omitted optional keys drop out, and every
    schema default is materialized. Two documents that differ only by writing a
    default explicitly are the same blueprint and get the same digest.

    Empty containers are pruned only where the schema supplies the same empty
    default -- the top-level blocks and the dependency lists. Nothing deeper is
    pruned: ``precondition: {}`` is a *required* procedure field, and dropping
    it would produce a document that no longer validates.
    """
    data = blueprint.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["procedures"] = [_flatten_body(body) for body in data.get("procedures", [])]
    data["pipelines"] = [_flatten_body(body) for body in data.get("pipelines", [])]
    data["dependencies"] = _prune_empty(data.get("dependencies", {}))
    return _prune_empty(data)


def _prune_empty(block: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in block.items() if value not in ({}, [])}


def canonical_bytes(blueprint: Blueprint) -> bytes:
    """Return the deterministic UTF-8 bytes the digest is computed over."""
    return canonical_json(canonical_document(blueprint)).encode("utf-8")


def canonical_yaml(blueprint: Blueprint) -> str:
    """Return the canonical document as key-sorted YAML, for review and diffing."""
    return yaml.safe_dump(
        json.loads(canonical_json(canonical_document(blueprint))),
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )


def _flatten_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Re-flatten a wrapped procedure body back to its authored wire shape."""
    definition = dict(body.get("definition", {}))
    invocation = body.get("invocation")
    if invocation is not None:
        definition["invocation"] = invocation
    return definition


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def build_attachment_manifest(
    attachments: Iterable[str | Path], *, root: str | Path
) -> list[BlueprintAttachment]:
    """Digest each attachment and return the manifest ordered by manifest path."""
    root_path = Path(root)
    entries: dict[str, BlueprintAttachment] = {}
    for attachment in attachments:
        candidate = Path(attachment)
        absolute = candidate if candidate.is_absolute() else root_path / candidate
        try:
            relative = absolute.resolve().relative_to(root_path.resolve())
        except ValueError:
            raise BlueprintDigestError(
                f"Attachment '{attachment}' resolves outside the blueprint root "
                f"'{root_path}'; attachments must be shipped alongside the document"
            ) from None
        try:
            payload = absolute.read_bytes()
        except OSError as exc:
            raise BlueprintDigestError(
                f"Cannot read blueprint attachment '{absolute}': {exc}"
            ) from None
        manifest_path = relative.as_posix()
        if manifest_path in entries:
            raise BlueprintDigestError(
                f"Attachment '{manifest_path}' was supplied more than once; "
                "the manifest must name each path exactly once"
            )
        entries[manifest_path] = BlueprintAttachment(
            path=manifest_path, digest=f"sha256:{hashlib.sha256(payload).hexdigest()}"
        )
    return [entries[key] for key in sorted(entries)]


def compute_blueprint_digest(
    blueprint: Blueprint, *, attachments: Sequence[BlueprintAttachment] = ()
) -> str:
    """Return the content digest over the canonical document and its attachments.

    Follows the kit-bundle digest pattern: NUL-delimited, length-unambiguous,
    and ordered, so no two distinct manifests can produce the same preimage.
    """
    digest = hashlib.sha256()
    digest.update(_DIGEST_PREFIX)
    digest.update(b"/")
    digest.update(BLUEPRINT_FORMAT_VERSION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(canonical_bytes(blueprint))
    digest.update(b"\0")
    for attachment in sorted(attachments, key=lambda item: item.path):
        digest.update(attachment.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(attachment.digest.encode("utf-8"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _issues_from_validation_error(exc: ValidationError) -> list[BlueprintIssue]:
    issues: list[BlueprintIssue] = []
    for error in exc.errors():
        path = _format_location(error.get("loc", ()))
        message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        issues.append(BlueprintIssue(path=path, message=message, expected=_expected(error)))
    return issues


def _format_location(loc: Sequence[Any]) -> str:
    parts: list[str] = []
    for segment in loc:
        if isinstance(segment, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{segment}]"
            else:
                parts.append(f"[{segment}]")
            continue
        text = str(segment)
        if text in _UNION_TAG_SEGMENTS or "[" in text:
            continue
        parts.append(text)
    return ".".join(parts)


def _expected(error: Mapping[str, Any]) -> str | None:
    ctx = error.get("ctx")
    if isinstance(ctx, Mapping):
        expected = ctx.get("expected")
        if expected:
            return str(expected)
    error_type = str(error.get("type", ""))
    if error_type == "extra_forbidden":
        return "remove the key; blueprint objects forbid unknown fields"
    if error_type == "missing":
        return "the field is required"
    return None
