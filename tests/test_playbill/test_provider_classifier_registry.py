"""Provider classifier installation is digest-keyed and fully re-proven."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_core.playbill.provider_classifiers import (
    ProviderBucketClassifierRegistry,
    ProviderClassifierInstallationRefused,
    core_provider_bucket_conformance_fixtures,
)
from cruxible_core.playbill.seed_artifacts.workspace_file import WORKSPACE_FILE_FIXTURES
from tests.test_playbill._p2b1_support import (
    accepted_interface,
    interface_fixture,
    interface_registration,
)


@dataclass(frozen=True)
class _Classifier:
    classifier_identity: str
    classifier_version: int
    classifier_digest: str
    result: str

    def classify(self, canonical_input: CanonicalValue) -> str:
        assert canonical_input == {"size": 3}
        return self.result


def _classifier(*, result: str = "size=small") -> _Classifier:
    registration = interface_registration()
    return _Classifier(
        classifier_identity=registration.classifier_identity,
        classifier_version=registration.classifier_version,
        classifier_digest=registration.classifier_digest,
        result=result,
    )


def test_classifier_install_reexecutes_every_accepted_fixture_before_publication() -> None:
    accepted = accepted_interface()
    registration = accepted.registration
    fixture = interface_fixture()
    registry = ProviderBucketClassifierRegistry(conformance_fixtures={fixture.fixture_id: fixture})

    installation = registry.install(accepted, _classifier())

    assert installation.classifier_digest == registration.classifier_digest
    assert (
        installation.results[0].fixture_digest == registration.conformance_proofs[0].fixture_digest
    )
    assert registry.require(registration.classifier_digest).classify({"size": 3}) == "size=small"


@pytest.mark.parametrize("result", ["size=large", "size=unknown"])
def test_classifier_install_is_atomic_on_wrong_or_invalid_output(result: str) -> None:
    accepted = accepted_interface()
    registration = accepted.registration
    fixture = interface_fixture()
    registry = ProviderBucketClassifierRegistry(conformance_fixtures={fixture.fixture_id: fixture})

    with pytest.raises(ProviderClassifierInstallationRefused) as caught:
        registry.install(accepted, _classifier(result=result))
    assert caught.value.code == "classifier_digest_mismatch"

    with pytest.raises(ProviderClassifierInstallationRefused) as absent:
        registry.require(registration.classifier_digest)
    assert absent.value.code == "classifier_not_installed"


def test_classifier_install_refuses_partial_reproof_and_identity_mismatch() -> None:
    accepted = accepted_interface()
    registration = accepted.registration
    empty = ProviderBucketClassifierRegistry(conformance_fixtures={})
    with pytest.raises(ProviderClassifierInstallationRefused) as partial:
        empty.install(accepted, _classifier())
    assert partial.value.code == "classifier_not_installed"

    fixture = interface_fixture()
    registry = ProviderBucketClassifierRegistry(conformance_fixtures={fixture.fixture_id: fixture})
    mismatched = _classifier().__class__(
        classifier_identity="core.other",
        classifier_version=registration.classifier_version,
        classifier_digest=registration.classifier_digest,
        result="size=small",
    )
    with pytest.raises(ProviderClassifierInstallationRefused) as identity:
        registry.install(accepted, mismatched)
    assert identity.value.code == "classifier_digest_mismatch"


def test_classifier_install_order_does_not_change_digest_keyed_availability() -> None:
    accepted = accepted_interface()
    registration = accepted.registration
    fixture = interface_fixture()
    first = ProviderBucketClassifierRegistry(conformance_fixtures={fixture.fixture_id: fixture})
    second = ProviderBucketClassifierRegistry(conformance_fixtures={fixture.fixture_id: fixture})

    first.install(accepted, _classifier())
    second.install(accepted, _classifier())
    first.install(accepted, _classifier())

    assert (
        first.installed_classifier_digests
        == second.installed_classifier_digests
        == {registration.classifier_digest}
    )
    assert first.installation(registration.classifier_digest) == second.installation(
        registration.classifier_digest
    )


def test_production_catalog_carries_fixtures_but_no_demo_executable() -> None:
    accepted = accepted_interface()
    registry = ProviderBucketClassifierRegistry()

    assert set(core_provider_bucket_conformance_fixtures()) == {
        "demo.small",
        *(fixture.fixture_id for fixture in WORKSPACE_FILE_FIXTURES),
    }
    with pytest.raises(ProviderClassifierInstallationRefused) as absent:
        registry.require_accepted(accepted)
    assert absent.value.code == "classifier_not_installed"
    assert registry.installed_classifier_digests == frozenset()


def test_accepted_registration_cannot_select_unshipped_classifier_code() -> None:
    accepted = accepted_interface()
    changed = accepted.model_copy(
        update={
            "registration": accepted.registration.model_copy(
                update={"classifier_digest": f"sha256:{'f' * 64}"}
            )
        }
    )
    registry = ProviderBucketClassifierRegistry()

    with pytest.raises(ProviderClassifierInstallationRefused) as caught:
        registry.require_accepted(changed)
    assert caught.value.code == "classifier_not_installed"
    assert registry.installed_classifier_digests == frozenset()
