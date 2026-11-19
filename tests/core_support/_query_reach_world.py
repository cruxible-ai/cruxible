"""A query world of links, keyed Subjects and pair Subjects, accepted raw.

Every Claim cites its own coordinator self-source Capture (``_pair_real``), so
verdicts are real: a fact with ``effective_until`` in the past is ``stale`` and
one with ``effective_from`` in the future is ``uncovered``, both hidden from a
``current`` read.

Kinds (prefix ``qr.``) mirror the security kit's shapes:

- ``unit`` (repo, surface) with ``status`` (an enum) and ``tag`` (many); ``finding`` (locus) with
  ``found_in`` (many, a unit) and ``severity``: the audit reads;
- ``component`` (purl) with ``affected_by`` (many, a vuln); ``vuln`` (vid);
  ``inventory`` (iid);
- ``occurrence`` (inventory, component) with ``scope``, and ``exposure``
  (component, vulnerability) with ``justification``: pair Subjects.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import (
    ClaimInverse,
    ClaimInverseSlotKey,
    ClaimType,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import NoneClaimObject, parse_claim, render_claim
from cruxible_client.contracts.identity_normalizers import NO_NORMALIZER, PurlTypeNormalizer
from cruxible_client.contracts.subject_kinds import (
    SubjectEnd,
    SubjectKeyRule,
    SubjectKind,
    SubjectKindKeys,
    render_key_slug,
    render_subject_kind,
    subject_kind_path,
)
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._pair_real import (
    OBSERVED_AT,
    SELF_SOURCE,
    _self_evidence,
    accept_raw,
    real_claim,
)
from tests.core_support._pair_support import literal, shell, to
from tests.core_support._support import initialize_local
from tests.support.claim_types import claim_type as build_claim_type

UNIT = "qr.unit"
FINDING = "qr.finding"
COMPONENT = "qr.component"
VULN = "qr.vuln"
INVENTORY = "qr.inventory"
OCCURRENCE = "qr.occurrence"
EXPOSURE = "qr.exposure"
#: A kind keyed by a purl under the ``purl-type`` normalizer: an operand compared
#: with its key takes that normalizer, which refuses text that is no purl.
PACKAGE = "qr.package"

REPO = f"{UNIT}.repo"
SURFACE = f"{UNIT}.surface"
STATUS = f"{UNIT}.status"
TAG = f"{UNIT}.tag"
#: A literal string slot that admits an inverse: it can hold a visible none.
REVIEW = f"{UNIT}.review"
LOCUS = f"{FINDING}.locus"
FOUND_IN = f"{FINDING}.found_in"
SEVERITY = f"{FINDING}.severity"
PURL = f"{COMPONENT}.purl"
AFFECTED_BY = f"{COMPONENT}.affected_by"
VID = f"{VULN}.vid"
IID = f"{INVENTORY}.iid"
O_INVENTORY = f"{OCCURRENCE}.inventory"
O_COMPONENT = f"{OCCURRENCE}.component"
SCOPE = f"{OCCURRENCE}.scope"
E_COMPONENT = f"{EXPOSURE}.component"
E_VULN = f"{EXPOSURE}.vulnerability"
JUSTIFICATION = f"{EXPOSURE}.justification"
P_PURL = f"{PACKAGE}.purl"
P_TIER = f"{PACKAGE}.tier"

#: Evaluation instant of every read: after every observation.
WHEN = OBSERVED_AT + timedelta(days=1)
PAST = OBSERVED_AT - timedelta(days=30)
FUTURE = OBSERVED_AT + timedelta(days=30)


def _type(
    predicate: str, value: Any = None, *, many: bool = False, inverse: bool = False
) -> ClaimType:
    extra: dict[str, Any] = {}
    if inverse:
        # A none says the whole one-valued slot holds nothing.
        extra["inverse"] = ClaimInverse(
            key=ClaimInverseSlotKey(kind="slot"), evidence_rules=("self",)
        )
    return build_claim_type(
        predicate,
        value_type={"type": "string"} if value is None else value,
        permitted_roles=("observation",),
        evidence_admission_policy=_self_evidence(),
        revision_evidence="replace",
        cardinality="many" if many else "one",
        **extra,
    )


TYPES: dict[str, ClaimType] = {
    item.predicate: item
    for item in (
        _type(REPO),
        _type(SURFACE),
        _type(STATUS, {"type": "string", "enum": ["blocked", "checked", "deferred"]}),
        _type(TAG, many=True),
        _type(REVIEW, inverse=True),
        _type(LOCUS),
        _type(FOUND_IN, (UNIT,), many=True),
        _type(SEVERITY),
        _type(PURL),
        _type(AFFECTED_BY, (VULN,), many=True),
        _type(VID),
        _type(IID),
        _type(O_INVENTORY, (INVENTORY,)),
        _type(O_COMPONENT, (COMPONENT,)),
        _type(SCOPE, inverse=True),
        _type(E_COMPONENT, (COMPONENT,)),
        _type(E_VULN, (VULN,)),
        _type(JUSTIFICATION),
        _type(P_PURL),
        _type(P_TIER),
    )
}

KEYS: dict[str, tuple[str, ...]] = {
    UNIT: (REPO, SURFACE),
    FINDING: (LOCUS,),
    COMPONENT: (PURL,),
    VULN: (VID,),
    INVENTORY: (IID,),
    OCCURRENCE: (O_INVENTORY, O_COMPONENT),
    EXPOSURE: (E_COMPONENT, E_VULN),
    PACKAGE: (P_PURL,),
}
NORMALIZERS: dict[str, Any] = {
    P_PURL: PurlTypeNormalizer(version=1, identity="package"),
}


def _kind(kind: str) -> SubjectKind:
    return SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=kind),
        identity_rule=SubjectKindKeys(
            parts=tuple(
                SubjectKeyRule(claim_type=key, normalizer=NORMALIZERS.get(key, NO_NORMALIZER))
                for key in KEYS[kind]
            ),
            uniqueness="unique",
        ),
    )


_FLUSH_AT = 4000


class ReachWorld:
    """One instance plus a pending change; ``accept`` lands it as one raw proposal."""

    def __init__(self, tmp_path: Path) -> None:
        self.instance: PlaybillInstance
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.instance, _owner = initialize_local(tmp_path)
        vocabulary: dict[str, bytes] = {
            capture_contract_path(SELF_SOURCE.identity.name): render_capture_contract(SELF_SOURCE)
        }
        for item in TYPES.values():
            vocabulary[claim_type_path(item.predicate)] = render_claim_type(item)
        for kind in KEYS:
            vocabulary[subject_kind_path(kind)] = render_subject_kind(_kind(kind))
        accepted = accept_raw(self.instance, vocabulary, name="reach-vocabulary")
        assert accepted.candidate is not None, accepted.evaluation.diagnostics
        self._tree = dict(self.instance.tree_at(self.instance.accepted_coordinate().git_oid))
        self._changes: dict[str, bytes] = {}
        self._number = 1
        self._proposals = 0

    def subject(self, kind: str, *parts: str) -> str:
        """A keyed Subject: a literal part is its text, a Subject part an end's path."""

        if len(self._changes) > _FLUSH_AT:
            # A proposal carries at most 5,000 changed members.
            self.accept()
        values = [SubjectEnd(path=part) if part.startswith("subjects/") else part for part in parts]
        identifier = render_key_slug(values)
        at = subject_path(kind, identifier)
        if at in self._tree:
            return at
        self._changes[at] = self._tree[at] = render_subject(shell(kind, identifier))
        for predicate, part in zip(KEYS[kind], parts, strict=True):
            self.claim(predicate, part, at)
        return at

    def claim(
        self,
        predicate: str,
        value: str | None,
        at: str,
        *,
        effective_from: datetime | None = None,
        effective_until: datetime | None = None,
    ) -> str:
        """A Claim of ``predicate`` about ``at``: a Subject path names a Subject object.

        ``None`` is an inverse (``none``): the slot holds nothing.
        """

        obj: Any
        if value is None:
            obj = NoneClaimObject()
        else:
            obj = to(value) if value.startswith("subjects/") else literal(value)
        path, content = real_claim(
            self.instance,
            self._tree,
            10_000_000 + self._number,
            predicate,
            obj,
            subject=at,
            claim_type=TYPES[predicate],
            effective_from=effective_from,
            effective_until=effective_until,
        )
        self._number += 1
        self._changes[path] = self._tree[path] = content
        return path

    def retire(self, claim: str) -> None:
        """Retire ``claim`` lifecycle-only."""

        from cruxible_core.claims.key_claims import retired_key_claim

        self._changes[claim] = self._tree[claim] = render_claim(
            retired_key_claim(parse_claim(self._tree[claim], path=claim))
        )

    def accept(self) -> None:
        if not self._changes:
            return
        self._proposals += 1
        accepted = accept_raw(self.instance, self._changes, name=f"reach-{self._proposals}")
        assert accepted.candidate is not None, accepted.evaluation.diagnostics
        self._changes = {}
        self._tree = dict(self.instance.tree_at(self.instance.accepted_coordinate().git_oid))


def kinds_of(paths: Sequence[str]) -> list[str]:
    return [path.split("/", 2)[1] for path in paths]


__all__ = [
    "PACKAGE",
    "P_PURL",
    "P_TIER",
    "AFFECTED_BY",
    "COMPONENT",
    "EXPOSURE",
    "E_COMPONENT",
    "E_VULN",
    "FINDING",
    "FOUND_IN",
    "FUTURE",
    "IID",
    "INVENTORY",
    "JUSTIFICATION",
    "LOCUS",
    "OCCURRENCE",
    "O_COMPONENT",
    "O_INVENTORY",
    "PAST",
    "PURL",
    "REPO",
    "SCOPE",
    "SEVERITY",
    "STATUS",
    "SURFACE",
    "TAG",
    "REVIEW",
    "TYPES",
    "UNIT",
    "VID",
    "VULN",
    "WHEN",
    "ReachWorld",
]
