"""Governed Provider interface/vocabulary/classifier acceptance laws."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    ProviderInterfaceRegistrationV1,
    evaluate_provider_interface_law,
    parse_provider_interface,
    provider_bucket_classifier_digest,
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from tests.test_playbill._p2b1_support import (
    interface_fixture,
    interface_registration,
)


def test_provider_interface_round_trip_and_subordinate_digest_correspondence() -> None:
    registration = interface_registration()
    content = render_provider_interface(registration)

    assert (
        parse_provider_interface(
            content,
            path=provider_interface_path(registration.interface_id),
        )
        == registration
    )
    assert provider_interface_digest(registration).tagged == (
        "sha256:d0d89c02d6095a2a2e5231418070fecd5277eaac369dbe079a771a251eeb960b"
    )


def test_provider_interface_refuses_byte_and_classifier_digest_mismatch() -> None:
    registration = interface_registration()
    payload = registration.model_dump(mode="json")
    payload["interface_digest"] = f"sha256:{'0' * 64}"
    with pytest.raises(ValidationError, match="interface digest does not reproduce"):
        ProviderInterfaceRegistrationV1.model_validate(payload)

    payload = registration.model_dump(mode="json")
    payload["classifier_digest"] = f"sha256:{'0' * 64}"
    with pytest.raises(ValidationError, match="classifier digest does not reproduce"):
        ProviderInterfaceRegistrationV1.model_validate(payload)


def test_classifier_digest_moves_only_with_its_frozen_preimage() -> None:
    registration = interface_registration()
    original = registration.classifier_digest

    assert original != provider_bucket_classifier_digest(
        classifier_identity=registration.classifier_identity,
        classifier_version=2,
        conformance_fixture_set_digest=registration.conformance_fixture_set_digest,
    )
    assert original == provider_bucket_classifier_digest(
        classifier_identity=registration.classifier_identity,
        classifier_version=registration.classifier_version,
        conformance_fixture_set_digest=registration.conformance_fixture_set_digest,
    )


def test_interface_law_reproduces_fixture_catalog_and_exact_succession() -> None:
    registration = interface_registration()
    fixture = interface_fixture()
    path = provider_interface_path(registration.interface_id)
    assert (
        evaluate_provider_interface_law(
            registration,
            path=path,
            predecessor=None,
            conformance_fixtures={fixture.fixture_id: fixture},
        ).verdict
        == "accepted"
    )

    accepted = AcceptedProviderInterfaceRegistrationV1(
        path=path,
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )
    successor = registration.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=accepted.artifact_digest),
        }
    )
    assert (
        evaluate_provider_interface_law(
            successor,
            path=path,
            predecessor=accepted,
            conformance_fixtures={fixture.fixture_id: fixture},
        ).verdict
        == "accepted"
    )

    missing = evaluate_provider_interface_law(
        successor,
        path=path,
        predecessor=accepted,
        conformance_fixtures={},
    )
    assert missing.diagnostics[0].code == "playbill.provider_interface.bucket_fixture_missing"
