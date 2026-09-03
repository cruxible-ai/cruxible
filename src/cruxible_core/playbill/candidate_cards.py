"""Deterministic, authority-free Markdown cards for candidate artifact changes."""

from __future__ import annotations

import json
from collections.abc import Mapping

from cruxible_client.contracts.artifacts import ArtifactKindRegistry
from cruxible_client.contracts.canonical import canonical_digest, normalize_ledger_path
from cruxible_client.contracts.errors import ProjectionFormatError, ProposalIntegrityError

CARD_NAMESPACE = "cards/"
CARD_RENDERER_IMPLEMENTATION = "python-reference-v1"
_HEADER_TEMPLATE = "# {kind}: {identity}\n\n- Artifact: `{path}`\n\n"
_BODY_TEMPLATE = "```json\n{body}\n```\n"
_REMOVAL_TEMPLATE = "removed at {coordinate}\n"
CARD_TEMPLATE_DIGESTS = tuple(
    f"sha256:{canonical_digest('playbill-card-template-v1', {'template': template})}"
    for template in (_HEADER_TEMPLATE, _BODY_TEMPLATE, _REMOVAL_TEMPLATE)
)
CARD_RENDERER_DIGEST = "sha256:" + canonical_digest(
    "playbill-card-renderer-v1",
    {
        "implementation": CARD_RENDERER_IMPLEMENTATION,
        "template_digests": list(CARD_TEMPLATE_DIGESTS),
    },
)


def is_candidate_card_path(path: str) -> bool:
    """Return whether a normalized ledger path belongs to the derivative card namespace."""

    return normalize_ledger_path(path).startswith(CARD_NAMESPACE)


def candidate_card_path(artifact_path: str) -> str:
    """Map one canonical JSON artifact path to its fixed Markdown sidecar."""

    path = normalize_ledger_path(artifact_path)
    if not path.endswith(".json") or path.startswith(CARD_NAMESPACE):
        raise ProposalIntegrityError("candidate cards require a canonical JSON artifact path")
    return f"{CARD_NAMESPACE}{path[:-5]}.md"


def _identity(payload: Mapping[str, object], *, path: str) -> str:
    raw = payload.get("identity")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        kind = raw.get("kind")
        name = raw.get("name")
        if isinstance(kind, str) and isinstance(name, str):
            return f"{kind}:{name}"
    for key in ("principal_id", "artifact_id", "name", "predicate", "tag"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return path


def render_candidate_card(
    artifact_path: str,
    content: bytes,
    *,
    artifact_kinds: ArtifactKindRegistry,
) -> bytes:
    """Render one compiler-recognized canonical JSON artifact as stable Markdown."""

    path = normalize_ledger_path(artifact_path)
    try:
        kind = artifact_kinds.resolve_path(path)
        payload = json.loads(content)
    except (ProjectionFormatError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalIntegrityError(
            f"candidate card source is not a registered artifact: {path}"
        ) from exc
    if kind in {"changeset", "presentation"} or not isinstance(payload, dict):
        raise ProposalIntegrityError(f"candidate card source has no floor card: {path}")
    body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    return (
        _HEADER_TEMPLATE.format(kind=kind, identity=_identity(payload, path=path), path=path)
        + _BODY_TEMPLATE.format(body=body)
    ).encode("utf-8")


def render_removal_card(*, coordinate: str) -> bytes:
    """Render the ruled one-line tombstone for an artifact removal."""

    return _REMOVAL_TEMPLATE.format(coordinate=coordinate).encode("utf-8")


def derive_candidate_cards(
    *,
    base_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    coordinate: str,
    artifact_kinds: ArtifactKindRegistry,
) -> dict[str, bytes]:
    """Return the candidate tree with exact derivative cards for semantic changes."""

    result = {
        path: content
        for path, content in candidate_tree.items()
        if not is_candidate_card_path(path)
    }
    result.update(
        {path: content for path, content in base_tree.items() if is_candidate_card_path(path)}
    )
    semantic_paths = sorted(
        {
            path
            for path in {*base_tree, *candidate_tree}
            if not is_candidate_card_path(path)
            and not path.startswith("changesets/")
            and (base_tree.get(path) != candidate_tree.get(path))
        },
        key=lambda item: item.encode("utf-8"),
    )
    for path in semantic_paths:
        try:
            kind = artifact_kinds.resolve_path(path)
        except ProjectionFormatError:
            continue
        if kind in {"changeset", "presentation"} or not path.endswith(".json"):
            continue
        card_path = candidate_card_path(path)
        content = candidate_tree.get(path)
        result[card_path] = (
            render_removal_card(coordinate=coordinate)
            if content is None
            else render_candidate_card(path, content, artifact_kinds=artifact_kinds)
        )
    return result


def verify_candidate_cards(
    *,
    base_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    coordinate: str,
    artifact_kinds: ArtifactKindRegistry,
) -> None:
    """Re-derive and byte-verify every card in a candidate tree."""

    expected = derive_candidate_cards(
        base_tree=base_tree,
        candidate_tree=candidate_tree,
        coordinate=coordinate,
        artifact_kinds=artifact_kinds,
    )
    if expected != dict(candidate_tree):
        raise ProposalIntegrityError("candidate derivative cards do not reproduce exactly")


__all__ = [
    "CARD_NAMESPACE",
    "CARD_RENDERER_DIGEST",
    "CARD_RENDERER_IMPLEMENTATION",
    "CARD_TEMPLATE_DIGESTS",
    "candidate_card_path",
    "derive_candidate_cards",
    "is_candidate_card_path",
    "render_candidate_card",
    "render_removal_card",
    "verify_candidate_cards",
]
