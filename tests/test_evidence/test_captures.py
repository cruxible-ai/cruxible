from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CaptureProductionUnproven,
    CaptureRunCoordinateV1,
    InputReceiptSetManifest,
    build_cas_capture,
    build_coordinator_self_source_capture,
    build_derived_cas_capture,
    build_ledger_capture,
    capture_contract_digest,
    capture_contract_is_self_asserted,
    capture_is_coordinator_self_source,
    classify_capture_reuse,
    contract_materialization_modes,
    contract_source_kinds,
    evaluate_capture_contract_law,
    verify_capture,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import LedgerSourceReference
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._pc_c_support import (
    NOW,
    artifact_digest,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)


def _accepted_coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="11" * 32,
        semantic_root=digest("semantic", "one"),
        generation_root=digest("generation", "one"),
        compiler_digest=digest("compiler", "one"),
    )


class _LedgerResolver:
    def __init__(self, source: LedgerSourceReference, content: bytes) -> None:
        self.source = source
        self.content = content

    def read_ledger_source(self, source: LedgerSourceReference) -> bytes:
        if source != self.source:
            raise AssertionError("unexpected ledger source")
        return self.content


def test_cas_and_ledger_capture_commitments_verify_without_copying_ledger_bytes(
    tmp_path: Path,
) -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    store = body_store(tmp_path)
    cas_result = build_cas_capture(
        store=store,
        contract=contract,
        source_body=b'{"status":"released"}',
        run_coordinate=provider_run(provider_artifact),
        run_receipt_digest=digest("receipt", "cas"),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", "cas"),
        observed_at=NOW,
    )
    producers = {
        provider_artifact.identity.qualified: provider_run(provider_artifact).executable_digest
    }
    # A v1 envelope under a daemon-fetched contract carries no producer receipt:
    # it replays, but only an accepted citation of it may stand.
    with pytest.raises(CaptureProductionUnproven) as refused:
        verify_capture(
            cas_result.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests=producers,
        )
    assert refused.value.reason == "v1_envelope"
    assert (
        verify_capture(
            cas_result.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests=producers,
            require_production_proof=False,
        )
        == cas_result.envelope
    )

    ledger_body = b'{"policy":"approved"}\n'
    ledger_source = LedgerSourceReference(
        address=SemanticAddress.whole_artifact("documents/release-policy.json"),
        coordinate=_accepted_coordinate(),
    )
    ledger_result = build_ledger_capture(
        store=store,
        contract=contract,
        source=ledger_source,
        source_body=ledger_body,
        run_coordinate=provider_run(provider_artifact, run_id="ledger-run"),
        run_receipt_digest=digest("receipt", "ledger"),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", "ledger"),
        observed_at=NOW,
    )
    assert not store.verify(ledger_result.commitment_digest)
    assert (
        verify_capture(
            ledger_result.capture_digest,
            store=store,
            contract=contract,
            ledger_resolver=_LedgerResolver(ledger_source, ledger_body),
            producer_artifact_digests=producers,
            require_production_proof=False,
        )
        == ledger_result.envelope
    )


def test_derived_capture_requires_and_replays_exact_input_manifest(tmp_path: Path) -> None:
    contract = capture_contract(epistemic_grade="derived")
    provider_artifact = provider(contract)
    store = body_store(tmp_path)
    manifest = InputReceiptSetManifest(
        input_receipt_digests=(digest("receipt", "input"),),
        input_capture_digests=(digest("capture", "input"),),
        input_claim_artifact_digests=(artifact_digest("claim", "input"),),
    )
    result = build_derived_cas_capture(
        store=store,
        contract=contract,
        output_body=b'{"released":true}',
        manifest=manifest,
        reducer_digest=artifact_digest("reducer", "release-v1"),
        run_coordinate=provider_run(provider_artifact),
        run_receipt_digest=digest("receipt", "derived"),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", "derived"),
        observed_at=NOW,
    )
    assert result.envelope.input_receipt_set_manifest_digest is not None
    assert (
        verify_capture(
            result.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests={
                provider_artifact.identity.qualified: provider_run(
                    provider_artifact
                ).executable_digest
            },
            require_production_proof=False,
        )
        == result.envelope
    )
    assert (
        classify_capture_reuse(
            result.envelope,
            contract=contract,
            store=store,
            claim_id="CLM-0123456789abcdef0123456789abcdef",
        )
        == "not_shareable"
    )


