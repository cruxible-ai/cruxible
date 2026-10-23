"""Erasing a Capture's retained body: governed by its contract, previewed, recorded, resurfaced."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.capture_erasure import (
    CaptureEraseRefusedError,
    CaptureEraseRequest,
    CaptureEraseResult,
)
from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.captures import (
    CAPTURE_COMPONENTS,
    authorize_erasure,
    capture_component_pin,
    capture_contract_path,
    evaluate_capture_contract_law,
)
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.repairs import HandEditRepair, RepairOperation
from cruxible_client.contracts.write import capture_handle
from cruxible_core.errors import ChangeRefusedError
from cruxible_core.evidence import capture_erasures
from cruxible_core.evidence.capture_erasures import CaptureErasureStore
from cruxible_core.server.errors import error_to_response
from cruxible_core.service.change_preview import change_entry, state_change_scope
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.evidence.capture_erasure import service_erase_capture
from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
from tests.core_support._pc_c_support import capture_contract
from tests.support.capture_erasure_world import READ, ErasureWorld, erasure_world, write

BECAUSE = "The scan output printed an API key; rotated, so the bytes must go."


def _erase(
    world: ErasureWorld,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
    capture: str | None = None,
) -> CaptureEraseResult:
    """The facade's own scope around the service: preview by default, commit with ``at``."""

    request = CaptureEraseRequest(
        capture_digest=capture or world.capture, because=BECAUSE, dry_run=dry_run, at=at
    )
    with change_entry(request.dry_run, "irreversible"):
        with state_change_scope(
            dry_run=request.dry_run,
            at=request.at,
            kind="irreversible",
            operation="cruxible.capture.erase",
            describe="erasing",
        ) as change:
            return service_erase_capture(
                world.instance,
                request=request,
                actor_id="owner",
                previewing=change.previewing,
                observe=change.observe,
            )


def _evidence(world: ErasureWorld):  # type: ignore[no-untyped-def]
    result = service_playbill_get(
        world.instance, request=GetRequest(ref=world.claim, detail="evidence"), access=READ
    )
    assert result.evidence is not None
    return result.evidence


@pytest.fixture
def world(tmp_path: Path) -> ErasureWorld:
    return erasure_world(tmp_path)


def test_a_preview_names_what_would_go_and_writes_nothing(world: ErasureWorld) -> None:
    store = world.instance.body_store()

    preview = _erase(world)

    assert preview.status == "would_erase"
    assert preview.erasure.body_digests == (world.body,)
    assert preview.erasure.capture_contract == world.contract.identity.qualified
    assert preview.citing_claims == (world.claim,) and preview.citing_claim_count == 1
    # The only evidence this Claim has: a commit would leave it bare.
    assert preview.left_without_evidence == (world.claim,)
    assert store.verify(world.body)
    assert not CaptureErasureStore(world.instance).root.exists()


def test_a_commit_needs_the_previews_coordinate(world: ErasureWorld) -> None:
    with pytest.raises(ChangeRefusedError) as unconfirmed:
        _erase(world, dry_run=False)
    assert unconfirmed.value.error_code == "cruxible.preview.confirmation_required"
    with pytest.raises(ChangeRefusedError) as wrong:
        _erase(world, dry_run=False, at="0" * 64)
    assert wrong.value.error_code == "cruxible.preview.state_moved"
    assert world.instance.body_store().verify(world.body)


def test_erasing_destroys_the_body_and_keeps_the_envelope_and_a_record(
    world: ErasureWorld,
) -> None:
    preview = _erase(world)

    erased = _erase(world, dry_run=False, at=preview.coordinate.digest)

    store = world.instance.body_store()
    assert erased.status == "erased"
    assert erased.erasure.erased_by == "owner" and erased.erasure.because == BECAUSE
    assert not store._path(world.body).exists()
    assert store.verify(world.capture)  # the envelope, the evidence record, stays
    recorded = CaptureErasureStore(world.instance).for_capture(world.capture)
    assert recorded == erased.erasure
    # Again: nothing new is recorded, and the stale preview no longer commits.
    assert _erase(world).status == "already_erased"
    with pytest.raises(ChangeRefusedError) as moved:
        _erase(world, dry_run=False, at=preview.coordinate.digest)
    assert moved.value.error_code == "cruxible.preview.state_moved"


