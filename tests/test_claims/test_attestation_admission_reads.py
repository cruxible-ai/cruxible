"""Served attestation admission uses accepted indexes; replay remains independent."""

from datetime import timedelta
from pathlib import Path

from cruxible_client.contracts.accepted_attestations import (
    attestation_path,
    render_accepted_attestation,
)
from cruxible_client.contracts.authoring.models import (
    AttestationAuthoringPayload,
    ChangeSetAuthoringPayload,
    authoring_member_identity,
)
from cruxible_client.contracts.claim_attestations import (
    accepted_referent_coordinates_from_tree,
    claim_attestation_v2_envelope_digest,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.test_claims.test_claim_attestation_service import RECORDED_AT, _request
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_indexes.test_resolution_contracts import _accept_tree


def test_historical_attestation_batch_reuses_exact_principal_and_indexed_referents(
    tmp_path: Path, monkeypatch
) -> None:
    import cruxible_core.evidence.attestation_verification as verification
    import cruxible_core.proposals.proposals as proposals

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    original = instance.accepted_coordinate()
    records = tuple(
        _request(
            instance,
            owner,
            claim_id,
            tmp_path,
            stance=stance,
            attested_at=RECORDED_AT + timedelta(seconds=i),
        ).attestation
        for i, stance in enumerate(("support", "contradict", "unsure"))
    )
    tree = instance.tree_at(original.git_oid)
    tree[attestation_path(claim_attestation_v2_envelope_digest(records[0]))] = (
        render_accepted_attestation(records[0])
    )
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-28T15:01:00.000000Z",
        proposal_name="advance",
    )
    current = instance.accepted_coordinate()
    # Same referents as retained law evidence, with an explicit historical cutoff.
    expected = accepted_referent_coordinates_from_tree(
        instance.tree_at(current.git_oid), current=AcceptedCoordinate.from_internal(current)
    )
    assert instance.accepted_referent_coordinates(current) == expected
    assert AcceptedCoordinate.from_internal(current) not in (
        instance.accepted_referent_coordinates(original)
    )

    lookups = []
    verified = []
    original_lookup = instance.accepted_principal
    original_verify = verification.verify_attestation_binding

    def principal(at, name):
        if at == AcceptedCoordinate.from_internal(original):
            lookups.append((at, name))
        return original_lookup(at, name)

    def verify(*args, **kwargs):
        verified.append(args[0])
        return original_verify(*args, **kwargs)

    def no_scan(*args, **kwargs):
        raise AssertionError("served admission must not parse the full historical registry/ledger")

    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(
                (AttestationAuthoringPayload(attestation=item) for item in records[1:]),
                key=authoring_member_identity,
            )
        )
    )
    actor = AuthenticatedActor(actor_id="owner")
    with monkeypatch.context() as patch:
        patch.setattr(proposals, "accepted_referent_coordinates_from_tree", no_scan)
        patch.setattr(proposals, "principal_registry_from_tree", no_scan)
        patch.setattr(instance, "accepted_principal", principal)
        patch.setattr(verification, "verify_attestation_binding", verify)
        coordinator = AuthoringIntentCoordinator.for_instance(instance)
        intent = coordinator.create(
            actor=actor, payload=payload, canonical_timestamp="2026-08-28T15:02:00.000000Z"
        ).intent
        result = coordinator.submit(intent.intent_id, actor=actor)
        assert result.status.state == "ready_to_activate", result.model_dump(mode="json")
    assert verified
    assert len(lookups) * 2 == len(verified)
    assert set(lookups) == {(AcceptedCoordinate.from_internal(original), "owner")}


