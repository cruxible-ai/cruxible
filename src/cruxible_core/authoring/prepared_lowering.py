"""Disposable reuse of lowering whose inputs have no operational dependencies.

This is not a preflight certificate cache. Reference validation, candidate
evaluation, receipt resolution and proposal authorization still run on every
request. Only explicitly admitted payload families use this optimization.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Callable

from cruxible_client.contracts.authoring.models import (
    ApprovalPolicyAuthoringPayload,
    AuthoringExactContentObject,
    AuthoringIntentV1,
    AuthoringPayload,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayloadV1,
    ClaimTypeAuthoringPayload,
    ProcedureRuntimePolicyAuthoringPayload,
    QueryDefinitionAuthoringPayload,
    SelfSourceBody,
    SubjectAuthoringPayload,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.proposal_models import (
    AuthenticatedActor,
    ProposalReceiveLimits,
)
from cruxible_core.authoring.lowering import LoweredAuthoring
from cruxible_core.derived.derived_runtime import BoundedCache
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance

# Bounds are retained serialized/tree bytes, not a claim about Python heap size.
MAX_ENTRIES = 4
MAX_RETAINED_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class _Entry:
    lowered: LoweredAuthoring
    bodies: tuple[str, ...]
    weight: int


def _cache(instance: PlaybillInstance) -> BoundedCache[_Entry]:
    return instance.derived.memo(
        "prepared_lowering", max_entries=MAX_ENTRIES, max_bytes=MAX_RETAINED_BYTES
    )


def _eligible(payload: object) -> bool:
    if isinstance(payload, ChangeSetAuthoringPayload):
        return all(_eligible(member) for member in payload.members)
    if isinstance(payload, ClaimAuthoringPayloadV1):
        # Working selections consult projection registrations; existing captures
        # consult producer receipts and other mutable provenance. Neither belongs
        # in a cache keyed only by immutable authoring and accepted state.
        return isinstance(payload.source, SelfSourceBody)
    return isinstance(
        payload,
        SubjectAuthoringPayload
        | ClaimTypeAuthoringPayload
        | QueryDefinitionAuthoringPayload
        | ApprovalPolicyAuthoringPayload
        | ProcedureRuntimePolicyAuthoringPayload,
    )


def _generated_bodies(payload: AuthoringPayload, lowered: LoweredAuthoring) -> tuple[str, ...]:
    """The bodies written by eligible lowering, including each new envelope."""

    members = payload.members if isinstance(payload, ChangeSetAuthoringPayload) else (payload,)
    digests: set[str] = set()
    for member in members:
        if not isinstance(member, ClaimAuthoringPayloadV1):
            continue
        assert isinstance(member.source, SelfSourceBody)
        digests.add("sha256:" + hashlib.sha256(member.source.content).hexdigest())
        if isinstance(member.statement.object, AuthoringExactContentObject):
            digests.add("sha256:" + hashlib.sha256(member.statement.object.content).hexdigest())
    resolved = lowered.resolved_authoring
    rows = resolved.get("members", [resolved])
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        capture_digest = row.get("capture_digest")
        if capture_digest is not None:
            assert isinstance(capture_digest, str)
            digests.add(capture_digest)
    return tuple(sorted(digests))


def reuse_lowering(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor: AuthenticatedActor,
    accepted: AcceptedCoordinate,
    receive_limits: ProposalReceiveLimits,
    compute: Callable[[], LoweredAuthoring],
) -> LoweredAuthoring:
    """Reuse exact eligible inputs, or recover by ordinary lowering on a miss."""

    if not _eligible(intent.payload):
        return compute()
    # Serialize actual nested values anew: frozen Pydantic models can contain
    # mutable dictionaries. Neither a remembered digest nor object identity is
    # sufficient. Only execution results are excluded from the input identity.
    inputs = canonical_bytes(
        {
            "intent": intent.model_dump(
                mode="json", exclude={"last_preflight", "candidate_status"}
            ),
            "actor": actor.model_dump(mode="json"),
            "accepted": accepted.model_dump(mode="json"),
            "descriptor": instance.descriptor.model_dump(mode="json"),
            "receive_limits": receive_limits.model_dump(mode="json"),
        }
    )
    key = hashlib.sha256(inputs).hexdigest()
    cache = _cache(instance)
    generation = cache.generation
    entry = cache.get(key)
    if entry is not None:
        store = instance.body_store()
        if all(store.verify(digest) for digest in entry.bodies):
            # Fresh nested containers prevent downstream mutation poisoning reuse.
            return copy.deepcopy(entry.lowered)
        # Missing generated bodies are recreated by normal lowering. Corrupt CAS
        # bytes raise exactly as storing those bodies during lowering would.
        cache.pop(key)
    lowered = compute()
    bodies = _generated_bodies(intent.payload, lowered)
    assert isinstance(lowered.proposed_tree, SnapshotTree)  # LoweredAuthoring seals its tree.
    # The entry keeps its accepted root alive, even after the root cache lets
    # it go, so that root counts at its resident size: its rows, which for a
    # lazily read tree are references rather than blob bytes. Counting its
    # whole logical size instead made a real-size instance's lowering too heavy
    # to keep, and submission recomputed it.
    root = lowered.proposed_tree._parent or lowered.proposed_tree
    weight = (
        len(inputs)
        + root._resident_bytes
        + len(canonical_bytes(lowered.resolved_authoring))
        + sum(len(path.encode()) + len(content) for path, content in lowered.changed_members)
    )
    if weight <= MAX_RETAINED_BYTES:
        entry = _Entry(copy.deepcopy(lowered), bodies, weight)
        cache.put(key, entry, weight=weight, expected_generation=generation)
    return lowered