def test_reads_show_the_evidence_as_erased_and_the_verdict_reads_it_unreplayable(
    world: ErasureWorld,
) -> None:
    before = _evidence(world)
    (cited,) = before.captures
    assert cited.erased is None
    card = service_playbill_get(
        world.instance, request=GetRequest(ref=world.claim), access=READ
    ).card
    verdict = card.verdict  # type: ignore[union-attr]

    _erase(world, dry_run=False, at=_erase(world).coordinate.digest)

    (after,) = _evidence(world).captures
    assert after.erased is not None
    assert (after.erased.by, after.erased.because) == ("owner", BECAUSE)
    assert after.status == cited.status  # admission is unchanged: core does not reinterpret
    card = service_playbill_get(
        world.instance, request=GetRequest(ref=world.claim), access=READ
    ).card
    # The field's own rule (require current replay) reads unreplayable evidence as
    # stale; erasure adds no interpretation of its own.
    assert (verdict, card.verdict) == ("supported", "stale")  # type: ignore[union-attr]
    read = service_read_playbill_capture(
        world.instance, request=CaptureReadRequest(capture_digest=world.capture), access=READ
    )
    assert (read.status, read.reason) == ("unavailable", "body_erased")
    assert read.erasure is not None and read.erasure.because == BECAUSE
    capture_card = service_playbill_get(
        world.instance, request=GetRequest(ref=capture_handle(world.capture)), access=READ
    ).card
    assert capture_card.status == "unavailable"  # type: ignore[union-attr]
    assert capture_card.erased is not None  # type: ignore[union-attr]


def test_a_contract_that_does_not_authorize_erasure_refuses_with_the_repair(
    tmp_path: Path,
) -> None:
    world = erasure_world(tmp_path, erasure=False)

    with pytest.raises(CaptureEraseRefusedError) as refused:
        _erase(world)

    error = refused.value
    assert error.error_code == "cruxible.capture.erasure_prohibited"
    # Public names only: the contract and the helper that opts a new one in, never
    # an internal field path or the rule's pin identifier.
    text = str(error)
    assert world.contract.identity.qualified in text and "authorize_erasure" in text
    assert "retention_erasure_policy" not in text and "playbill." not in text
    assert isinstance(error.repair, HandEditRepair)
    assert error.repair.hand_edit.target == world.contract.identity.qualified
    assert "authorize_erasure" in error.repair.hand_edit.required_change
    status, body = error_to_response(error)
    assert status == 409 and body.error_code == "cruxible.capture.erasure_prohibited"
    assert body.repair == error.repair
    assert body.context["capture_digest"] == world.capture
    assert body.context["repair_line"] == error.repair_line
    assert world.instance.body_store().verify(world.body)


def test_an_unknown_digest_refuses(world: ErasureWorld) -> None:
    with pytest.raises(CaptureEraseRefusedError) as refused:
        _erase(world, capture="sha256:" + "e" * 64)
    assert refused.value.error_code == "cruxible.capture.erasure_unavailable"
    assert refused.value.repair == RepairOperation(
        operation="cruxible.get", arguments={"ref": "sha256:" + "e" * 64}
    )


def test_authorize_erasure_sets_the_policy_and_the_pin_together() -> None:
    prohibited = capture_contract()
    authorized = authorize_erasure(prohibited)

    policy = authorized.retention_erasure_policy
    assert policy.erasure == "authorized_by_rule"
    (rule,) = (pin for pin in authorized.pins if pin.role == "erasure-rule")
    assert policy.erasure_rule_digest == rule.artifact_digest
    assert CAPTURE_COMPONENTS.resolves(rule)
    # Everything else is the contract as written, and applying it twice is a no-op.
    assert (
        authorized.model_copy(
            update={
                "retention_erasure_policy": prohibited.retention_erasure_policy,
                "pins": prohibited.pins,
            }
        )
        == prohibited
    )
    assert authorize_erasure(authorized) == authorized
    assert (
        evaluate_capture_contract_law(
            authorized, path=capture_contract_path("test.orders-v1"), predecessor=None
        ).verdict
        == "accepted"
    )


def test_a_policy_without_its_pin_refuses_naming_authorize_erasure() -> None:
    rule = capture_component_pin("erasure-rule", "playbill.capture-erasure-authority-v1")
    contract = capture_contract()
    unpinned = contract.model_copy(
        update={
            "retention_erasure_policy": contract.retention_erasure_policy.model_copy(
                update={
                    "erasure": "authorized_by_rule",
                    "erasure_rule_digest": rule.artifact_digest,
                }
            )
        }
    )

    result = evaluate_capture_contract_law(
        unpinned, path=capture_contract_path("test.orders-v1"), predecessor=None
    )

    assert result.verdict == "refused"
    (diagnostic,) = result.diagnostics
    assert diagnostic.code == "cruxible.capture_contract.erasure_rule_unresolved"
    assert "authorize_erasure(contract)" in diagnostic.message


