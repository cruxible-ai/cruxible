"""Byte-bound immutable Claim derivations shared by the write pipeline.

The strict parse owns this value, not a request or a tree. Its path and exact
bytes are the parse memo's key; changing any of them derives a new value.
Acceptance-dependent rows (revision, verdict and explanations) stay with the
generation compiler. Only intrinsic rows can travel with a member.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cruxible_client.contracts.canonical import file_digest
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    claim_artifact_digest,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.content_memo import request_derive, seal

if TYPE_CHECKING:
    from cruxible_client.contracts.projection_extensions import ProjectionFact


@dataclass(frozen=True, slots=True)
class SealedClaimMember:
    path: str
    content: bytes
    claim: ClaimArtifact
    input_digest: str
    artifact_digest: str
    statement_digest: str
    facts: tuple[ProjectionFact, ...]


_MEMBERS: OrderedDict[tuple[str, bytes], SealedClaimMember] = OrderedDict()
_LOCK = threading.RLock()
_MAX_BYTES = 32 * 1024 * 1024
_WEIGHT = 0


def _clear() -> None:
    """Forget retained members for cold-path differential tests."""
    global _WEIGHT
    with _LOCK:
        _MEMBERS.clear()
        _WEIGHT = 0


def sealed_claim_member(content: bytes, *, path: str) -> SealedClaimMember:
    """The request owns the member even if the bounded process memo evicts it."""
    return request_derive(
        ("core.sealed_claim_member", path, content),
        lambda: _sealed_claim_member(content, path=path),
    )


def _sealed_claim_member(content: bytes, *, path: str) -> SealedClaimMember:
    global _WEIGHT
    cacheable = type(content) is bytes and type(path) is str
    key = (path, content)
    # Hold one lock through construction: concurrent consumers also derive the
    # member once. Only intrinsic computation occurs here, with no I/O.
    with _LOCK:
        found = _MEMBERS.get(key) if cacheable else None
        if found is not None:
            _MEMBERS.move_to_end(key)
            return found
        claim = parse_claim(content, path=path)
        from cruxible_core.compiler.projection_artifacts import _claim_static_facts

        input_digest = file_digest(content).tagged
        artifact_digest = claim_artifact_digest(claim).tagged
        statement_digest = claim_statement_digest(claim.statement).tagged
        facts = _claim_static_facts(
            claim,
            path=path,
            input_digest=input_digest,
            artifact_digest=artifact_digest,
            statement_digest=statement_digest,
        )
        # ProjectionFact is only shallowly frozen until sealed. Never retain a
        # caller-mutable normalized value in the member's intrinsic row stream.
        if not all(seal(fact) for fact in facts):
            raise TypeError("intrinsic Claim projection facts must be sealable")
        member = SealedClaimMember(
            path, content, claim, input_digest, artifact_digest, statement_digest, facts
        )
        # Account for the bytes, parsed model and normalized facts. A large
        # member bypasses retention; no cache grows with the instance.
        weight = len(content) * 16 + len(path)
        if cacheable and weight <= _MAX_BYTES // 16:
            _MEMBERS[key] = member
            _WEIGHT += weight
            while _WEIGHT > _MAX_BYTES:
                old_key, old = _MEMBERS.popitem(last=False)
                _WEIGHT -= len(old.content) * 16 + len(old_key[0])
        return member
