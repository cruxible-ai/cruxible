"""Non-authoritative SDK program provenance on AuthoringIntent events."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    AUTHORING_SDK_VERSION,
    AuthoringProgramOperationV1,
    AuthoringProgramStampV1,
    authoring_program_digest,
)
from cruxible_core.playbill.authoring.coordinator import (
    AuthoringIntentCoordinator,
    AuthoringProgramStampError,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentEventV3
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _self_source_payload,
)


def _stamp(operation: str = "claim") -> AuthoringProgramStampV1:
    return AuthoringProgramStampV1(
        program_digest=authoring_program_digest(
            sdk_contract_snapshot_digest=AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
            operations=(
                AuthoringProgramOperationV1(
                    operation=operation,
                    decisions={"subject": "project.work_item/wi-42"},
                ),
            ),
        ),
        sdk_version=AUTHORING_SDK_VERSION,
        sdk_contract_snapshot_digest=AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    )


def test_program_stamp_is_event_committed_and_identity_excluded(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _self_source_payload()

    first = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
        reference_expectations=(),
        program_stamp=_stamp("claim"),
    ).intent
    first_events = coordinator.store._load_events(  # noqa: SLF001 - wire proof
        coordinator.store.root / first.intent_id
    )
    repeated = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
        reference_expectations=(),
        program_stamp=_stamp("claim"),
    ).intent
    second = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
        reference_expectations=(),
        program_stamp=_stamp("revise"),
    ).intent
    events = coordinator.store._load_events(  # noqa: SLF001 - wire proof
        coordinator.store.root / first.intent_id
    )

    assert repeated == first == second
    assert len(first_events) == 2
    assert len(events) == 3
    assert isinstance(events[1], AuthoringIntentEventV3)
    assert isinstance(events[2], AuthoringIntentEventV3)
    assert events[1].program_stamp != events[2].program_stamp
    assert events[1].event_digest != events[2].event_digest
    assert first.intent_revision == 0


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            {"sdk_version": "999.0.0"},
            "playbill.authoring.program_stamp_version_incompatible",
        ),
        (
            {"sdk_contract_snapshot_digest": "sha256:" + "0" * 64},
            "playbill.authoring.program_stamp_contract_mismatch",
        ),
        (
            {
                "sdk_contract_snapshot_digest": (
                    "sha256:f802cd994cf904b94f4a8714b7b44c9d5db1e5b5b5ad33541ff5a609fb6d04c8"
                )
            },
            "playbill.authoring.program_stamp_contract_mismatch",
        ),
    ],
)
def test_program_stamp_requires_exact_daemon_contract(
    tmp_path: Path,
    change: dict[str, str],
    code: str,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)

    with pytest.raises(AuthoringProgramStampError) as caught:
        coordinator.create(
            actor=AuthenticatedActor(actor_id="owner"),
            payload=_self_source_payload(),
            canonical_timestamp=TIMESTAMP,
            reference_expectations=(),
            program_stamp=_stamp().model_copy(update=change),
        )

    assert caught.value.code == code
