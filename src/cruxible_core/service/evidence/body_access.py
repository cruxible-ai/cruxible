"""Who may read a Capture's bytes: its CaptureKind's ``body_access`` (C14).

A Capture's kind is resolved from the contract digest its envelope opens with
(the leading bytes every canonical envelope begins with, the same bytes the
envelope index classifies by), never from the envelope's body-derived source or
selector metadata. Every body-reading surface asks here, per Capture, before it
reads an envelope or a body: a restricted kind's Capture is withheld from a
caller without ``cruxible_body_read_restricted`` and named in a typed
``body_restricted`` notice. A Capture whose kind no accepted version resolves is
withheld as restricted; one whose bytes do not open as an envelope has no kind
and nothing to withhold.

Cost: one bounded head read per Capture and one contract resolution per
distinct contract digest per resolver (a resolver lives for one request).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from cruxible_client.contracts.capture_reads import BodyRestricted
from cruxible_client.contracts.captures import (
    CaptureBodyAccess,
    CaptureContractAny,
    capture_body_access,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.capture_envelope_index import ENVELOPE_HEAD_LENGTH, envelope_contract


@dataclass(frozen=True)
class CaptureKindAccess:
    """One Capture's kind and access, as resolved without reading its envelope.

    ``contract_digest`` is None when the bytes are absent or are not an
    envelope; ``kind`` is None when no accepted version resolves the digest.
    """

    capture_digest: str
    contract_digest: str | None
    kind: str | None
    contract_path: str | None
    contract: CaptureContractAny | None
    access: CaptureBodyAccess | None

    @property
    def restricted(self) -> bool:
        """Restricted by its kind, or a Capture whose kind does not resolve."""

        return self.contract_digest is not None and self.access != "open"

    def withheld_from(self, access: BodyAccessContext) -> bool:
        return self.restricted and not access.can_read_restricted


@dataclass
class CaptureAccessResolver:
    """Per-request memo of Capture kinds at one accepted coordinate."""

    instance: PlaybillInstance
    coordinate: AcceptedProjectionCoordinate
    _contracts: dict[str, tuple[str | None, str | None, CaptureContractAny | None]] = field(
        default_factory=dict
    )
    _captures: dict[str, CaptureKindAccess] = field(default_factory=dict)

    def capture(self, digest: str) -> CaptureKindAccess:
        known = self._captures.get(digest)
        if known is None:
            known = self.captures((digest,))[digest]
        return known

    def captures(self, digests: Iterable[str]) -> dict[str, CaptureKindAccess]:
        wanted = tuple(dict.fromkeys(digests))
        store = self.instance.body_store()
        heads = {
            digest: envelope_contract(store.peek(digest, ENVELOPE_HEAD_LENGTH))
            for digest in wanted
            if digest not in self._captures
        }
        self._resolve_contracts({named for named in heads.values() if named is not None})
        for digest, named in heads.items():
            kind, path, contract = (None, None, None) if named is None else self._contracts[named]
            self._captures[digest] = CaptureKindAccess(
                capture_digest=digest,
                contract_digest=named,
                kind=kind,
                contract_path=path,
                contract=contract,
                access=None if contract is None else capture_body_access(contract),
            )
        return {digest: self._captures[digest] for digest in wanted}

    def _resolve_contracts(self, digests: set[str]) -> None:
        missing = sorted(digests - set(self._contracts))
        if not missing:
            return
        unresolved: list[str] = []
        with self.instance.bind_accepted_projection(self.coordinate) as projection:
            connection = projection.typed.connection
            for digest in missing:
                row = connection.execute(
                    "SELECT identity, path FROM capture_contracts WHERE artifact_digest=?",
                    (digest,),
                ).fetchone()
                contract = None if row is None else projection.typed.source(str(row[0]))
                if contract is None:
                    unresolved.append(digest)
                    continue
                self._contracts[digest] = (str(row[0]), str(row[1]), contract)
        public = AcceptedCoordinate.from_internal(self.coordinate)
        for digest in unresolved:
            # A superseded version: accepted history holds every version.
            found = self.instance.accepted_capture_contract_version(public, digest)
            self._contracts[digest] = (
                (None, None, None)
                if found is None
                else (found.contract.identity.qualified, found.path, found.contract)
            )

    def withheld(
        self, digests: Iterable[str], access: BodyAccessContext
    ) -> tuple[dict[str, CaptureKindAccess], BodyRestricted | None]:
        """The Captures among ``digests`` withheld from ``access``, and their notice."""

        if access.can_read_restricted:
            return {}, None
        withheld = {
            digest: item
            for digest, item in self.captures(digests).items()
            if item.withheld_from(access)
        }
        return withheld, body_restricted_notice(withheld.values())


def body_restricted_notice(items: Iterable[CaptureKindAccess]) -> BodyRestricted | None:
    """One typed notice naming every withheld Capture and its kind, or None."""

    listed = tuple(items)
    if not listed:
        return None
    return BodyRestricted(
        capture_digests=tuple(sorted({item.capture_digest for item in listed})),
        capture_kinds=tuple(sorted({item.kind for item in listed if item.kind is not None})),
    )


def capture_access_resolver(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate | None = None
) -> CaptureAccessResolver:
    return CaptureAccessResolver(
        instance, instance.accepted_coordinate() if coordinate is None else coordinate
    )


__all__ = [
    "CaptureAccessResolver",
    "CaptureKindAccess",
    "body_restricted_notice",
    "capture_access_resolver",
]
