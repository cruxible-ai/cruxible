from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    build_coordinator_self_source_capture,
    build_working_selection_capture,
    capture_contract_digest,
    capture_contract_path,
    parse_capture_envelope,
    render_capture_contract,
    render_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationCaptureReferenceV1,
    ClaimAttestationStatementV2,
    ClaimAttestationV2,
    claim_attestation_v2_statement_bytes,
)
from cruxible_client.contracts.claims import (
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.signing import LocalEd25519ClaimAttestationSigner
from cruxible_core.service.playbill_claim_attestations import (
    ClaimAttestationRefusal,
    _examined_capture_semantics,
    service_append_claim_attestation,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextRequestV2,
    service_playbill_next,
)
from tests.test_playbill.test_authoring_existing_capture import _activate, shared_capture_world
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world

RECORDED_AT = datetime(2026, 8, 28, 15, tzinfo=UTC)


def _request(
    instance,
    owner,
    claim_id: str,
    root: Path,
    *,
    basis: str = "examined_existing",
    stance: str = "support",
    captures: tuple[str, ...] | None = None,
    attested_at: datetime = RECORDED_AT,
    valid_until: datetime | None = None,
):  # type: ignore[no-untyped-def]
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    tree = instance.tree_at(coordinate.git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    subject_content = tree[claim.statement.subject.artifact_path]
    object_shell_digest = None
    if isinstance(claim.statement.object, SubjectClaimObject):
        object_path = claim.statement.object.address.artifact_path
        object_shell_digest = subject_digest(
            parse_subject(tree[object_path], path=object_path)
        ).tagged
    evidence_captures = tuple(
        sorted(
            {
                citation.capture_digest
                for citation in claim.backing.citations
                if citation.role == "evidence"
            },
            key=lambda item: item.encode("ascii"),
        )
    )
    cited = evidence_captures if captures is None else captures
    statement = ClaimAttestationStatementV2(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        claim_identity=claim.identity,
        claim_artifact_digest=claim_artifact_digest(claim).tagged,
        claim_statement_digest=claim_statement_digest(claim.statement).tagged,
        subject_shell_digest=subject_digest(
            parse_subject(subject_content, path=claim.statement.subject.artifact_path)
        ).tagged,
        object_shell_digest=object_shell_digest,
        attesting_principal_id=owner.principal.principal_id,
        signing_key_digest=owner.principal.public_key_digest,
        attestation_basis=basis,
        stance=stance,
        cited_capture_digests=cited,
        attested_at=attested_at,
        valid_until=valid_until,
    )
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer=owner.principal.principal_id,
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(root / "workspace", instance.root),
    )
    return ClaimAttestationAppendRequestV1(
        attestation=signer.sign_claim_attestation_v2(statement),
        capture_references=(
            tuple(ClaimAttestationCaptureReferenceV1(capture_digest=digest) for digest in cited)
            if basis == "new_capture"
            else ()
        ),
    )


def _resign(
    request: ClaimAttestationAppendRequestV1,
    owner,
    **statement_updates: object,
) -> ClaimAttestationAppendRequestV1:  # type: ignore[no-untyped-def]
    statement = request.attestation.statement.model_copy(update=statement_updates)
    private_key = serialization.load_ssh_private_key(
        owner.private_key_path.read_bytes(),
        password=None,
    )
    assert isinstance(private_key, Ed25519PrivateKey)
    attestation = ClaimAttestationV2(
        statement=statement,
        signature=private_key.sign(claim_attestation_v2_statement_bytes(statement)).hex(),
    )
    return request.model_copy(update={"attestation": attestation})


def _assert_refusal(
    instance,
    request: ClaimAttestationAppendRequestV1,
    code: str,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ClaimAttestationRefusal) as error:
        service_append_claim_attestation(
            instance,
            request=request,
            actor_id="owner",
            recorded_at=RECORDED_AT,
        )
    assert error.value.error_code == f"playbill.claim_attestation.{code}"


def test_served_append_verifies_and_duplicate_is_an_identical_read(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)

    first = service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    retry = service_append_claim_attestation(
        instance,
        request=request.model_copy(update={"note": "ignored on duplicate"}),
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )

    assert retry == first
    assert len(instance.claim_attestation_evidence_store().events()) == 1
    assert first.recorded_head == first.current_head


def test_served_append_refuses_actor_relay_before_store_disclosure(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)

    with pytest.raises(ClaimAttestationRefusal) as error:
        service_append_claim_attestation(
            instance,
            request=request,
            actor_id="reviewer",
            recorded_at=RECORDED_AT,
        )

    assert error.value.error_code == "playbill.claim_attestation.actor_signer_mismatch"


def test_service_gate_independently_refuses_nonordinary_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)

    class Registry:
        def require_active(self, principal_id: str):  # type: ignore[no-untyped-def]
            assert principal_id == "owner"
            return owner.principal.model_copy(update={"kind": "recovery"})

    monkeypatch.setattr(
        "cruxible_core.service.playbill_claim_attestations.principal_registry_from_tree",
        lambda *_args, **_kwargs: Registry(),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_claim_attestations.verify_claim_attestation_v2_principal",
        lambda *_args, **_kwargs: None,
    )

    _assert_refusal(instance, request, "principal_not_ordinary")


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"instance_id": "inst_other"}, "statement_binding_mismatch"),
        ({"claim_artifact_digest": "sha256:" + "9" * 64}, "claim_artifact_digest_mismatch"),
        ({"subject_shell_digest": "sha256:" + "8" * 64}, "statement_binding_mismatch"),
    ],
)
def test_signed_claim_bindings_refuse_cross_instance_artifact_and_shell_tampering(
    tmp_path: Path,
    updates: dict[str, object],
    code: str,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _resign(_request(instance, owner, claim_id, tmp_path), owner, **updates)
    _assert_refusal(instance, request, code)


def test_unaccepted_referent_and_missing_claim_refuse_exact_codes(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)
    bad_coordinate = request.attestation.statement.referent_coordinate.model_copy(
        update={"git_oid": "9" * 40}
    )
    _assert_refusal(
        instance,
        _resign(request, owner, referent_coordinate=bad_coordinate),
        "referent_coordinate_unaccepted",
    )
    _assert_refusal(
        instance,
        _resign(
            request,
            owner,
            claim_identity=request.attestation.statement.claim_identity.model_copy(
                update={"name": "CLM-ffffffffffffffffffffffffffffffff"}
            ),
        ),
        "claim_not_found_at_referent",
    )


@pytest.mark.parametrize("checked_phase", ["referent", "append"])
def test_signing_key_digest_is_checked_at_each_coordinate_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checked_phase: str,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _resign(
        _request(instance, owner, claim_id, tmp_path),
        owner,
        signing_key_digest="sha256:" + "7" * 64,
    )
    from cruxible_core.service import playbill_claim_attestations as service_module

    original = service_module._principal_at

    def phase_selective(tree, *, coordinate, statement, phase):  # type: ignore[no-untyped-def]
        if phase != checked_phase:
            return owner.principal
        return original(
            tree,
            coordinate=coordinate,
            statement=statement,
            phase=phase,
        )

    monkeypatch.setattr(service_module, "_principal_at", phase_selective)
    if checked_phase == "append":
        monkeypatch.setattr(
            service_module,
            "verify_claim_attestation_v2_principal",
            lambda *_args, **_kwargs: None,
        )
    _assert_refusal(instance, request, f"signing_key_invalid_at_{checked_phase}")


def test_principal_inactive_codes_are_distinct_by_coordinate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)
    from cruxible_core.service import playbill_claim_attestations as service_module

    class InactiveRegistry:
        def require_active(self, _principal_id: str):  # type: ignore[no-untyped-def]
            raise PlaybillFormatError("inactive")

    monkeypatch.setattr(
        service_module,
        "principal_registry_from_tree",
        lambda *_args, **_kwargs: InactiveRegistry(),
    )
    _assert_refusal(instance, request, "principal_inactive_at_referent")

    original = service_module._principal_at

    def referent_allowed(tree, *, coordinate, statement, phase):  # type: ignore[no-untyped-def]
        if phase == "referent":
            return owner.principal
        return original(tree, coordinate=coordinate, statement=statement, phase=phase)

    monkeypatch.setattr(service_module, "_principal_at", referent_allowed)
    _assert_refusal(instance, request, "principal_inactive_at_append")


