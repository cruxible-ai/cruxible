"""Customized-object detection: the digest comparison an update must honour."""

from __future__ import annotations

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, InstallNotFoundError
from cruxible_core.installs.types import compute_install_object_digest
from cruxible_core.service import (
    service_detect_object_customization,
    service_get_install,
    service_objects_owned_by_install,
    service_record_object_customization,
    service_record_owned_object,
)
from tests.test_installs.conftest import actor, create_install, load_receipt

INSTALLED_QUERY = {
    "mode": "collection",
    "returns": "Vulnerability",
    "where": {"result.properties.status": {"eq": "open"}},
}
EDITED_QUERY = {
    "mode": "collection",
    "returns": "Vulnerability",
    "where": {"result.properties.status": {"eq": "triaged"}},
}


def _install_with_query(instance: CruxibleInstance) -> tuple[str, str]:
    record = create_install(instance)
    digest = compute_install_object_digest(INSTALLED_QUERY)
    service_record_owned_object(
        instance,
        record.install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        installed_digest=digest,
        actor_context=actor(),
    )
    return record.install_id, digest


# ---------------------------------------------------------------------------
# The digest helper
# ---------------------------------------------------------------------------


def test_digest_is_stable_across_key_order() -> None:
    """Canonical JSON: an object is its content, not its serialization order."""
    reordered = {key: INSTALLED_QUERY[key] for key in reversed(list(INSTALLED_QUERY))}
    assert compute_install_object_digest(reordered) == compute_install_object_digest(
        INSTALLED_QUERY
    )


def test_digest_changes_with_content() -> None:
    assert compute_install_object_digest(INSTALLED_QUERY) != compute_install_object_digest(
        EDITED_QUERY
    )


def test_digest_is_prefixed() -> None:
    assert compute_install_object_digest(INSTALLED_QUERY).startswith("sha256:")


# ---------------------------------------------------------------------------
# Detection (pure read)
# ---------------------------------------------------------------------------


def test_matching_digest_is_not_customized(instance: CruxibleInstance) -> None:
    install_id, digest = _install_with_query(instance)

    report = service_detect_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=digest,
    )
    assert report.customized is False
    assert report.installed_digest == digest
    assert report.current_digest == digest


def test_differing_digest_is_customized(instance: CruxibleInstance) -> None:
    install_id, digest = _install_with_query(instance)
    edited = compute_install_object_digest(EDITED_QUERY)

    report = service_detect_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=edited,
    )
    assert report.customized is True
    assert report.installed_digest == digest
    assert report.current_digest == edited


def test_detection_persists_nothing(instance: CruxibleInstance) -> None:
    """The read-only hook must not silently mutate the ledger."""
    install_id, _ = _install_with_query(instance)
    service_detect_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=compute_install_object_digest(EDITED_QUERY),
    )

    owned = service_objects_owned_by_install(instance, install_id)[0]
    assert owned.customized is False
    assert owned.current_digest is None


def test_detection_on_an_unowned_object_is_refused(instance: CruxibleInstance) -> None:
    install_id, _ = _install_with_query(instance)

    with pytest.raises(ConfigError) as excinfo:
        service_detect_object_customization(
            instance,
            install_id,
            object_kind="contract",
            object_name="pub.kev.NotMine",
            current_digest="sha256:whatever",
        )

    assert "does not own" in str(excinfo.value)


def test_detection_on_an_unknown_install_raises_not_found(
    instance: CruxibleInstance,
) -> None:
    with pytest.raises(InstallNotFoundError):
        service_detect_object_customization(
            instance,
            "inst-missing",
            object_kind="named_query",
            object_name="pub.kev.queue",
            current_digest="sha256:x",
        )


# ---------------------------------------------------------------------------
# Recording the verdict
# ---------------------------------------------------------------------------


def test_recording_a_customization_persists_the_flag_and_digest(
    instance: CruxibleInstance,
) -> None:
    install_id, _ = _install_with_query(instance)
    edited = compute_install_object_digest(EDITED_QUERY)

    report = service_record_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=edited,
        actor_context=actor(),
    )

    assert report.customized is True
    owned = service_objects_owned_by_install(instance, install_id)[0]
    assert owned.customized is True
    assert owned.current_digest == edited


def test_recording_an_unchanged_object_clears_the_flag(
    instance: CruxibleInstance,
) -> None:
    """A customization reverted by the customer must stop being reported."""
    install_id, digest = _install_with_query(instance)
    service_record_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=compute_install_object_digest(EDITED_QUERY),
        actor_context=actor(),
    )

    report = service_record_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=digest,
        actor_context=actor(),
    )

    assert report.customized is False
    assert service_objects_owned_by_install(instance, install_id)[0].customized is False


def test_recording_a_customization_is_receipted(instance: CruxibleInstance) -> None:
    install_id, _ = _install_with_query(instance)
    report = service_record_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=compute_install_object_digest(EDITED_QUERY),
        actor_context=actor(),
    )

    receipt = load_receipt(instance, report.receipt_id)
    assert receipt.operation_type == "install_object_customization"
    assert receipt.committed is True


def test_recording_on_an_unowned_object_is_refused_and_receipted(
    instance: CruxibleInstance,
) -> None:
    install_id, _ = _install_with_query(instance)

    with pytest.raises(ConfigError) as excinfo:
        service_record_object_customization(
            instance,
            install_id,
            object_kind="enum",
            object_name="pub.kev.Severity",
            current_digest="sha256:x",
            actor_context=actor(),
        )

    receipt = load_receipt(instance, excinfo.value.mutation_receipt_id)
    assert receipt.committed is False


def test_customization_does_not_disturb_the_phase(instance: CruxibleInstance) -> None:
    install_id, _ = _install_with_query(instance)
    service_record_object_customization(
        instance,
        install_id,
        object_kind="named_query",
        object_name="pub.kev.queue",
        current_digest=compute_install_object_digest(EDITED_QUERY),
        actor_context=actor(),
    )

    assert service_get_install(instance, install_id).install.phase == "preparing"