def test_referent_producer_and_contract_tables_read_only_their_own_paths(
    tmp_path: Path,
) -> None:
    """The referent's CaptureContracts, Providers and Procedures are read by prefix (perf B6).

    The answers are the whole-tree ones; only the matching paths' bytes are read.
    """

    from collections.abc import Iterator, Mapping

    from cruxible_client.contracts.captures import capture_contract_digest, parse_capture_contract
    from cruxible_client.contracts.procedures.artifacts import (
        parse_procedure,
        procedure_artifact_digest,
    )
    from cruxible_client.contracts.providers import parse_provider, provider_digest
    from cruxible_core.evidence import attestation_verification as verification

    instance, _claim_id, _owner = _accepted_claim_world(tmp_path)
    source = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert any(path.startswith("capture-contracts/") for path in source)

    class Counted(Mapping[str, bytes]):
        def __init__(self) -> None:
            self.read: list[str] = []

        def __getitem__(self, path: str) -> bytes:
            self.read.append(path)
            return source[path]

        def __iter__(self) -> Iterator[str]:
            return iter(source)

        def __len__(self) -> int:
            return len(source)

    prefixes = ("capture-contracts/", "providers/", "procedures/")
    tree = Counted()
    contracts = verification._capture_contracts(tree)
    providers = verification._provider_digests(tree)
    procedures = verification._procedure_digests(tree)
    assert tree.read and all(path.startswith(prefixes) for path in tree.read)
    assert len(tree.read) == sum(1 for path in source if path.startswith(prefixes))
    assert {key: value.path for key, value in contracts.items()} == {
        capture_contract_digest(parse_capture_contract(content, path=path)).tagged: path
        for path, content in source.items()
        if path.startswith("capture-contracts/")
    }
    assert providers == {
        (parsed := parse_provider(content, path=path)).identity.qualified: provider_digest(
            parsed
        ).tagged
        for path, content in source.items()
        if path.startswith("providers/")
    }
    assert procedures == {
        (parsed := parse_procedure(content, path=path)).identity.qualified: (
            procedure_artifact_digest(parsed).tagged
        )
        for path, content in source.items()
        if path.startswith("procedures/")
    }
    # The identity-path lookups answer what the whole tables answer (review F-007).
    lookup = verification._ReferentProducers(source)
    for qualified, digest in (providers | procedures).items():
        assert qualified in lookup and lookup[qualified] == digest
    assert "Provider:absent-provider" not in lookup and lookup.get("Procedure:absent") is None
    for accepted in contracts.values():
        assert verification._contract_live_at(source, accepted.contract.identity) == (
            accepted.contract.lifecycle.state == "live"
        )


def test_new_capture_admission_reads_only_the_cited_contract_and_producers(
    tmp_path: Path,
) -> None:
    """Review F-007: a fixed cited Capture reads the same files however many others exist.

    Unrelated CaptureContracts, Providers and Procedures are planted as bytes no
    parser accepts: any read of them would refuse. The admission answer equals
    the one over the clean referent, and the reads do not grow.
    """

    from collections.abc import Iterator, Mapping

    from cruxible_client.contracts.claims import claim_path
    from cruxible_core.evidence.attestation_verification import (
        verify_attestation_admission,
        verify_attestation_referent,
    )
    from cruxible_core.service.claims.claims import _claim_law_evidence
    from tests.test_claims.test_claim_attestation_service import _coordinator_new_capture

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    capture = _coordinator_new_capture(instance, claim_id)
    request = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        captures=(capture.capture_digest,),
    )
    referent = instance.accepted_coordinate()
    clean = instance.tree_at(referent.git_oid)
    principals = instance.accepted_principal_registry(referent)
    claim = verify_attestation_referent(
        request.attestation,
        instance_id=instance.descriptor.instance_id,
        referent_tree=clean,
        referent_principals=principals,
        at=RECORDED_AT,
    )

    class Counted(Mapping[str, bytes]):
        def __init__(self, rows: dict[str, bytes]) -> None:
            self.rows, self.read = rows, 0

        def __getitem__(self, path: str) -> bytes:
            self.read += 1
            return self.rows[path]

        def __iter__(self) -> Iterator[str]:
            return iter(self.rows)

        def __len__(self) -> int:
            return len(self.rows)

    def admit(tree: Mapping[str, bytes]):  # type: ignore[no-untyped-def]
        return verify_attestation_admission(
            request.attestation,
            claim=claim,
            referent_tree=tree,
            referent_principals=principals,
            current_tree=tree,
            current_principals=principals,
            bodies=instance.body_store(),
            law=_claim_law_evidence(instance, path=claim_path(claim_id), at=referent),
            producer_receipt_resolver=instance.capture_verification_context(),
            historical_capture_contract=lambda digest: instance.accepted_capture_contract_version(
                AcceptedCoordinate.from_internal(referent), digest
            ),
        )

    expected = admit(clean)
    reads = []
    for unrelated in (3, 30):
        rows = dict(clean)
        for number in range(unrelated):
            for prefix in ("capture-contracts", "providers", "procedures"):
                rows[f"{prefix}/zz-unrelated-{number:03d}.json"] = b"not an artifact"
        tree = Counted(rows)
        assert admit(tree) == expected
        reads.append(tree.read)
    assert reads[0] == reads[1], reads


