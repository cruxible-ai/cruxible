"""Disposable reuse of lowering whose inputs have no operational dependencies.

This is not a preflight certificate cache. Reference validation, candidate
evaluation, receipt resolution and proposal authorization still run on every
request. Only explicitly admitted payload families use this optimization.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from cruxible_client.contracts.authoring.models import (
    ApprovalPolicyAuthoringPayloadV1,
    AuthoringExactContentObjectV1,
    AuthoringIntentV1,
    AuthoringPayloadV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimTypeAuthoringPayloadV1,
    ProcedureRuntimePolicyAuthoringPayloadV1,
    QueryDefinitionAuthoringPayloadV1,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.proposal_models import (
    AuthenticatedActor,
    ProposalReceiveLimits,
)
from cruxible_core.playbill.authoring.lowering import LoweredAuthoring
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate

# Bounds are retained serialized/tree bytes, not a claim about Python heap size.
MAX_ENTRIES = 4
MAX_RETAINED_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class _Entry:
    lowered: LoweredAuthoring
    bodies: tuple[str, ...]
    weight: int


_caches: weakref.WeakKeyDictionary[PlaybillInstance, OrderedDict[str, _Entry]] = (
    weakref.WeakKeyDictionary()
)
_lock = threading.Lock()


def _eligible(payload: object) -> bool:
    if isinstance(payload, ChangeSetAuthoringPayloadV1):
        return all(_eligible(member) for member in payload.members)
    if isinstance(payload, ClaimAuthoringPayloadV1):
        # Working selections consult projection registrations; existing captures
        # consult producer receipts and other mutable provenance. Neither belongs
        # in a cache keyed only by immutable authoring and accepted state.
        return isinstance(payload.source, SelfSourceBodyV1)
    return isinstance(
        payload,
        SubjectAuthoringPayloadV1
        | ClaimTypeAuthoringPayloadV1
        | QueryDefinitionAuthoringPayloadV1
        | ApprovalPolicyAuthoringPayloadV1
        | ProcedureRuntimePolicyAuthoringPayloadV1,
    )


def _generated_bodies(payload: AuthoringPayloadV1, lowered: LoweredAuthoring) -> tuple[str, ...]:
    """The bodies written by eligible lowering, including each new envelope."""

    members = payload.members if isinstance(payload, ChangeSetAuthoringPayloadV1) else (payload,)
    digests: set[str] = set()
    for member in members:
        if not isinstance(member, ClaimAuthoringPayloadV1):
            continue
        assert isinstance(member.source, SelfSourceBodyV1)
        digests.add("sha256:" + hashlib.sha256(member.source.content).hexdigest())
        if isinstance(member.statement.object, AuthoringExactContentObjectV1):
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
    with _lock:
        cache = _caches.setdefault(instance, OrderedDict())
        entry = cache.get(key)
        if entry is not None:
            cache.move_to_end(key)
    if entry is not None:
        store = instance.body_store()
        if all(store.verify(digest) for digest in entry.bodies):
            # Fresh nested containers prevent downstream mutation poisoning reuse.
            return copy.deepcopy(entry.lowered)
        # Missing generated bodies are recreated by normal lowering. Corrupt CAS
        # bytes raise exactly as storing those bodies during lowering would.
        with _lock:
            cache.pop(key, None)
    lowered = compute()
    bodies = _generated_bodies(intent.payload, lowered)
    weight = (
        len(inputs)
        + sum(len(path.encode()) + len(content) for path, content in lowered.proposed_tree.items())
        + len(canonical_bytes(lowered.resolved_authoring))
        + sum(len(path.encode()) + len(content) for path, content in lowered.changed_members)
    )
    if weight <= MAX_RETAINED_BYTES:
        entry = _Entry(copy.deepcopy(lowered), bodies, weight)
        with _lock:
            cache[key] = entry
            cache.move_to_end(key)
            while len(cache) > MAX_ENTRIES or sum(item.weight for item in cache.values()) > (
                MAX_RETAINED_BYTES
            ):
                cache.popitem(last=False)
    return lowered
