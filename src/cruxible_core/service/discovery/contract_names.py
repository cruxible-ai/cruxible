"""CaptureContracts by name, never by digest, for every read verb.

``orient`` predicate descriptors, ``query`` ClaimType rows and ``get`` cards all
show the evidence a ClaimType admits as CaptureContract names:

- a v6 ClaimType's rules (``ClaimEvidenceAdmissionRule``) name contracts by
  identity already, so the name is read off the ``ArtifactRef``;
- a v5 (or older) rule names exact contract versions by digest, and each digest
  resolves through accepted state to the identity it is a version of;
- a digest that resolves to nothing shows as ``unresolved:<12 hex>``, never as
  the bare digest.
"""

from __future__ import annotations

import sqlite3

from cruxible_client.contracts.captures import AcceptedCaptureContract
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionRule
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance

_QUALIFIER = "CaptureContract:"


def unresolved_name(digest: str) -> str:
    """How a contract digest that resolves to no identity is shown."""

    return f"unresolved:{digest.rpartition(':')[2][:12]}"


class CaptureContractNames:
    """Resolve the CaptureContracts evidence rules admit to their identity names.

    ``connection`` is an open accepted projection at ``coordinate``; when given,
    a digest of a contract's current version resolves from its index row before
    falling back to accepted history, which also finds superseded versions.
    Names are bare (``fixture.reports``) unless ``qualified`` asks for the
    ``CaptureContract:`` reference form.
    """

    def __init__(
        self,
        instance: PlaybillInstance,
        coordinate: AcceptedProjectionCoordinate,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._instance = instance
        self._at = AcceptedCoordinate.from_internal(coordinate)
        self._connection = connection
        self._versions: dict[str, AcceptedCaptureContract | None] = {}
        self._names: dict[str, str | None] = {}
        self._lineages: dict[str, tuple[str, ...]] = {}

    def version_of(self, digest: str) -> AcceptedCaptureContract | None:
        """The accepted contract version a digest names, even if superseded."""

        if digest not in self._versions:
            try:
                found = self._instance.accepted_capture_contract_version(self._at, digest)
            except CruxibleError:
                found = None
            self._versions[digest] = found
        return self._versions[digest]

    def _identity_name(self, digest: str) -> str | None:
        if digest not in self._names:
            name: str | None = None
            if self._connection is not None:
                row = self._connection.execute(
                    "SELECT identity FROM capture_contracts WHERE artifact_digest=?", (digest,)
                ).fetchone()
                if row is not None:
                    name = str(row[0]).removeprefix(_QUALIFIER)
            if name is None:
                version = self.version_of(digest)
                name = None if version is None else version.contract.identity.name
            self._names[digest] = name
        return self._names[digest]

    def name(self, digest: str, *, qualified: bool = False) -> str:
        """The identity a contract digest is a version of, or ``unresolved:<prefix>``."""

        found = self._identity_name(digest)
        if found is None:
            return unresolved_name(digest)
        return _QUALIFIER + found if qualified else found

    def admitted(self, claim_type: ClaimType, *, qualified: bool = False) -> tuple[str, ...]:
        """The contract names a ClaimType's evidence rules admit, in byte order."""

        names: set[str] = set()
        for rule in claim_type.evidence_admission_policy.rules:
            if isinstance(rule, ClaimEvidenceAdmissionRule):
                names.update(
                    item.target.qualified if qualified else item.target.name
                    for item in rule.capture_contracts
                )
            else:
                names.update(
                    self.name(digest, qualified=qualified)
                    for digest in rule.capture_contract_digests
                )
        return tuple(sorted(names, key=lambda item: item.encode("utf-8")))

    @staticmethod
    def names_by_digest(claim_type: ClaimType) -> bool:
        """Whether any of this ClaimType's evidence rules still names contracts by digest."""

        rules = claim_type.evidence_admission_policy.rules
        return any(
            bool(rule.capture_contract_digests)
            for rule in rules
            if not isinstance(rule, ClaimEvidenceAdmissionRule)
        )

    def lineage(self, identity: str) -> tuple[str, ...]:
        """Every accepted version digest of one contract identity, oldest first."""

        if identity not in self._lineages:
            with self._instance.accepted_history_reader(at=self._at) as history:
                occurrences = history.occurrences(identity)
            ordered: list[str] = []
            for location in occurrences:
                if location.artifact_digest not in ordered:
                    ordered.append(location.artifact_digest)
            self._lineages[identity] = tuple(ordered)
        return self._lineages[identity]

    def version_number(self, identity: str, digest: str) -> int:
        """Which accepted version of ``identity`` a digest is, counting from 1."""

        lineage = self.lineage(identity)
        return lineage.index(digest) + 1 if digest in lineage else len(lineage)


__all__ = ["CaptureContractNames", "unresolved_name"]
