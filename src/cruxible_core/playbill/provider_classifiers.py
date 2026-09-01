"""Daemon-owned, digest-keyed Provider bucket classifier installation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    ProviderBucketClassifierInstallationResultV1,
    ProviderBucketClassifierInstallationV1,
    ProviderBucketConformanceFixtureV1,
    ProviderInterfaceRegistrationV1,
    provider_bucket_fixture_digest,
)


class ProviderBucketClassifierProtocol(Protocol):
    """Installed code whose identity must reproduce accepted registration bytes."""

    @property
    def classifier_identity(self) -> str: ...

    @property
    def classifier_version(self) -> int: ...

    @property
    def classifier_digest(self) -> str: ...

    def classify(self, canonical_input: CanonicalValue) -> str: ...


class ProviderClassifierInstallationRefused(PlaybillExecutionError):
    """An installed classifier failed identity or fixture re-proof."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# Provider interfaces are compiler extensions. Each release adds its exact fixture
# bytes here before an accepted registration using them can pass its law. An empty
# initial catalog is deliberate: proposal content cannot mint its own test oracle.
CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1: Mapping[str, ProviderBucketConformanceFixtureV1] = {}


def core_provider_bucket_conformance_fixtures() -> Mapping[str, ProviderBucketConformanceFixtureV1]:
    """Return the compiler-owned fixture catalog used by acceptance and install."""

    return CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1


class ProviderBucketClassifierRegistry:
    """Publish classifiers only after exact accepted-fixture re-execution."""

    def __init__(
        self,
        *,
        conformance_fixtures: Mapping[str, ProviderBucketConformanceFixtureV1] | None = None,
    ) -> None:
        self._fixtures = dict(
            core_provider_bucket_conformance_fixtures()
            if conformance_fixtures is None
            else conformance_fixtures
        )
        self._classifiers: dict[str, ProviderBucketClassifierProtocol] = {}
        self._installations: dict[str, ProviderBucketClassifierInstallationV1] = {}

    @property
    def installed_classifier_digests(self) -> frozenset[str]:
        return frozenset(self._classifiers)

    def install(
        self,
        accepted: AcceptedProviderInterfaceRegistrationV1,
        classifier: ProviderBucketClassifierProtocol,
    ) -> ProviderBucketClassifierInstallationV1:
        """Re-prove every accepted fixture before publishing one digest."""

        registration: ProviderInterfaceRegistrationV1 = accepted.registration
        if (
            classifier.classifier_identity != registration.classifier_identity
            or classifier.classifier_version != registration.classifier_version
            or classifier.classifier_digest != registration.classifier_digest
        ):
            raise ProviderClassifierInstallationRefused(
                "classifier_digest_mismatch",
                "installed classifier identity, version, or digest differs from registration",
            )

        results: list[ProviderBucketClassifierInstallationResultV1] = []
        for proof in registration.conformance_proofs:
            fixture = self._fixtures.get(proof.fixture_id)
            if fixture is None or provider_bucket_fixture_digest(fixture) != proof.fixture_digest:
                raise ProviderClassifierInstallationRefused(
                    "classifier_not_installed",
                    f"accepted fixture {proof.fixture_id!r} is unavailable at this compiler",
                )
            measured = classifier.classify(fixture.canonical_input)  # type: ignore[arg-type]
            try:
                registration.vocabulary.validate_bucket(measured)
            except ValueError as exc:
                raise ProviderClassifierInstallationRefused(
                    "classifier_digest_mismatch",
                    f"classifier returned an invalid bucket for fixture {proof.fixture_id!r}",
                ) from exc
            if measured != proof.measured_bucket_id:
                raise ProviderClassifierInstallationRefused(
                    "classifier_digest_mismatch",
                    f"classifier failed fixture {proof.fixture_id!r}",
                )
            results.append(
                ProviderBucketClassifierInstallationResultV1(
                    fixture_id=proof.fixture_id,
                    fixture_digest=proof.fixture_digest,
                    measured_bucket_id=measured,
                )
            )

        installation = ProviderBucketClassifierInstallationV1(
            classifier_identity=registration.classifier_identity,
            classifier_version=registration.classifier_version,
            classifier_digest=registration.classifier_digest,
            conformance_fixture_set_digest=registration.conformance_fixture_set_digest,
            results=tuple(results),
        )
        self._classifiers[registration.classifier_digest] = classifier
        self._installations[registration.classifier_digest] = installation
        return installation

    def require(self, classifier_digest: str) -> ProviderBucketClassifierProtocol:
        try:
            return self._classifiers[classifier_digest]
        except KeyError as exc:
            raise ProviderClassifierInstallationRefused(
                "classifier_not_installed",
                f"accepted classifier {classifier_digest} is unavailable or not fully re-proven",
            ) from exc

    def installation(self, classifier_digest: str) -> ProviderBucketClassifierInstallationV1:
        self.require(classifier_digest)
        return self._installations[classifier_digest]


__all__ = [
    "CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1",
    "ProviderBucketClassifierProtocol",
    "ProviderBucketClassifierRegistry",
    "ProviderClassifierInstallationRefused",
    "core_provider_bucket_conformance_fixtures",
]
