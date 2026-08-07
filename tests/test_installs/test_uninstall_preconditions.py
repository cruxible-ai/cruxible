"""Uninstall PRECONDITIONS: what the ledger can block on, and what it cannot.

Phase 1 ships the check, not the uninstall. These tests pin both halves of the
contract — the blockers it finds, and the blindness it is required to declare.
"""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import InstallNotFoundError
from cruxible_core.installs.types import (
    UNOBSERVABLE_REFERENCE_SOURCES,
    compute_install_object_digest,
)
from cruxible_core.service import (
    service_advance_install_phase,
    service_record_object_customization,
    service_record_owned_object,
    service_uninstall_preconditions,
)
from tests.test_installs.conftest import actor, create_install, reference


def _own(
    instance: CruxibleInstance,
    install_id: str,
    *,
    object_kind: str,
    object_name: str,
    references: tuple[object, ...] = (),
    installed_digest: str = "sha256:obj",
):
    return service_record_owned_object(
        instance,
        install_id,
        object_kind=object_kind,  # type: ignore[arg-type]
        object_name=object_name,
        installed_digest=installed_digest,
        references=references,  # type: ignore[arg-type]
        actor_context=actor(),
    )


def _activate(instance: CruxibleInstance, install_id: str) -> None:
    for phase in ("pending_acceptance", "active"):
        service_advance_install_phase(
            instance,
            install_id,
            to_phase=phase,
            actor_context=actor(),  # type: ignore[arg-type]
        )


def _provider_and_consumer(instance: CruxibleInstance) -> tuple[str, str]:
    """One install owning a contract, another whose procedure references it."""
    provider = create_install(instance, artifact_id="base", install_id="inst-base")
    _own(instance, provider.install_id, object_kind="contract", object_name="pub.base.Request")

    consumer = create_install(instance, artifact_id="triage", install_id="inst-triage")
    _own(
        instance,
        consumer.install_id,
        object_kind="procedure",
        object_name="pub.triage.run",
        references=(reference("contract", "pub.base.Request"),),
    )
    return provider.install_id, consumer.install_id


# ---------------------------------------------------------------------------
# Blockers
# ---------------------------------------------------------------------------


def test_an_unreferenced_install_is_not_blocked(instance: CruxibleInstance) -> None:
    record = create_install(instance)
    _own(instance, record.install_id, object_kind="named_query", object_name="pub.a.q")
    _activate(instance, record.install_id)

    report = service_uninstall_preconditions(instance, record.install_id)
    assert report.blocked is False
    assert report.blockers == []
    assert report.install_phase == "active"


def test_a_referenced_object_blocks_removal(instance: CruxibleInstance) -> None:
    provider_id, consumer_id = _provider_and_consumer(instance)
    _activate(instance, provider_id)
    _activate(instance, consumer_id)

    report = service_uninstall_preconditions(instance, provider_id)
    assert report.blocked is True
    assert len(report.blockers) == 1
    blocker = report.blockers[0]
    assert blocker.object_kind == "contract"
    assert blocker.object_name == "pub.base.Request"
    assert blocker.referencing_install_id == "inst-triage"
    assert blocker.referencing_install_phase == "active"
    assert blocker.referencing_object_name == "pub.triage.run"


def test_the_referencing_install_is_itself_removable(instance: CruxibleInstance) -> None:
    """Dependencies are directional: the consumer blocks nothing."""
    provider_id, consumer_id = _provider_and_consumer(instance)
    _activate(instance, provider_id)
    _activate(instance, consumer_id)

    assert service_uninstall_preconditions(instance, consumer_id).blocked is False


def test_an_installs_own_internal_references_never_block_it(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)
    _own(instance, record.install_id, object_kind="contract", object_name="pub.a.Request")
    _own(
        instance,
        record.install_id,
        object_kind="procedure",
        object_name="pub.a.run",
        references=(reference("contract", "pub.a.Request"),),
    )
    _activate(instance, record.install_id)

    assert service_uninstall_preconditions(instance, record.install_id).blocked is False


@pytest.mark.parametrize("path", [("failed",), ("failed", "rolling_back")])
def test_a_referencing_install_under_cleanup_still_blocks(
    instance: CruxibleInstance,
    path: tuple[str, ...],
) -> None:
    """A failed consumer still holds its claims, so its reference still counts.

    Its rollback has to be able to find the object it references; letting the
    provider be removed first is the same race the ownership hold prevents.
    """
    provider_id, consumer_id = _provider_and_consumer(instance)
    _activate(instance, provider_id)
    for phase in path:
        service_advance_install_phase(
            instance,
            consumer_id,
            to_phase=phase,  # type: ignore[arg-type]
            actor_context=actor(),
        )

    report = service_uninstall_preconditions(instance, provider_id)
    assert report.blocked is True
    assert report.blockers[0].referencing_install_phase == path[-1]


