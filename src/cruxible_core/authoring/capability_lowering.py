"""Lower one authored Capability version onto its immutable artifact bytes.

The author names the request and result Contracts ``name@N``; lowering pins each
to the digest of the version accepted (or staged earlier in the same change
set), names version N-1 as the predecessor of every later version, and then
runs the capability acceptance law against the staged tree, so every law
refusal reaches the author here, typed and with its repair. Acceptance runs the
same law again over the candidate tree.

Reads, by path: the two Contracts, the predecessor version and this version's
own path. Nothing grows with the number of capabilities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cruxible_client.contracts.authoring.models import CapabilityAuthoringPayload
from cruxible_client.contracts.capabilities import (
    CAPABILITY_EFFECT_CHANGED,
    CAPABILITY_IMMUTABLE,
    CAPABILITY_PIN_DIGEST_MISMATCH,
    CAPABILITY_PIN_UNRESOLVED,
    CAPABILITY_PREDECESSOR_MISMATCH,
    CAPABILITY_PREDECESSOR_MISSING,
    CAPABILITY_RESERVED,
    CapabilityFormatError,
    capability_digest,
    capability_path,
    evaluate_capability_law,
    parse_capability,
    render_capability,
    validate_capability,
)
from cruxible_client.contracts.shapes import (
    ContractRef,
    contract_digest,
    contract_path,
    parse_contract,
)
from cruxible_core.capabilities.artifact import BUILTIN_CAPABILITY_NAMES


@dataclass(frozen=True)
class CapabilityLoweringRefusal(Exception):
    """One typed authoring refusal; ``lowering`` re-raises it with its repair."""

    code: str
    message: str
    repair: str


def _repair(code: str, payload: CapabilityAuthoringPayload) -> str:
    spelling = f"{payload.name}@{payload.version}"
    return {
        CAPABILITY_IMMUTABLE: f"Author {payload.name}@{payload.version + 1} instead.",
        CAPABILITY_PREDECESSOR_MISSING: f"Author {payload.name}@{payload.version - 1} first.",
        CAPABILITY_PREDECESSOR_MISMATCH: (
            f"Author {spelling} against the accepted version {payload.version - 1}."
        ),
        CAPABILITY_EFFECT_CHANGED: (
            "Keep the effect of the earlier versions, or declare a new capability name."
        ),
        CAPABILITY_PIN_UNRESOLVED: "Accept the Contract first, or in the same change set.",
        CAPABILITY_PIN_DIGEST_MISMATCH: (
            "Write the Contract as name@N without a digest; lowering pins the accepted version."
        ),
        CAPABILITY_RESERVED: (
            "Declare the capability under your own prefix; workspace. names are core's."
        ),
    }.get(code, "Repair the capability's name, version, effect and Contracts.")


def _accepted_contract_digest(tree: Mapping[str, bytes], ref: ContractRef) -> str | None:
    path = contract_path(ref.name, ref.version)
    content = tree.get(path)
    return None if content is None else contract_digest(parse_contract(content, path=path)).tagged


def render_capability_member(
    payload: CapabilityAuthoringPayload, *, tree: Mapping[str, bytes]
) -> tuple[str, bytes, str]:
    """``(path, bytes, digest)`` of one capability version, pinned and judged against ``tree``."""

    def pin(role: str, ref: ContractRef) -> ContractRef:
        digest = _accepted_contract_digest(tree, ref)
        if digest is None:
            raise CapabilityLoweringRefusal(
                CAPABILITY_PIN_UNRESOLVED,
                f"{payload.name}@{payload.version} {role}: {ref.spelling} is not an "
                "accepted Contract.",
                f"Accept {ref.spelling} first, or in the same change set.",
            )
        if ref.digest is not None and ref.digest != digest:
            raise CapabilityLoweringRefusal(
                CAPABILITY_PIN_DIGEST_MISMATCH,
                f"{ref.spelling} is accepted at {digest}, not {ref.digest}.",
                _repair(CAPABILITY_PIN_DIGEST_MISMATCH, payload),
            )
        return ref.model_copy(update={"digest": digest})

    predecessor: str | None = None
    if payload.version > 1:
        previous_path = capability_path(payload.name, payload.version - 1)
        previous = tree.get(previous_path)
        if previous is None:
            raise CapabilityLoweringRefusal(
                CAPABILITY_PREDECESSOR_MISSING,
                f"{payload.name}@{payload.version} succeeds version {payload.version - 1}, "
                "which is not accepted.",
                _repair(CAPABILITY_PREDECESSOR_MISSING, payload),
            )
        predecessor = capability_digest(parse_capability(previous, path=previous_path)).tagged
    try:
        capability = validate_capability(
            {
                "name": payload.name,
                "version": payload.version,
                "description": payload.description,
                "effect": payload.effect,
                "in": pin("in", payload.in_).model_dump(mode="json"),
                "out": pin("out", payload.out).model_dump(mode="json"),
                **({"predecessor_digest": predecessor} if predecessor else {}),
            }
        )
    except CapabilityFormatError as exc:
        raise CapabilityLoweringRefusal(
            "cruxible.capability.invalid",
            str(exc),
            "Repair the capability's name, version, description, effect and Contracts.",
        ) from exc
    path = capability_path(capability.name, capability.version)
    content = render_capability(capability)
    problems = evaluate_capability_law(
        capability,
        path=path,
        lookup=tree.get,
        parent_content=tree.get(path),
        content=content,
        builtin_capabilities=BUILTIN_CAPABILITY_NAMES,
    )
    if problems:
        first = problems[0]
        raise CapabilityLoweringRefusal(first.code, first.message, _repair(first.code, payload))
    return path, content, capability_digest(capability).tagged


__all__ = ["CapabilityLoweringRefusal", "render_capability_member"]