def _with_unrelated_contracts(source, size: int):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.captures import (
        capture_contract_path,
        parse_capture_contract,
        render_capture_contract,
    )

    path = next(p for p in source if p.startswith("capture-contracts/"))
    contract = parse_capture_contract(source[path], path=path)
    rows = dict(source)
    for number in range(size):
        name = f"zz-unrelated-{number:03d}"
        copied = contract.model_copy(
            update={"identity": contract.identity.model_copy(update={"name": name})}
        )
        rows[capture_contract_path(name)] = render_capture_contract(copied)
    return rows


def test_an_authoritative_index_miss_never_reads_the_contract_population(tmp_path) -> None:
    """Review F-009: an indexed absence answers absence without enumerating contracts.

    Only a lookup without index authority (a plain callable, no provider) still
    reads the referent's contracts whole; both refuse alike at admission.
    """

    from collections.abc import Iterator, Mapping

    from cruxible_core.evidence import attestation_verification as verification
    from cruxible_core.evidence.attestation_verification import CaptureContractVersions

    instance, _claim_id, _owner = _accepted_claim_world(tmp_path)
    source = instance.tree_at(instance.accepted_coordinate().git_oid)
    referent = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    served = instance.accepted_capture_contract_versions(referent)
    assert served.authoritative

    class Counted(Mapping[str, bytes]):
        def __init__(self, rows: dict[str, bytes]) -> None:
            self.rows, self.reads = rows, 0

        def __getitem__(self, path: str) -> bytes:
            self.reads += 1
            return self.rows[path]

        def __iter__(self) -> Iterator[str]:
            return iter(self.rows)

        def __len__(self) -> int:
            return len(self.rows)

    absent = "sha256:" + "f" * 64
    work = []
    for size in (3, 30):
        tree = Counted(_with_unrelated_contracts(source, size))
        for locator in (CaptureContractVersions(lambda _d: None, authoritative=True), served):
            assert verification._current_contract(tree, absent, locator) is None
        work.append(tree.reads)
        fallback = Counted(_with_unrelated_contracts(source, size))
        assert verification._current_contract(fallback, absent, lambda _d: None) is None
        assert fallback.reads >= size
    assert work == [0, 0], work


def test_a_located_contract_is_current_only_with_its_exact_bytes_at_the_referent(
    tmp_path,
) -> None:
    """The index locates a version; the referent's own bytes decide whether it is present."""

    from cruxible_client.contracts.captures import (
        capture_contract_digest,
        render_capture_contract,
    )
    from cruxible_core.evidence import attestation_verification as verification

    instance, _claim_id, _owner = _accepted_claim_world(tmp_path)
    source = instance.tree_at(instance.accepted_coordinate().git_oid)
    accepted = next(iter(verification._capture_contracts(source).values()))
    digest = accepted.artifact_digest
    assert verification._current_contract(source, digest, lambda _d: accepted) == accepted
    assert verification._current_contract({}, digest, lambda _d: accepted) is None
    assert verification._current_contract(source, "sha256:" + "f" * 64, lambda _d: accepted) is None
    other = "e" if accepted.contract.replay_policy_digest != "sha256:" + "e" * 64 else "d"
    changed = accepted.contract.model_copy(update={"replay_policy_digest": "sha256:" + other * 64})
    rows = dict(source)
    rows[accepted.path] = render_capture_contract(changed)
    assert capture_contract_digest(changed).tagged != digest
    assert verification._current_contract(rows, digest, lambda _d: accepted) is None