def test_an_interrupted_erase_finishes_under_its_first_record(
    world: ErasureWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    preview = _erase(world)
    original = ContentAddressedBodyStore.erase

    def crash(self, digest):  # type: ignore[no-untyped-def]
        raise OSError("power cut")

    monkeypatch.setattr(ContentAddressedBodyStore, "erase", crash)
    with pytest.raises(OSError):
        _erase(world, dry_run=False, at=preview.coordinate.digest)
    first = CaptureErasureStore(world.instance).for_capture(world.capture)
    assert first is not None and world.instance.body_store().verify(world.body)
    monkeypatch.setattr(ContentAddressedBodyStore, "erase", original)

    retried = _erase(world, dry_run=False, at=_erase(world).coordinate.digest)

    assert retried.status == "erased" and retried.erasure == first
    assert not world.instance.body_store()._path(world.body).exists()


def test_the_cost_is_the_capture_and_the_claims_citing_it(
    world: ErasureWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unrelated Claims grow the instance, not the erase.
    for index in range(2, 4):
        assert (
            write(
                world.instance,
                {
                    "op": "set",
                    "subject": f"project.work_item/wi-{index}",
                    "field": "title",
                    "value": f"item {index}",
                },
                because="Unrelated.",
            ).refusal
            is None
        )
    looked_up: list[str] = []
    original = capture_erasures.evidence_captures

    def counting(connection, claim):  # type: ignore[no-untyped-def]
        looked_up.append(claim)
        return original(connection, claim)

    monkeypatch.setattr(capture_erasures, "evidence_captures", counting)

    _erase(world, dry_run=False, at=_erase(world).coordinate.digest)

    # One citation lookup per citing Claim, per call (the preview and the commit).
    assert looked_up == [f"Claim:{world.claim}"] * 2


def test_the_worker_resurfaces_a_claim_left_bare_and_clears_it_when_rebound(
    world: ErasureWorld,
) -> None:
    from cruxible_core.consumers.next.evidence import erased_claims, evidence_findings
    from tests.support.capture_erasure_world import rebind, rescan
    from tests.test_consumers.test_evidence_availability import NOW, _drain

    instance = world.instance
    _drain(instance, now=NOW)
    _erase(world, dry_run=False, at=_erase(world).coordinate.digest)
    _drain(instance, now=NOW + timedelta(minutes=1))

    (bare,) = erased_claims(instance)
    assert bare.claim_identity == f"Claim:{world.claim}"
    assert bare.capture_digests == (world.capture,)
    assert evidence_findings(instance) == ()  # an erased body is no damage

    # Re-bound onto a fresh observation: the row follows the Claim away.
    outcome = rebind(instance, world.claim, rescan(world))
    assert outcome.refusal is None, outcome
    _drain(instance, now=NOW + timedelta(minutes=2))
    assert erased_claims(instance) == ()
    (cited,) = _evidence(world).captures
    assert cited.erased is None and cited.capture != capture_handle(world.capture)


def test_a_claim_left_bare_can_still_be_retired(world: ErasureWorld) -> None:
    _erase(world, dry_run=False, at=_erase(world).coordinate.digest)

    # The retirement inherits the erased Capture: it verifies on its envelope and
    # the erasure record, so the Claim is not stranded (compiler revision 35).
    retired = write(world.instance, {"op": "retire", "target": world.claim}, because="Erased.")

    assert retired.refusal is None, retired.refusal


def test_a_claim_left_bare_can_still_be_revised_onto_new_evidence(world: ErasureWorld) -> None:
    from tests.support.capture_erasure_world import cite, rescan

    _erase(world, dry_run=False, at=_erase(world).coordinate.digest)
    fresh = rescan(world)

    # The type accumulates evidence, so the revision inherits the erased Capture too.
    revised = cite(world.instance, fresh, "low")

    assert revised.refusal is None, revised.refusal
    assert revised.changes[0].claim == world.claim
    erased, current = sorted(_evidence(world).captures, key=lambda item: item.erased is None)
    assert erased.erased is not None and current.erased is None


def test_citing_an_erased_capture_anew_still_needs_its_body(world: ErasureWorld) -> None:
    _erase(world, dry_run=False, at=_erase(world).coordinate.digest)

    outcome = write(
        world.instance,
        {
            "op": "set",
            "subject": "project.work_item/wi-2",
            "field": "severity",
            "value": "high",
            "evidence": {"kind": "capture", "capture": world.capture},
        },
        because="Cites the erased scan.",
    )

    assert outcome.refusal is not None


def test_an_erasure_record_that_is_not_the_contracts_own_does_not_verify(
    world: ErasureWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.exhaust.producer_receipts import LocalProducerReceiptResolver

    erased = _erase(world, dry_run=False, at=_erase(world).coordinate.digest).erasure
    forged = erased.model_copy(update={"erasure_rule_digest": "sha256:" + "0" * 64})
    monkeypatch.setattr(
        LocalProducerReceiptResolver, "capture_erasure", lambda self, digest: forged
    )

    retired = write(world.instance, {"op": "retire", "target": world.claim}, because="Erased.")

    assert retired.refusal is not None
    assert retired.refusal.code == "cruxible.claim.capture_unverified"