def test_contract_run_can_produce_capture_without_claiming_provider_identity(
    tmp_path: Path,
) -> None:
    contract = capture_contract()
    result = build_cas_capture(
        store=body_store(tmp_path),
        contract=contract,
        source_body=b"bounded source",
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="watcher",
            run_id="watcher-run",
            bound_generation=digest("generation", "one"),
            executable_identity=contract.identity,
            executable_digest=capture_contract_digest(contract).tagged,
        ),
        run_receipt_digest=digest("receipt", "watcher"),
        producer=ArtifactIdentity(kind="Watcher", name="orders"),
        producer_binding_digest=digest("binding", "watcher"),
        observed_at=NOW,
    )
    assert result.envelope.producer.kind == "Watcher"


def test_coordinator_self_source_profile_is_cas_only_and_claim_bound(tmp_path: Path) -> None:
    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    store = body_store(tmp_path)
    claim_id = "CLM-0123456789abcdef0123456789abcdef"
    body = b"The work item is ready."
    result = build_coordinator_self_source_capture(
        store=store,
        actor_id="owner",
        claim_id=claim_id,
        body=body,
        observed_at=NOW,
        accepted_coordinate=_accepted_coordinate(),
    )

    assert contract.identity.qualified == ("CaptureContract:playbill.coordinator-self-source-v1")
    assert "logical_source_identities" not in contract.model_dump()
    assert contract_source_kinds(contract) == ("cas",)
    assert contract_materialization_modes(contract) == ("cas",)
    assert contract.retention_erasure_policy.erasure == "prohibited"
    assert capture_contract_is_self_asserted(contract)
    assert result.source_body_materialized is True
    assert store.verify(result.commitment_digest)
    assert store.verify(result.capture_digest)
    assert (
        store.read(
            result.commitment_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
        == body
    )
    assert verify_capture(result.capture_digest, store=store, contract=contract) == result.envelope
    assert capture_is_coordinator_self_source(
        result.envelope,
        contract=contract,
        claim_id=claim_id,
    )
    assert not capture_is_coordinator_self_source(
        result.envelope,
        contract=contract,
        claim_id="CLM-abcdefabcdefabcdefabcdefabcdefab",
    )
    assert (
        classify_capture_reuse(
            result.envelope,
            contract=contract,
            store=store,
            claim_id=claim_id,
        )
        == "claim_bound"
    )
    direct_mapping = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.source_subject_mapping_digest
    mixed_family = contract.model_copy(
        update={
            "components": contract.components.model_copy(
                update={
                    "source_subject_mapping_digest": direct_mapping,
                }
            )
        }
    )
    assert (
        classify_capture_reuse(
            result.envelope,
            contract=mixed_family,
            store=store,
            claim_id=claim_id,
        )
        == "claim_bound_mismatch"
    )
    assert (
        evaluate_capture_contract_law(
            contract,
            path="capture-contracts/playbill.coordinator-self-source-v1.json",
            predecessor=None,
        ).verdict
        == "accepted"
    )
    rewritten = contract.model_copy(update={"shape": "http_response"})
    refused = evaluate_capture_contract_law(
        rewritten,
        path="capture-contracts/playbill.coordinator-self-source-v1.json",
        predecessor=None,
    )
    assert refused.verdict == "refused"
    assert refused.diagnostics[0].code == ("cruxible.capture_contract.coordinator_profile_mismatch")


def test_a_derive_manifest_records_state_reads_and_keeps_older_bytes() -> None:
    from cruxible_client.contracts.captures import (
        input_receipt_set_manifest_digest,
        render_input_receipt_set_manifest,
    )

    older = InputReceiptSetManifest(input_capture_digests=(digest("capture", "input"),))
    assert b"input_accepted_state_digests" not in render_input_receipt_set_manifest(older)
    reads_only = InputReceiptSetManifest(input_accepted_state_digests=(digest("read", "watched"),))
    assert reads_only.input_accepted_state_digests == (digest("read", "watched"),)
    assert input_receipt_set_manifest_digest(reads_only) != input_receipt_set_manifest_digest(older)
    with pytest.raises(ValueError, match="at least one exact input"):
        InputReceiptSetManifest()


def test_a_derive_reducer_is_content_addressed_and_read_back_exactly(tmp_path: Path) -> None:
    from cruxible_client.contracts.captures import (
        CaptureFormatError,
        DeriveReducer,
        derive_input_template_digest,
        derive_reducer_digest,
        read_derive_reducer,
        render_derive_reducer,
    )

    store = body_store(tmp_path)
    template = {"source": "$steps.catalog.capture", "watched": "$steps.watched.rows"}
    reducer = DeriveReducer(
        provider_artifact_digest=digest("provider", "build"),
        operation_signature_digest=digest("operation", "derive"),
        implementation_digest=digest("implementation", "derive"),
        input_template_digest=derive_input_template_digest(template),
    )
    stored = store.store(render_derive_reducer(reducer))
    assert stored.digest == derive_reducer_digest(reducer).tagged
    assert read_derive_reducer(store, stored.digest) == reducer
    # The template digest is the node's input alone: another template differs.
    assert derive_input_template_digest({**template, "watched": []}) != (
        reducer.input_template_digest
    )
    other = store.store(b'{"tag":"something-else"}')
    with pytest.raises(CaptureFormatError, match="reducer is invalid"):
        read_derive_reducer(store, other.digest)


# -- One Capture per citation (revision 72): own words located in a file ----------------

_LOCATED_CLAIM = "CLM-0123456789abcdef0123456789abcdef"
_LOCATED_BODY = b"The checks passed.\n"
# Computed by revision 71's builder (601152d4e) from these exact inputs.
_UNLOCATED_DIGEST = "sha256:791b750287f66cca00ffc26a2733b9aef91d3968fdabe4eda3fc901f6decc404"


def _location(body: bytes = _LOCATED_BODY, source_id: str = "repo.notes"):  # type: ignore[no-untyped-def]
    import base64
    import hashlib

    from cruxible_client.contracts.authoring.models import (
        WorkingAnchorWindow,
        WorkingDigestCoordinate,
        WorkingSelectionObservation,
    )

    page = b"# Decision\n" + body
    return WorkingSelectionObservation(
        source_id=source_id,
        coordinate=WorkingDigestCoordinate(
            source_content_digest="sha256:" + hashlib.sha256(page).hexdigest(),
            source_byte_length=len(page),
        ),
        selected_content_base64=base64.b64encode(body).decode("ascii"),
        selected_bytes_digest="sha256:" + hashlib.sha256(body).hexdigest(),
        selector=WorkingAnchorWindow(
            anchor=body.decode("ascii").strip(),
            start_byte=len(page) - len(body),
            end_byte=len(page),
            observed_occurrence_count=1,
        ),
    )


def _located(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = body_store(tmp_path)
    location = _location()
    result = build_coordinator_self_source_capture(
        store=store,
        actor_id="owner",
        claim_id=_LOCATED_CLAIM,
        body=_LOCATED_BODY,
        observed_at=NOW,
        accepted_coordinate=_accepted_coordinate(),
        location=location,
    )
    return store, location, result


def test_located_own_words_are_one_coordinator_capture_sourced_at_their_file(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.captures import (
        FOREIGN_SOURCE_COORDINATE_TYPE,
        FOREIGN_SOURCE_SELECTOR_TYPE,
        coordinator_location_valid,
    )
    from cruxible_client.contracts.source_references import ExternalSourceReference

    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    store, location, result = _located(tmp_path)
    envelope = result.envelope
    source = envelope.source
    assert isinstance(source, ExternalSourceReference)
    assert source.source_identity == "repo.notes"
    assert (source.coordinate_type, source.selector_type) == (
        FOREIGN_SOURCE_COORDINATE_TYPE,
        FOREIGN_SOURCE_SELECTOR_TYPE,
    )
    assert source.coordinate == location.coordinate.model_dump(mode="json")
    assert source.selector == {
        "claim_id": _LOCATED_CLAIM,
        "working_selection": location.selector.model_dump(mode="json"),
    }
    assert source.replayability == "attested_only"
    assert envelope.capture_contract_digest == capture_contract_digest(contract).tagged
    assert envelope.commitment.digest == location.selected_bytes_digest
    assert envelope.run_coordinate.run_id == f"coordinator-self-source:{_LOCATED_CLAIM.casefold()}"
    assert coordinator_location_valid(envelope)
    assert verify_capture(result.capture_digest, store=store, contract=contract) == envelope
    assert capture_is_coordinator_self_source(envelope, contract=contract, claim_id=_LOCATED_CLAIM)
    reuse = classify_capture_reuse(
        envelope, contract=contract, store=store, claim_id=_LOCATED_CLAIM
    )
    assert reuse == "claim_bound"
    other = classify_capture_reuse(
        envelope,
        contract=contract,
        store=store,
        claim_id="CLM-abcdefabcdefabcdefabcdefabcdefab",
    )
    assert other == "claim_bound_mismatch"


def test_a_location_must_select_exactly_the_own_words(tmp_path: Path) -> None:
    from cruxible_client.contracts.captures import CaptureFormatError

    with pytest.raises(CaptureFormatError, match="exactly the own words"):
        build_coordinator_self_source_capture(
            store=body_store(tmp_path),
            actor_id="owner",
            claim_id=_LOCATED_CLAIM,
            body=b"Other words.\n",
            observed_at=NOW,
            accepted_coordinate=_accepted_coordinate(),
            location=_location(),
        )


@pytest.mark.parametrize(
    "forgery",
    ["schema", "replayability", "selector_claim", "binding", "foreign_contract"],
)
def test_a_located_coordinator_capture_outside_its_one_form_refuses(
    tmp_path: Path, forgery: str
) -> None:
    from cruxible_client.contracts.captures import (
        CaptureFormatError,
        coordinator_location_valid,
        foreign_source_capture_contract,
        render_capture_envelope,
    )

    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    store, _location_used, result = _located(tmp_path)
    envelope = result.envelope
    source = envelope.source
    assert source.kind == "external"
    selector = dict(source.selector)  # type: ignore[union-attr, arg-type]
    if forgery == "schema":
        forged_source = source.model_copy(update={"selector_type": "whole-response-v1"})
    elif forgery == "replayability":
        forged_source = source.model_copy(update={"replayability": "exact"})
    elif forgery == "selector_claim":
        selector["claim_id"] = "CLM-abcdefabcdefabcdefabcdefabcdefab"
        forged_source = source.model_copy(update={"selector": selector})
    elif forgery == "binding":
        forged_source = source.model_copy(update={"producer_binding_digest": digest("x", "y")})
    else:
        forged_source = source
    forged = envelope.model_copy(update={"source": forged_source})
    stored = store.store(render_capture_envelope(forged))
    if forgery == "foreign_contract":
        # The located form is the coordinator contract's alone: the same envelope
        # under the file's own contract does not verify as that contract's.
        with pytest.raises(CaptureFormatError):
            verify_capture(
                stored.digest, store=store, contract=foreign_source_capture_contract("repo.notes")
            )
        return
    assert not coordinator_location_valid(forged)
    with pytest.raises(CaptureFormatError):
        verify_capture(stored.digest, store=store, contract=contract)
    assert (
        classify_capture_reuse(forged, contract=contract, store=store, claim_id=_LOCATED_CLAIM)
        == "claim_bound_mismatch"
    )


def test_unlocated_own_words_keep_their_exact_bytes(tmp_path: Path) -> None:
    """``location=None`` builds the very envelope revision 71 built (digest pinned)."""

    from cruxible_client.contracts.canonical import Sha256Value, typed_digest

    store = body_store(tmp_path)
    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    unlocated = build_coordinator_self_source_capture(
        store=store,
        actor_id="owner",
        claim_id=_LOCATED_CLAIM,
        body=_LOCATED_BODY,
        observed_at=NOW,
        accepted_coordinate=_accepted_coordinate(),
    )
    # The pre-revision-72 composition, spelled out: one CAS observation under the
    # coordinator contract with the original receipt and binding preimages.
    coordinate = _accepted_coordinate()
    expected = build_cas_capture(
        store=store,
        contract=contract,
        source_body=_LOCATED_BODY,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"coordinator-self-source:{_LOCATED_CLAIM.casefold()}",
            bound_generation=coordinate.generation_root,
            executable_identity=contract.identity,
            executable_digest=capture_contract_digest(contract).tagged,
        ),
        run_receipt_digest=typed_digest(
            Sha256Value,
            "playbill-coordinator-self-source-receipt-v1",
            {
                "actor_id": "owner",
                "body_digest": unlocated.commitment_digest,
                "claim_id": _LOCATED_CLAIM,
                "observed_at": NOW.isoformat(),
            },
        ).tagged,
        producer=ArtifactIdentity(kind="Principal", name="owner"),
        producer_binding_digest=typed_digest(
            Sha256Value,
            "playbill-coordinator-self-source-binding-v1",
            {
                "actor_id": "owner",
                "claim_id": _LOCATED_CLAIM,
                "generation_root": coordinate.generation_root,
            },
        ).tagged,
        observed_at=NOW,
    )
    assert unlocated.capture_digest == expected.capture_digest
    assert unlocated.capture_digest == _UNLOCATED_DIGEST
    _store, _location_used, located = _located(tmp_path / "located")
    assert located.capture_digest != unlocated.capture_digest
    assert located.commitment_digest == unlocated.commitment_digest


def test_located_own_words_reproduce_their_commitment_length(tmp_path: Path) -> None:
    """A located envelope claiming another length than its retained bytes refuses.

    The unlocated (CAS) form already refuses the same forgery; the located form
    holds the same exact bytes and refuses alike.
    """

    from cruxible_client.contracts.captures import CaptureFormatError, render_capture_envelope

    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    store, _location_used, result = _located(tmp_path)
    for forged_from in (
        result.envelope,
        build_coordinator_self_source_capture(
            store=store,
            actor_id="owner",
            claim_id=_LOCATED_CLAIM,
            body=_LOCATED_BODY,
            observed_at=NOW,
            accepted_coordinate=_accepted_coordinate(),
        ).envelope,
    ):
        forged = forged_from.model_copy(
            update={"commitment": forged_from.commitment.model_copy(update={"byte_length": 0})}
        )
        digest = store.store(render_capture_envelope(forged)).digest
        with pytest.raises(CaptureFormatError, match="byte length does not reproduce"):
            verify_capture(digest, store=store, contract=contract)