def test_a_rolled_back_referencing_install_stops_blocking(instance: CruxibleInstance) -> None:
    """A dependency held by an install that released its names is not a dependency."""
    provider_id, consumer_id = _provider_and_consumer(instance)
    _activate(instance, provider_id)
    for phase in ("failed", "rolling_back", "rolled_back"):
        service_advance_install_phase(
            instance,
            consumer_id,
            to_phase=phase,  # type: ignore[arg-type]
            actor_context=actor(),
        )

    assert service_uninstall_preconditions(instance, provider_id).blocked is False


def test_a_preparing_referencing_install_still_blocks(instance: CruxibleInstance) -> None:
    """An install mid-preflight holds its claims; ignoring it would race."""
    provider_id, _ = _provider_and_consumer(instance)
    _activate(instance, provider_id)

    report = service_uninstall_preconditions(instance, provider_id)
    assert report.blocked is True
    assert report.blockers[0].referencing_install_phase == "preparing"


def test_a_reference_to_an_unowned_name_is_not_a_blocker(
    instance: CruxibleInstance,
) -> None:
    provider = create_install(instance, artifact_id="base", install_id="inst-base")
    _own(instance, provider.install_id, object_kind="contract", object_name="pub.base.Request")

    consumer = create_install(instance, artifact_id="triage", install_id="inst-triage")
    _own(
        instance,
        consumer.install_id,
        object_kind="procedure",
        object_name="pub.triage.run",
        references=(reference("named_query", "pub.somewhere.else"),),
    )
    _activate(instance, provider.install_id)
    _activate(instance, consumer.install_id)

    assert service_uninstall_preconditions(instance, provider.install_id).blocked is False


def test_multiple_referencing_installs_are_all_reported(
    instance: CruxibleInstance,
) -> None:
    provider = create_install(instance, artifact_id="base", install_id="inst-base")
    _own(instance, provider.install_id, object_kind="contract", object_name="pub.base.Request")
    for index in range(2):
        consumer = create_install(instance, artifact_id=f"c{index}", install_id=f"inst-c{index}")
        _own(
            instance,
            consumer.install_id,
            object_kind="procedure",
            object_name=f"pub.c{index}.run",
            references=(reference("contract", "pub.base.Request"),),
        )
        _activate(instance, consumer.install_id)
    _activate(instance, provider.install_id)

    report = service_uninstall_preconditions(instance, provider.install_id)
    assert sorted(blocker.referencing_install_id for blocker in report.blockers) == [
        "inst-c0",
        "inst-c1",
    ]


# ---------------------------------------------------------------------------
# Customized objects and the declared limit
# ---------------------------------------------------------------------------


def test_customized_objects_are_reported_without_blocking(
    instance: CruxibleInstance,
) -> None:
    record = create_install(instance)
    _own(
        instance,
        record.install_id,
        object_kind="named_query",
        object_name="pub.a.q",
        installed_digest=compute_install_object_digest({"mode": "collection"}),
    )
    service_record_object_customization(
        instance,
        record.install_id,
        object_kind="named_query",
        object_name="pub.a.q",
        current_digest=compute_install_object_digest({"mode": "traversal"}),
        actor_context=actor(),
    )
    _activate(instance, record.install_id)

    report = service_uninstall_preconditions(instance, record.install_id)
    # A customer edit is a WARNING, not a blocker: uninstalling is permitted,
    # but discarding their work silently is not.
    assert report.blocked is False
    assert [item.object_name for item in report.customized_objects] == ["pub.a.q"]


def test_every_report_declares_what_it_cannot_see(instance: CruxibleInstance) -> None:
    """`blocked=False` must never be readable as "safe to delete"."""
    record = create_install(instance)
    _activate(instance, record.install_id)

    report = service_uninstall_preconditions(instance, record.install_id)
    assert report.unobservable_reference_sources == list(UNOBSERVABLE_REFERENCE_SOURCES)
    assert report.unobservable_reference_sources


def test_blocked_reports_also_declare_the_limit(instance: CruxibleInstance) -> None:
    provider_id, consumer_id = _provider_and_consumer(instance)
    _activate(instance, provider_id)
    _activate(instance, consumer_id)

    report = service_uninstall_preconditions(instance, provider_id)
    assert report.blocked is True
    assert report.unobservable_reference_sources == list(UNOBSERVABLE_REFERENCE_SOURCES)


def test_preconditions_on_an_unknown_install_raise_not_found(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(InstallNotFoundError):
        service_uninstall_preconditions(instance, "inst-missing")
