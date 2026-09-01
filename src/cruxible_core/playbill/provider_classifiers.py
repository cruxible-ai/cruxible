"""Daemon-owned, digest-keyed Provider bucket classifier installation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    ProviderBucketClassifierInstallationResultV1,
    ProviderBucketClassifierInstallationV1,
    ProviderBucketConformanceFixtureProofV1,
    ProviderBucketConformanceFixtureV1,
    ProviderInterfaceRegistrationV1,
    provider_bucket_classifier_digest,
    provider_bucket_fixture_digest,
    provider_bucket_fixture_set_digest,
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


_CORE_DEMO_SIZE_FIXTURE_V1 = ProviderBucketConformanceFixtureV1(
    fixture_id="demo.small",
    canonical_input={"size": 3},
    measured_bucket_id="size=small",
)


@dataclass(frozen=True)
class _CoreDemoSizeClassifierV1:
    classifier_identity: str = "core.demo.size"
    classifier_version: int = 1
    classifier_digest: str = provider_bucket_classifier_digest(
        classifier_identity="core.demo.size",
        classifier_version=1,
        conformance_fixture_set_digest=provider_bucket_fixture_set_digest(
            (
                ProviderBucketConformanceFixtureProofV1(
                    selector="size=*",
                    fixture_id=_CORE_DEMO_SIZE_FIXTURE_V1.fixture_id,
                    fixture_digest=provider_bucket_fixture_digest(_CORE_DEMO_SIZE_FIXTURE_V1),
                    measured_bucket_id=_CORE_DEMO_SIZE_FIXTURE_V1.measured_bucket_id,
                ),
            )
        ),
    )

    def classify(self, canonical_input: CanonicalValue) -> str:
        if not isinstance(canonical_input, dict):
            raise ProviderClassifierInstallationRefused(
                "unclassified_input", "core.demo.size requires an object input"
            )
        size = canonical_input.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ProviderClassifierInstallationRefused(
                "unclassified_input", "core.demo.size requires a nonnegative integer size"
            )
        return "size=small" if size <= 3 else "size=large"


# This compiler-owned catalog is the oracle for accepted fixture proofs. Proposal
# content can cite it but cannot add a fixture or executable classifier.
CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1: Mapping[str, ProviderBucketConformanceFixtureV1] = {
    _CORE_DEMO_SIZE_FIXTURE_V1.fixture_id: _CORE_DEMO_SIZE_FIXTURE_V1
}
CORE_PROVIDER_BUCKET_CLASSIFIERS_V1: Mapping[str, ProviderBucketClassifierProtocol] = {
    _CoreDemoSizeClassifierV1().classifier_digest: _CoreDemoSizeClassifierV1(),
}


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

    def require_accepted(
        self,
        accepted: AcceptedProviderInterfaceRegistrationV1,
    ) -> ProviderBucketClassifierProtocol:
        """Install a core classifier only after re-proving this accepted registration."""

        digest = accepted.registration.classifier_digest
        try:
            return self.require(digest)
        except ProviderClassifierInstallationRefused:
            try:
                classifier = CORE_PROVIDER_BUCKET_CLASSIFIERS_V1[digest]
            except KeyError as exc:
                raise ProviderClassifierInstallationRefused(
                    "classifier_not_installed",
                    f"accepted classifier {digest} has no core implementation",
                ) from exc
            self.install(accepted, classifier)
            return self.require(digest)


# The runtime and discovery surface share this daemon-local installation registry.
# Accepted registrations remain governed; installed classifier code remains local.
PROVIDER_BUCKET_CLASSIFIER_REGISTRY = ProviderBucketClassifierRegistry()


__all__ = [
    "CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1",
    "CORE_PROVIDER_BUCKET_CLASSIFIERS_V1",
    "ProviderBucketClassifierProtocol",
    "ProviderBucketClassifierRegistry",
    "PROVIDER_BUCKET_CLASSIFIER_REGISTRY",
    "ProviderClassifierInstallationRefused",
    "core_provider_bucket_conformance_fixtures",
]
