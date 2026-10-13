"""Shared signed-input verification for pending and accepted attestations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator, Literal, Mapping, NoReturn, Protocol

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    CaptureContract,
    CaptureObjectStoreProtocol,
    CaptureProductionUnproven,
    ProducerReceiptResolverProtocol,
    capture_contract_digest,
    capture_contract_is_self_asserted,
    capture_contract_path,
    parse_capture_contract,
    parse_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationResolvedArtifact,
    ClaimAttestationStatement,
    verify_claim_attestation_v2_principal,
)
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimArtifactAny,
    ClaimBacking,
    ClaimLawEvidenceAny,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    evaluate_capture_evidence_admissions,
    parse_claim,
)
from cruxible_client.contracts.errors import CruxibleError, FormatError
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.providers import parse_provider, provider_digest, provider_path
from cruxible_client.contracts.source_references import LedgerSourceReference
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.derived.derived_state import paths_with_prefix
from cruxible_core.indexes.projection import AcceptedCoordinate


class ActivePrincipalReader(Protocol):
    """The exact accepted principal lookup needed by attestation verification."""

    def require_active(self, principal_id: str) -> PrincipalRecord: ...


def _capture_contracts(
    tree: Mapping[str, bytes],
) -> dict[str, AcceptedCaptureContract]:
    result: dict[str, AcceptedCaptureContract] = {}
    # Filter the paths before ordering or reading any: only CaptureContracts are read.
    for path in sorted(
        paths_with_prefix(tree, "capture-contracts/"), key=lambda item: item.encode("utf-8")
    ):
        contract = parse_capture_contract(tree[path], path=path)
        digest = capture_contract_digest(contract).tagged
        result[digest] = AcceptedCaptureContract(
            path=path,
            contract=contract,
            artifact_digest=digest,
        )
    return result


class ClaimAttestationRefusal(FormatError):
    """One typed, stable refusal from the attestation door."""

    def __init__(self, suffix: str, message: str) -> None:
        self.error_code = f"cruxible.claim_attestation.{suffix}"
        super().__init__(f"{self.error_code}: {message}")


def _refuse(suffix: str, message: str) -> NoReturn:
    raise ClaimAttestationRefusal(suffix, message)


@dataclass(frozen=True)
class _TreeLedgerResolver:
    tree: Mapping[str, bytes]
    coordinate: AcceptedCoordinate

    def read_ledger_source(self, source: LedgerSourceReference) -> bytes:
        if source.coordinate != self.coordinate:
            _refuse("capture_binding_invalid", "Capture names another accepted coordinate")
        content = self.tree.get(source.address.artifact_path)
        if content is None:
            _refuse("capture_unavailable", "Capture ledger source is absent")
        return content


def _accepted_claim(tree: Mapping[str, bytes], claim_id: str) -> ClaimArtifactAny:
    path = claim_path(claim_id)
    content = tree.get(path)
    if content is None:
        _refuse("claim_not_found_at_referent", "signed Claim is absent at the referent")
    try:
        return parse_claim(content, path=path)
    except (CruxibleError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "statement_binding_mismatch", "accepted Claim does not parse canonically"
        ) from exc


def _subject_shell_digest(tree: Mapping[str, bytes], claim: ClaimArtifactAny) -> str:
    path = claim.statement.subject.artifact_path
    content = tree.get(path)
    if content is None:
        _refuse("statement_binding_mismatch", "Claim subject shell is absent")
    try:
        return subject_digest(parse_subject(content, path=path)).tagged
    except (CruxibleError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "statement_binding_mismatch", "Claim subject shell does not reproduce"
        ) from exc


def _object_shell_digest(tree: Mapping[str, bytes], claim: ClaimArtifactAny) -> str | None:
    if not isinstance(claim.statement.object, SubjectClaimObject):
        return None
    path = claim.statement.object.address.artifact_path
    content = tree.get(path)
    if content is None:
        _refuse("statement_binding_mismatch", "Claim object shell is absent")
    try:
        return subject_digest(parse_subject(content, path=path)).tagged
    except (CruxibleError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "statement_binding_mismatch", "Claim object shell does not reproduce"
        ) from exc


def _examined_capture_semantics(claim: ClaimArtifactAny, capture_digest: str) -> None:
    if capture_digest not in claim.backing.capture_digests:
        _refuse("examined_capture_not_backing", "Capture is not backing of the signed Claim")
    if not isinstance(claim.backing, ClaimBacking):
        return
    associations = tuple(
        item for item in claim.backing.citations if item.capture_digest == capture_digest
    )
    # No explicit association means immutable legacy-evidence semantics.
    if associations and not any(item.role == "evidence" for item in associations):
        _refuse("examined_capture_not_evidence", "copy-only backing is not attestable evidence")


class _ReferentProducers(Mapping[str, str]):
    """The referent's Provider and Procedure digests, each read at its identity's own path.

    An accepted artifact with a qualified identity can only be at that identity's
    path (its parse checks the correspondence), so looking one up reads that one
    file and answers what the whole-prefix tables did. Enumerating (which no
    verification does) falls back to those tables.
    """

    def __init__(self, tree: Mapping[str, bytes]) -> None:
        self._tree = tree
        self._found: dict[str, str | None] = {}

    def _lookup(self, qualified: object) -> str | None:
        if not isinstance(qualified, str):
            return None
        if qualified in self._found:
            return self._found[qualified]
        kind, separator, name = qualified.partition(":")
        digest: str | None = None
        try:
            if separator and kind == "Provider":
                path = provider_path(name)
                content = self._tree.get(path)
                if content is not None:
                    provider = parse_provider(content, path=path)
                    if provider.identity.qualified == qualified:
                        digest = provider_digest(provider).tagged
            elif separator and kind == "Procedure":
                path = procedure_path(name)
                content = self._tree.get(path)
                if content is not None:
                    procedure = parse_procedure(content, path=path)
                    if procedure.identity.qualified == qualified:
                        digest = procedure_artifact_digest(procedure).tagged
        except (CruxibleError, ValueError):
            # A name that is not path-addressable names no accepted artifact.
            digest = None
        self._found[qualified] = digest
        return digest

    def __getitem__(self, qualified: str) -> str:
        digest = self._lookup(qualified)
        if digest is None:
            raise KeyError(qualified)
        return digest

    def __contains__(self, qualified: object) -> bool:
        return self._lookup(qualified) is not None

    def _whole(self) -> dict[str, str]:
        return _provider_digests(self._tree) | _procedure_digests(self._tree)

    def __iter__(self) -> Iterator[str]:
        return iter(self._whole())

    def __len__(self) -> int:
        return len(self._whole())


def _contract_live_at(tree: Mapping[str, bytes], identity: ArtifactIdentity) -> bool:
    """Whether the CaptureContract of ``identity`` is live in ``tree``, read at its own path."""
    try:
        path = capture_contract_path(identity.name)
    except (CruxibleError, ValueError):
        return False
    content = tree.get(path)
    if content is None:
        return False
    contract = parse_capture_contract(content, path=path)
    return contract.identity == identity and contract.lifecycle.state == "live"


def _current_contract(
    tree: Mapping[str, bytes],
    digest: str,
    historical: Callable[[str], AcceptedCaptureContract | None] | None,
) -> AcceptedCaptureContract | None:
    """The CaptureContract with exactly ``digest`` present in ``tree``, or None.

    The accepted-history index locates the version (and so its one path: the
    digest covers the identity); the referent's own bytes at that path decide
    whether it is the version present there. Without an index answer the
    referent's CaptureContracts are read whole, as before.
    """
    version = None if historical is None else historical(digest)
    if version is None:
        return _capture_contracts(tree).get(digest)
    content = tree.get(version.path)
    if content is None:
        return None
    contract = parse_capture_contract(content, path=version.path)
    if capture_contract_digest(contract).tagged != digest:
        return None
    return AcceptedCaptureContract(path=version.path, contract=contract, artifact_digest=digest)


def _provider_digests(tree: Mapping[str, bytes]) -> dict[str, str]:
    result: dict[str, str] = {}
    # By path first: ``items()`` would read every blob of the tree to keep a few.
    for path in paths_with_prefix(tree, "providers/"):
        provider = parse_provider(tree[path], path=path)
        result[provider.identity.qualified] = provider_digest(provider).tagged
    return result


def _procedure_digests(tree: Mapping[str, bytes]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in paths_with_prefix(tree, "procedures/"):
        procedure = parse_procedure(tree[path], path=path)
        result[procedure.identity.qualified] = procedure_artifact_digest(procedure).tagged
    return result


def _live_at_append(
    identity: ArtifactIdentity,
    *,
    append_tree: Mapping[str, bytes],
) -> bool:
    if identity.kind == "Provider":
        content = append_tree.get(provider_path(identity.name))
        if content is None:
            return False
        provider = parse_provider(content, path=provider_path(identity.name))
        return provider.lifecycle.state == "live"
    if identity.kind == "CaptureContract":
        return _contract_live_at(append_tree, identity)
    if identity.kind == "Procedure":
        content = append_tree.get(procedure_path(identity.name))
        if content is None:
            return False
        procedure = parse_procedure(content, path=procedure_path(identity.name))
        return procedure.lifecycle.state == "live"
    return False


def _new_capture_accounts(
    *,
    bodies: CaptureObjectStoreProtocol,
    principals: ActivePrincipalReader,
    law: ClaimLawEvidenceAny,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None,
    statement: ClaimAttestationStatement,
    claim: ClaimArtifactAny,
    referent_tree: Mapping[str, bytes],
    append_tree: Mapping[str, bytes],
    historical_capture_contract: Callable[[str], AcceptedCaptureContract | None] | None = None,
    require_production_proof: bool = True,
) -> tuple[tuple[str, ...], tuple[ClaimAttestationResolvedArtifact, ...]]:
    # The cited Captures' contracts and producers are resolved by their exact
    # digest or identity, never by reading every contract or producer.
    producers = _ReferentProducers(referent_tree)
    admitted: list[str] = []
    claim_type_content = referent_tree.get(claim_type_path(claim.statement.predicate))
    if claim_type_content is None:
        _refuse("capture_admission_refused", "ClaimType is absent at the signed referent")
    try:
        claim_type = parse_claim_type(
            claim_type_content,
            path=claim_type_path(claim.statement.predicate),
        )
    except (CruxibleError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "capture_admission_refused",
            "Claim evidence admission inputs do not reproduce at the signed referent",
        ) from exc
    resolved: dict[tuple[str, str], ClaimAttestationResolvedArtifact] = {}
    for digest in statement.cited_capture_digests:
        try:
            raw = bodies.read(
                digest,
                access=BodyAccessContext(
                    principal_id="playbill-claim-attestation",
                    can_read_body=True,
                ),
            )
        except CruxibleError as exc:
            raise ClaimAttestationRefusal(
                "capture_unavailable", "Capture is absent from CAS"
            ) from exc
        try:
            envelope = parse_capture_envelope(raw)
        except (CruxibleError, ValueError) as exc:
            raise ClaimAttestationRefusal("capture_invalid", "Capture envelope is invalid") from exc
        accepted = _current_contract(
            referent_tree, envelope.capture_contract_digest, historical_capture_contract
        )
        if accepted is None and historical_capture_contract is not None:
            # A Capture made under a version since succeeded is still exact
            # evidence of that version, provided its contract is live at referent.
            accepted = historical_capture_contract(envelope.capture_contract_digest)
            if accepted is not None and not _contract_live_at(
                referent_tree, accepted.contract.identity
            ):
                _refuse(
                    "capture_contract_not_live_at_referent",
                    "CaptureContract is not live at referent",
                )
        if accepted is None:
            _refuse("capture_contract_unresolved", "CaptureContract is not accepted at referent")
        contract: CaptureContract = accepted.contract
        if contract.lifecycle.state != "live":
            _refuse(
                "capture_contract_not_live_at_referent",
                "CaptureContract is not live at referent",
            )
        executable = envelope.run_coordinate.executable_identity
        if executable == contract.identity:
            if envelope.run_coordinate.executable_digest != accepted.artifact_digest:
                _refuse(
                    "capture_executable_unresolved",
                    "Capture executable does not resolve to its exact CaptureContract",
                )
        elif executable.kind in {"Provider", "Procedure"}:
            executable_digest = producers.get(executable.qualified)
            if executable_digest != envelope.run_coordinate.executable_digest:
                _refuse(
                    "capture_executable_unresolved",
                    "Capture executable producer is not accepted at its exact digest",
                )
        else:
            _refuse(
                "capture_executable_unresolved",
                "Capture executable is not an accepted CaptureContract or producer",
            )

        provenance_grade = (
            "self-asserted" if capture_contract_is_self_asserted(contract) else "daemon-fetched"
        )
        if envelope.producer.kind == "Provider":
            if envelope.producer.qualified not in producers:
                _refuse("capture_provider_unresolved", "Capture Provider is not accepted")
        elif envelope.producer.kind == "Principal":
            try:
                principals.require_active(envelope.producer.name)
            except CruxibleError as exc:
                raise ClaimAttestationRefusal(
                    "capture_provider_unresolved",
                    "Capture producer Principal is not active at the signed referent",
                ) from exc
            if provenance_grade != "self-asserted":
                _refuse(
                    "capture_provider_unresolved",
                    "daemon-fetched Capture provenance requires an accepted Provider",
                )
        elif envelope.producer.kind == "Procedure":
            if envelope.producer.qualified not in producers:
                _refuse("capture_provider_unresolved", "Capture Procedure is not accepted")
        else:
            _refuse(
                "capture_provider_unresolved",
                "Capture producer is not an accepted Provider, Procedure, or active Principal",
            )
        try:
            verify_capture(
                digest,
                store=bodies,
                contract=contract,
                ledger_resolver=_TreeLedgerResolver(
                    referent_tree,
                    statement.referent_coordinate,
                ),
                producer_artifact_digests=producers,
                producer_receipt_resolver=producer_receipt_resolver,
                require_production_proof=require_production_proof,
            )
        except ClaimAttestationRefusal:
            raise
        except CaptureProductionUnproven as exc:
            raise ClaimAttestationRefusal(
                "capture_production_unproven",
                "Capture has no daemon proof that a run produced it",
            ) from exc
        except (CruxibleError, ValueError) as exc:
            raise ClaimAttestationRefusal(
                "capture_binding_invalid",
                "Capture does not reproduce against its accepted contract and producer",
            ) from exc
        try:
            decisions = evaluate_capture_evidence_admissions(
                claim,
                claim_type=claim_type,
                capture_digest=digest,
                capture_contract=accepted,
                envelope=envelope,
                verified_attestations=law.verified_attestations,
            )
        except (CruxibleError, ValueError) as exc:
            raise ClaimAttestationRefusal(
                "capture_admission_refused",
                "Capture evidence admission could not be evaluated",
            ) from exc
        if not decisions:
            _refuse(
                "capture_admission_refused",
                "CaptureContract declares no evaluable evidence kind",
            )
        if any(item.trace.result.verdict == "eligible" for item in decisions):
            admitted.append(digest)
        contract_resolved = ClaimAttestationResolvedArtifact(
            identity=contract.identity,
            artifact_digest=accepted.artifact_digest,
            live_at_append=_live_at_append(contract.identity, append_tree=append_tree),
        )
        resolved[(contract.identity.qualified, accepted.artifact_digest)] = contract_resolved
        for identity in {envelope.producer, envelope.run_coordinate.executable_identity}:
            if identity.kind not in {"Provider", "Procedure"}:
                continue
            artifact_digest = producers.get(identity.qualified)
            if artifact_digest is None:
                _refuse("capture_provider_unresolved", "Capture Provider is not accepted")
            resolved[(identity.qualified, artifact_digest)] = ClaimAttestationResolvedArtifact(
                identity=identity,
                artifact_digest=artifact_digest,
                live_at_append=_live_at_append(identity, append_tree=append_tree),
            )
    return (
        tuple(admitted),
        tuple(
            resolved[key]
            for key in sorted(
                resolved,
                key=lambda item: (item[0].encode("utf-8"), item[1].encode("ascii")),
            )
        ),
    )


def verify_attestation_referent(
    attestation: ClaimAttestation,
    *,
    instance_id: str,
    referent_tree: Mapping[str, bytes],
    referent_principals: ActivePrincipalReader,
    at: datetime,
) -> ClaimArtifactAny:
    """Verify immutable input for both pending append and governed acceptance.

    The caller proves that the referent coordinate is accepted and supplies its
    exact source snapshot. No projection or pending verification account grants
    authority to a signature during replay.
    """
    s = attestation.statement
    if s.instance_id != instance_id:
        _refuse("statement_binding_mismatch", "attestation belongs to another instance")
    claim = _accepted_claim(referent_tree, s.claim_identity.name)
    if (
        claim.identity != s.claim_identity
        or claim_artifact_digest(claim).tagged != s.claim_artifact_digest
    ):
        _refuse("claim_artifact_digest_mismatch", "signed Claim artifact differs")
    if claim_statement_digest(claim.statement).tagged != s.claim_statement_digest:
        _refuse("statement_binding_mismatch", "signed Claim statement differs")
    if (
        _subject_shell_digest(referent_tree, claim) != s.subject_shell_digest
        or _object_shell_digest(referent_tree, claim) != s.object_shell_digest
    ):
        _refuse("statement_binding_mismatch", "signed Claim shell digest differs")
    _verify_principal(attestation, referent_principals, phase="referent")
    if s.attested_at > at:
        _refuse("request_invalid", "attested_at is in the future")
    return claim


def _verify_principal(
    attestation: ClaimAttestation,
    registry: ActivePrincipalReader,
    *,
    phase: Literal["referent", "append"],
) -> None:
    s = attestation.statement
    try:
        principal = registry.require_active(s.attesting_principal_id)
    except CruxibleError as exc:
        raise ClaimAttestationRefusal(
            f"principal_inactive_at_{phase}", f"principal inactive at {phase}"
        ) from exc
    if principal.kind != "ordinary":
        _refuse("principal_not_ordinary", "only ordinary principals may attest")
    if principal.public_key_digest != s.signing_key_digest:
        _refuse(f"signing_key_invalid_at_{phase}", f"signing key invalid at {phase}")
    if phase == "referent":
        # Admission checks that the same key remains eligible; its signature
        # was already verified against that key at the signed referent.
        try:
            verify_claim_attestation_v2_principal(attestation, principal=principal)
        except CruxibleError as exc:
            raise ClaimAttestationRefusal("signature_invalid", "signature does not verify") from exc


def verify_attestation_admission(
    attestation: ClaimAttestation,
    *,
    claim: ClaimArtifactAny,
    referent_tree: Mapping[str, bytes],
    referent_principals: ActivePrincipalReader,
    current_tree: Mapping[str, bytes],
    current_principals: ActivePrincipalReader,
    bodies: CaptureObjectStoreProtocol,
    law: ClaimLawEvidenceAny | None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None,
    historical_capture_contract: Callable[[str], AcceptedCaptureContract | None] | None = None,
    require_production_proof: bool = True,
) -> tuple[tuple[str, ...], tuple[ClaimAttestationResolvedArtifact, ...]]:
    """Check current eligibility after authenticating the exact signed referent.

    `historical_capture_contract` resolves an exact contract version accepted at
    or before the signed referent, for Captures made under a since-succeeded
    version of a still-live contract. `require_production_proof` holds each
    newly cited Capture to daemon proof that a run produced it; a replay of an
    accepted attestation passes False.
    """
    s = attestation.statement
    _verify_principal(attestation, current_principals, phase="append")
    current = _accepted_claim(current_tree, s.claim_identity.name)
    if isinstance(current, ClaimArtifact):
        _refuse("claim_terminally_retired", "Claim lineage is terminally retired")
    if s.attestation_basis == "examined_existing":
        for digest in s.cited_capture_digests:
            _examined_capture_semantics(claim, digest)
        return s.cited_capture_digests, ()
    if law is None:
        _refuse("capture_admission_refused", "Claim admission evidence is absent at referent")
    return _new_capture_accounts(
        bodies=bodies,
        principals=referent_principals,
        law=law,
        producer_receipt_resolver=producer_receipt_resolver,
        statement=s,
        claim=claim,
        referent_tree=referent_tree,
        append_tree=current_tree,
        historical_capture_contract=historical_capture_contract,
        require_production_proof=require_production_proof,
    )


def verify_attestation_binding(
    attestation: ClaimAttestation,
    *,
    instance_id: str,
    referent_tree: Mapping[str, bytes],
    referent_principals: ActivePrincipalReader,
    current_tree: Mapping[str, bytes],
    current_principals: ActivePrincipalReader,
    at: datetime,
    bodies: CaptureObjectStoreProtocol,
    law: ClaimLawEvidenceAny | None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None,
    historical_capture_contract: Callable[[str], AcceptedCaptureContract | None] | None = None,
    require_production_proof: bool = True,
) -> tuple[tuple[str, ...], tuple[ClaimAttestationResolvedArtifact, ...]]:
    claim = verify_attestation_referent(
        attestation,
        instance_id=instance_id,
        referent_tree=referent_tree,
        referent_principals=referent_principals,
        at=at,
    )
    return verify_attestation_admission(
        attestation,
        claim=claim,
        referent_tree=referent_tree,
        referent_principals=referent_principals,
        current_tree=current_tree,
        current_principals=current_principals,
        bodies=bodies,
        law=law,
        producer_receipt_resolver=producer_receipt_resolver,
        historical_capture_contract=historical_capture_contract,
        require_production_proof=require_production_proof,
    )
