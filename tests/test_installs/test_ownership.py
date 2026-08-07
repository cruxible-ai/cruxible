"""Ownership records, the collision check, and the queries an installer needs."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import (
    InstallNotFoundError,
    InstallOwnershipCollisionError,
    InstallPhaseRequirementError,
)
from cruxible_core.installs.types import OWNED_OBJECT_KINDS, OwnedObjectKind
from cruxible_core.service import (
    service_advance_install_phase,
    service_check_ownership_collision,
    service_get_install,
    service_install_owning_object,
    service_objects_owned_by_install,
    service_record_owned_object,
)
from tests.test_installs.conftest import (
    actor,
    create_install,
    load_receipt,
    reference,
    validation_details,
)


def _own(
    instance: CruxibleInstance,
    install_id: str,
    *,
    object_kind: OwnedObjectKind = "named_query",
    object_name: str = "pub.kev.triage_queue",
    installed_digest: str = "sha256:q1",
    references: tuple[object, ...] = (),
):
    return service_record_owned_object(
        instance,
        install_id,
        object_kind=object_kind,
        object_name=object_name,
        installed_digest=installed_digest,
        references=references,  # type: ignore[arg-type]
        actor_context=actor(),
    )


# ---------------------------------------------------------------------------
# Recording ownership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("object_kind", OWNED_OBJECT_KINDS)
def test_every_declared_object_kind_can_be_owned(
    instance: CruxibleInstance,
    object_kind: OwnedObjectKind,
) -> None:
    record = create_install(instance)
    owned = _own(instance, record.install_id, object_kind=object_kind, object_name="pub.a.X")

    assert owned.object_kind == object_kind
    assert owned.customized is False
    assert owned.current_digest is None


def test_owned_objects_are_listed_for_their_install(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    _own(instance, record.install_id, object_kind="contract", object_name="pub.kev.Request")
    _own(instance, record.install_id, object_kind="named_query", object_name="pub.kev.queue")

    owned = service_objects_owned_by_install(instance, record.install_id)
    assert [(item.object_kind, item.object_name) for item in owned] == [
        ("contract", "pub.kev.Request"),
        ("named_query", "pub.kev.queue"),
    ]


def test_install_detail_carries_owned_objects(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    _own(instance, record.install_id)

    detail = service_get_install(instance, record.install_id)
    assert [item.object_name for item in detail.owned_objects] == ["pub.kev.triage_queue"]


def test_declared_references_round_trip(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    owned = _own(
        instance,
        record.install_id,
        object_kind="procedure",
        object_name="pub.kev.triage",
        references=(
            reference("named_query", "pub.kev.queue"),
            reference("contract", "pub.kev.Request"),
        ),
    )

    assert [(ref.object_kind, ref.object_name) for ref in owned.references] == [
        ("named_query", "pub.kev.queue"),
        ("contract", "pub.kev.Request"),
    ]
    reloaded = service_objects_owned_by_install(instance, record.install_id)[0]
    assert reloaded.references == owned.references


def test_recording_ownership_is_receipted(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    owned = _own(instance, record.install_id)

    receipt = load_receipt(instance, owned.receipt_id)
    assert receipt.operation_type == "install_record_owned_object"
    assert receipt.committed is True
    assert validation_details(receipt) == [
        {
            "passed": True,
            "install_id": record.install_id,
            "object_kind": "named_query",
            "object_name": "pub.kev.triage_queue",
        }
    ]


def test_ownership_on_an_unknown_install_raises_not_found(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(InstallNotFoundError):
        _own(instance, "inst-nope")


# ---------------------------------------------------------------------------
# Phase requirement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["pending_acceptance", "active", "failed"])
def test_ownership_may_only_be_claimed_while_preparing(
    instance: CruxibleInstance,
    phase: str,
) -> None:
    record = create_install(instance)
    path = {
        "pending_acceptance": ("pending_acceptance",),
        "active": ("pending_acceptance", "active"),
        "failed": ("failed",),
    }[phase]
    for step in path:
        service_advance_install_phase(
            instance,
            record.install_id,
            to_phase=step,
            actor_context=actor(),  # type: ignore[arg-type]
        )

    with pytest.raises(InstallPhaseRequirementError) as excinfo:
        _own(instance, record.install_id)

    assert excinfo.value.actual_phase == phase
    assert excinfo.value.required_phases == ["preparing"]
    assert f"'{phase}'" in str(excinfo.value)


def test_late_ownership_refusal_is_receipted(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    service_advance_install_phase(
        instance, record.install_id, to_phase="pending_acceptance", actor_context=actor()
    )

    with pytest.raises(InstallPhaseRequirementError) as excinfo:
        _own(instance, record.install_id)

    receipt = load_receipt(instance, excinfo.value.mutation_receipt_id)
    assert receipt.committed is False
    assert any(
        detail.get("actual_phase") == "pending_acceptance" for detail in validation_details(receipt)
    )


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------


def test_a_second_install_cannot_claim_a_held_name(instance: CruxibleInstance) -> None:
    first = create_install(instance, artifact_id="a", install_id="inst-a")
    second = create_install(instance, artifact_id="b", install_id="inst-b")
    _own(instance, first.install_id)

    with pytest.raises(InstallOwnershipCollisionError) as excinfo:
        _own(instance, second.install_id, installed_digest="sha256:other")

    error = excinfo.value
    assert error.object_name == "pub.kev.triage_queue"
    assert error.owning_install_id == "inst-a"
    assert error.owning_install_phase == "preparing"


def test_collision_refusal_is_receipted_and_writes_nothing(
    instance: CruxibleInstance,
) -> None:
    first = create_install(instance, artifact_id="a", install_id="inst-a")
    second = create_install(instance, artifact_id="b", install_id="inst-b")
    _own(instance, first.install_id)

    with pytest.raises(InstallOwnershipCollisionError) as excinfo:
        _own(instance, second.install_id)

    receipt = load_receipt(instance, excinfo.value.mutation_receipt_id)
    assert receipt.committed is False
    assert any(
        detail.get("reason") == "ownership collision" for detail in validation_details(receipt)
    )
    assert service_objects_owned_by_install(instance, second.install_id) == []


def test_the_same_name_under_a_different_kind_is_not_a_collision(
    instance: CruxibleInstance,
) -> None:
    first = create_install(instance, artifact_id="a", install_id="inst-a")
    second = create_install(instance, artifact_id="b", install_id="inst-b")
    _own(instance, first.install_id, object_kind="contract", object_name="Shared")
    _own(instance, second.install_id, object_kind="named_query", object_name="Shared")

    assert len(service_objects_owned_by_install(instance, second.install_id)) == 1


def _walk(instance: CruxibleInstance, install_id: str, path: tuple[str, ...]) -> None:
    for step in path:
        service_advance_install_phase(
            instance,
            install_id,
            to_phase=step,
            actor_context=actor(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("path", [("failed",), ("failed", "rolling_back")])
def test_a_failing_install_holds_its_names_until_it_is_rolled_back(
    instance: CruxibleInstance,
    path: tuple[str, ...],
) -> None:
    """Failing is not undoing.

    An install that fails may already have written the objects it claimed, and
    it still has to traverse rollback to take them back. If the name freed at
    ``failed``, a second install could claim it and the first install's rollback
    would then remove or overwrite an object the second one now owns.
    """
    first = create_install(instance, artifact_id="a", install_id="inst-a")
    _own(instance, first.install_id)
    _walk(instance, first.install_id, path)

    collision = service_check_ownership_collision(
        instance, object_kind="named_query", object_name="pub.kev.triage_queue"
    )
    assert collision is not None
    assert collision.owning_install_id == "inst-a"
    assert collision.owning_install_phase == path[-1]

    second = create_install(instance, artifact_id="a", install_id="inst-b")
    with pytest.raises(InstallOwnershipCollisionError) as excinfo:
        _own(instance, second.install_id, installed_digest="sha256:other")

    assert excinfo.value.owning_install_id == "inst-a"
    assert excinfo.value.owning_install_phase == path[-1]
    assert service_objects_owned_by_install(instance, second.install_id) == []


def test_a_rolled_back_install_frees_its_names(instance: CruxibleInstance) -> None:
    """Re-installing after a completed rollback must not be blocked."""
    first = create_install(instance, artifact_id="a", install_id="inst-a")
    _own(instance, first.install_id)
    _walk(instance, first.install_id, ("failed", "rolling_back", "rolled_back"))

    assert (
        service_check_ownership_collision(
            instance, object_kind="named_query", object_name="pub.kev.triage_queue"
        )
        is None
    )

    second = create_install(instance, artifact_id="a", install_id="inst-b")
    owned = _own(instance, second.install_id)
    assert owned.install_id == "inst-b"


def test_an_install_that_mutated_nothing_still_walks_rollback_before_its_names_free(
    instance: CruxibleInstance,
) -> None:
    """The price of holding through ``failed``, pinned deliberately.

    Nothing distinguishes "failed having written objects" from "failed having
    written none" — the ledger cannot know — so the no-mutation case pays a
    no-op rollback rather than the mutating case racing.
    """
    first = create_install(instance, artifact_id="a", install_id="inst-a")
    _own(instance, first.install_id)
    _walk(instance, first.install_id, ("failed",))
    second = create_install(instance, artifact_id="a", install_id="inst-b")

    with pytest.raises(InstallOwnershipCollisionError):
        _own(instance, second.install_id)

    _walk(instance, first.install_id, ("rolling_back", "rolled_back"))

    assert _own(instance, second.install_id).install_id == "inst-b"


def test_an_active_install_keeps_holding_its_names(instance: CruxibleInstance) -> None:
    first = create_install(instance, artifact_id="a", install_id="inst-a")
    _own(instance, first.install_id)
    for step in ("pending_acceptance", "active"):
        service_advance_install_phase(
            instance,
            first.install_id,
            to_phase=step,
            actor_context=actor(),  # type: ignore[arg-type]
        )

    collision = service_check_ownership_collision(
        instance, object_kind="named_query", object_name="pub.kev.triage_queue"
    )
    assert collision is not None
    assert collision.owning_install_phase == "active"


# ---------------------------------------------------------------------------
# Lookup queries
# ---------------------------------------------------------------------------


def test_collision_check_reports_nothing_for_an_unclaimed_name(
    instance: CruxibleInstance,
) -> None:
    assert (
        service_check_ownership_collision(
            instance, object_kind="contract", object_name="pub.nobody.X"
        )
        is None
    )


def test_install_owning_object_returns_the_owning_record(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance, install_id="inst-a")
    _own(instance, record.install_id)

    owner = service_install_owning_object(
        instance, object_kind="named_query", object_name="pub.kev.triage_queue"
    )
    assert owner is not None
    assert owner.install_id == "inst-a"
    assert owner.artifact.artifact_id == "kev-triage"


def test_install_owning_object_still_names_a_failed_owner(
    instance: CruxibleInstance,
) -> None:
    """ "Who owns this name" must not go blank the moment an install fails.

    The failed install is precisely who has to clean the object up.
    """
    record = create_install(instance, install_id="inst-a")
    _own(instance, record.install_id)
    _walk(instance, record.install_id, ("failed",))

    owner = service_install_owning_object(
        instance, object_kind="named_query", object_name="pub.kev.triage_queue"
    )
    assert owner is not None
    assert owner.install_id == "inst-a"
    assert owner.phase == "failed"


def test_install_owning_object_is_none_once_the_owner_rolled_back(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)
    _own(instance, record.install_id)
    _walk(instance, record.install_id, ("failed", "rolling_back", "rolled_back"))

    assert (
        service_install_owning_object(
            instance, object_kind="named_query", object_name="pub.kev.triage_queue"
        )
        is None
    )


def test_objects_owned_by_unknown_install_raises_not_found(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(InstallNotFoundError):
        service_objects_owned_by_install(instance, "inst-missing")