def test_examined_existing_semantics_refuse_nonbacking_and_copy_only(
    tmp_path: Path,
) -> None:
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    with pytest.raises(ClaimAttestationRefusal) as absent:
        _examined_capture_semantics(claim, "sha256:" + "6" * 64)
    assert absent.value.error_code.endswith("examined_capture_not_backing")

    citation = claim.backing.citations[0].model_copy(update={"role": "copy"})
    copy_only = claim.model_copy(
        update={"backing": claim.backing.model_copy(update={"citations": (citation,)})}
    )
    with pytest.raises(ClaimAttestationRefusal) as copy:
        _examined_capture_semantics(copy_only, citation.capture_digest)
    assert copy.value.error_code.endswith("examined_capture_not_evidence")


def _coordinator_new_capture(instance, claim_id: str):  # type: ignore[no-untyped-def]
    return build_coordinator_self_source_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        body=b"new exact Claim-bound observation\n",
        observed_at=RECORDED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )


def _store_envelope(instance, envelope):  # type: ignore[no-untyped-def]
    return instance.body_store().store(render_capture_envelope(envelope)).digest


def test_new_capture_account_commits_only_server_evaluated_admissions(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    capture = _coordinator_new_capture(instance, claim_id)
    service_append_claim_attestation(
        instance,
        request=_request(
            instance,
            owner,
            claim_id,
            tmp_path,
            basis="new_capture",
            captures=(capture.capture_digest,),
        ),
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    account = instance.claim_attestation_evidence_store().events()[0][1].verification_account
    assert account.admitted_capture_digests == ()
    assert account.statement.cited_capture_digests == (capture.capture_digest,)


def test_new_capture_refuses_unavailable_invalid_and_unresolved_contract(
    tmp_path: Path,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    missing = "sha256:" + "6" * 64
    _assert_refusal(
        instance,
        _request(
            instance,
            owner,
            claim_id,
            tmp_path,
            basis="new_capture",
            captures=(missing,),
        ),
        "capture_unavailable",
    )
    malformed = instance.body_store().store(b"not a Capture envelope").digest
    _assert_refusal(
        instance,
        _request(
            instance,
            owner,
            claim_id,
            tmp_path,
            basis="new_capture",
            captures=(malformed,),
        ),
        "capture_invalid",
    )

    direct = _coordinator_new_capture(instance, claim_id)
    envelope = parse_capture_envelope(
        instance.body_store().read(
            direct.capture_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    unresolved = _store_envelope(
        instance,
        envelope.model_copy(update={"capture_contract_digest": "sha256:" + "5" * 64}),
    )
    _assert_refusal(
        instance,
        _request(
            instance,
            owner,
            claim_id,
            tmp_path,
            basis="new_capture",
            captures=(unresolved,),
        ),
        "capture_contract_unresolved",
    )


def test_new_capture_refuses_provider_executable_binding_and_admission_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    direct = _coordinator_new_capture(instance, claim_id)
    envelope = parse_capture_envelope(
        instance.body_store().read(
            direct.capture_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    variants = (
        (
            envelope.model_copy(
                update={"producer": ArtifactIdentity(kind="Provider", name="missing-provider")}
            ),
            "capture_provider_unresolved",
        ),
        (
            envelope.model_copy(
                update={
                    "run_coordinate": envelope.run_coordinate.model_copy(
                        update={
                            "executable_identity": ArtifactIdentity(
                                kind="Provider", name="missing-executable"
                            )
                        }
                    )
                }
            ),
            "capture_executable_unresolved",
        ),
        (
            envelope.model_copy(
                update={
                    "commitment": envelope.commitment.model_copy(
                        update={"byte_length": envelope.commitment.byte_length + 1}
                    )
                }
            ),
            "capture_binding_invalid",
        ),
    )
    for variant, code in variants:
        digest = _store_envelope(instance, variant)
        _assert_refusal(
            instance,
            _request(
                instance,
                owner,
                claim_id,
                tmp_path,
                basis="new_capture",
                captures=(digest,),
            ),
            code,
        )

    monkeypatch.setattr(
        "cruxible_core.service.playbill_claim_attestations.evaluate_capture_evidence_admissions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unreproducible policy")),
    )
    _assert_refusal(
        instance,
        _request(
            instance,
            owner,
            claim_id,
            tmp_path,
            basis="new_capture",
            captures=(direct.capture_digest,),
        ),
        "capture_admission_refused",
    )


def test_new_capture_refuses_contract_not_live_at_referent(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    direct = _coordinator_new_capture(instance, claim_id)
    envelope = parse_capture_envelope(
        instance.body_store().read(
            direct.capture_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    old_digest = capture_contract_digest(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT).tagged
    retired = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=old_digest,
            )
        }
    )
    retired_digest = capture_contract_digest(retired).tagged
    retired_envelope = envelope.model_copy(
        update={
            "capture_contract_digest": retired_digest,
            "run_coordinate": envelope.run_coordinate.model_copy(
                update={"executable_digest": retired_digest}
            ),
        }
    )
    retired_capture = _store_envelope(instance, retired_envelope)
    request = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        captures=(retired_capture,),
    )
    signed_coordinate = request.attestation.statement.referent_coordinate
    referent = instance.resolve_accepted_coordinate(
        git_oid=signed_coordinate.git_oid,
        semantic_root=signed_coordinate.semantic_root,
        generation_root=signed_coordinate.generation_root,
        compiler_digest=signed_coordinate.compiler_digest,
    )
    referent_tree = instance.tree_at(referent.git_oid)
    referent_tree[capture_contract_path(retired.identity.name)] = render_capture_contract(retired)
    claim = parse_claim(referent_tree[claim_path(claim_id)], path=claim_path(claim_id))
    from cruxible_core.service.playbill_claim_attestations import _new_capture_accounts

    with pytest.raises(ClaimAttestationRefusal) as error:
        _new_capture_accounts(
            instance,
            statement=request.attestation.statement,
            claim=claim,
            referent=referent,
            referent_tree=referent_tree,
            append_tree=referent_tree,
        )
    assert error.value.error_code.endswith("capture_contract_not_live_at_referent")


def test_next_v2_reads_one_exact_evidence_head_while_v1_stays_legacy(
    tmp_path: Path,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    capture = build_coordinator_self_source_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        body=b"new supporting observation\n",
        observed_at=RECORDED_AT,
        accepted_coordinate=coordinate,
    )
    request = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        captures=(capture.capture_digest,),
    )
    appended = service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    access = CoverageAccessProfileV1(
        profile_id="attestation-door-test",
        permitted_access_classes=("instance", "public"),
    )

    legacy = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=RECORDED_AT,
            access_profile=access,
        ),
    )
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV2(
            evaluation_time=RECORDED_AT,
            access_profile=access,
            at_attestation_head_digest=appended.current_head,
        ),
    )

    assert result.tag == "playbill-next-result-v2"
    assert result.attestation_head_digest == appended.current_head
    assert result == service_playbill_next(
        instance,
        request=PlaybillNextRequestV2(
            evaluation_time=RECORDED_AT,
            access_profile=access,
            at_attestation_head_digest=appended.current_head,
        ),
    )
    assert not any(item.reason == "claim_new_evidence_supporting" for item in legacy.items)
    rows = tuple(item for item in result.items if item.reason == "claim_new_evidence_supporting")
    assert len(rows) == 1
    assert set(rows[0].detail) == {
        "claim_id",
        "claim_artifact_digest",
        "capture_digest",
        "attestation_event_digest",
        "attestation_basis",
        "stance",
        "attesting_principal",
        "current_at_append",
        "lineage_status",
    }
    assert rows[0].detail["lineage_status"] == "proven"
    assert rows[0].repair.command.endswith(
        f"--claim-id {claim_id} --capture-digest {capture.capture_digest}"
    )


def _assert_successor_resolves_attestation_membership(
    tmp_path: Path,
    *,
    stance: str,
    reason: str,
) -> None:
    (
        instance,
        owner,
        _actor,
        _first_claim_id,
        claim_id,
        _coordinator,
        shared_payload,
    ) = shared_capture_world(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    selected = b"new evidence for the accepted claim"
    source = instance.body_store().store(selected)
    capture = build_working_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        rationale="A new independent source observation awaits adjudication.",
        observed_at=RECORDED_AT,
        accepted_coordinate=coordinate,
        source_id="repo.work-items",
        coordinate={
            "source_byte_length": len(selected),
            "source_content_digest": source.digest,
        },
        selector={"anchor": "new evidence", "start_byte": 0, "end_byte": len(selected)},
        selected_content=selected,
    )
    request = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        stance=stance,
        captures=(capture.capture_digest,),
    )
    service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    access = CoverageAccessProfileV1(
        profile_id="attestation-door-resolution",
        permitted_access_classes=("instance", "public"),
    )

    def rows() -> tuple:  # type: ignore[no-untyped-def]
        return tuple(
            item
            for item in service_playbill_next(
                instance,
                request=PlaybillNextRequestV2(
                    evaluation_time=RECORDED_AT,
                    access_profile=access,
                ),
            ).items
            if item.reason == reason
        )

    def cite(role: str, timestamp: str) -> None:
        coordinator = AuthoringIntentCoordinator(
            instance=instance,
            store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        )
        payload = ClaimAuthoringPayloadV3(
            statement=shared_payload.statement,
            rationale="Adjudicate the newly observed Capture through the shipped citation path.",
            source=ExistingCaptureCitationSourceV1(capture_digest=capture.capture_digest),
            citation_role=role,  # type: ignore[arg-type]
            claim_ref=claim_id,
            dependency_drafts=ClaimDependencyDraftsV1(),
        )
        actor = AuthenticatedActor(actor_id="owner")
        intent = coordinator.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=timestamp,
        ).intent
        submitted = coordinator.submit(intent.intent_id, actor=actor)
        assert submitted.status.proposal_id is not None, (
            None
            if submitted.intent.last_preflight is None
            else submitted.intent.last_preflight.frontier.diagnostics
        )
        _activate(instance, submitted)
        instance.refresh()

    assert len(rows()) == 1
    cite("copy", "2026-08-28T15:01:00.000000Z")
    assert len(rows()) == 1
    cite("evidence", "2026-08-28T15:02:00.000000Z")
    assert rows() == ()


def test_copy_successor_keeps_membership_and_evidence_successor_resolves_it(
    tmp_path: Path,
) -> None:
    _assert_successor_resolves_attestation_membership(
        tmp_path,
        stance="contradict",
        reason="claim_contradicting_evidence_available",
    )
