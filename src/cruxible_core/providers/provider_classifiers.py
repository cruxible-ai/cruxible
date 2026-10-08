"""Daemon-owned, digest-keyed Provider bucket classifier installation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderBucketClassifierInstallation,
    ProviderBucketClassifierInstallationResult,
    ProviderBucketConformanceFixture,
    ProviderInterfaceRegistration,
    ProviderInterfaceRegistrationV1,
    provider_bucket_fixture_digest,
)
from cruxible_client.contracts.providers import ProviderImplementationRecord, ProviderV2
from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_DIGESTS
from cruxible_core.governance.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_FIXTURES,
    WorkspaceFileBucketClassifier,
    workspace_file_accepted_registration,
)
from cruxible_core.providers.web_fetch import (
    WEB_FETCH_FIXTURES,
    WEB_FETCH_INTERFACE_DIGEST,
    WebFetchBucketClassifier,
)

if TYPE_CHECKING:
    from cruxible_core.providers.provider_local_runtime import ProviderSpawnDeadline


class ProviderBucketClassifierProtocol(Protocol):
    """Installed code whose identity must reproduce accepted registration bytes."""

    @property
    def classifier_identity(self) -> str: ...

    @property
    def classifier_version(self) -> int: ...

    @property
    def classifier_digest(self) -> str: ...

    def classify(
        self, canonical_input: CanonicalValue, *, deadline: ProviderSpawnDeadline | None
    ) -> str:
        """Measure the input's bucket.

        ``deadline`` is required so no caller can drop it by omission: a Procedure
        run passes its own, so a classifier that spawns a child is held to the run's
        time; only installation conformance, outside any run, passes None.
        """
        ...


class ProviderClassifierInstallationRefused(ExecutionError):
    """An installed classifier failed identity or fixture re-proof."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


_CORE_DEMO_SIZE_FIXTURE_V1 = ProviderBucketConformanceFixture(
    fixture_id="demo.small",
    canonical_input={"size": 3},
    measured_bucket_id="size=small",
)


# This compiler-owned catalog is the oracle for accepted fixture proofs. Proposal
# content can cite it but cannot add a fixture. Executable classifiers are installed
# by the daemon operator and are never shipped as product-domain demo machinery.
CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1: Mapping[str, ProviderBucketConformanceFixture] = {
    fixture.fixture_id: fixture
    for fixture in (_CORE_DEMO_SIZE_FIXTURE_V1, *WORKSPACE_FILE_FIXTURES, *WEB_FETCH_FIXTURES)
}


def core_provider_bucket_conformance_fixtures() -> Mapping[str, ProviderBucketConformanceFixture]:
    """Return the compiler-owned fixture catalog used by acceptance and install."""

    return CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1


class ProviderBucketClassifierRegistry:
    """Publish classifiers only after exact accepted-fixture re-execution."""

    def __init__(
        self,
        *,
        conformance_fixtures: Mapping[str, ProviderBucketConformanceFixture] | None = None,
    ) -> None:
        self._fixtures = dict(
            core_provider_bucket_conformance_fixtures()
            if conformance_fixtures is None
            else conformance_fixtures
        )
        self._classifiers: dict[str, ProviderBucketClassifierProtocol] = {}
        self._installations: dict[str, ProviderBucketClassifierInstallation] = {}

    @property
    def installed_classifier_digests(self) -> frozenset[str]:
        return frozenset(self._classifiers)

    def install(
        self,
        accepted: AcceptedProviderInterfaceRegistration,
        classifier: ProviderBucketClassifierProtocol,
    ) -> ProviderBucketClassifierInstallation:
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

        results: list[ProviderBucketClassifierInstallationResult] = []
        fixtures = (
            {item.fixture_id: item for item in registration.conformance_fixtures}
            if isinstance(registration, ProviderInterfaceRegistration)
            else self._fixtures
        )
        for proof in registration.conformance_proofs:
            fixture = fixtures.get(proof.fixture_id)
            if fixture is None or provider_bucket_fixture_digest(fixture) != proof.fixture_digest:
                raise ProviderClassifierInstallationRefused(
                    "classifier_not_installed",
                    f"accepted fixture {proof.fixture_id!r} is unavailable at this compiler",
                )
            # Installation conformance runs outside any Procedure run.
            measured = classifier.classify(
                fixture.canonical_input,  # type: ignore[arg-type]
                deadline=None,
            )
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
                ProviderBucketClassifierInstallationResult(
                    fixture_id=proof.fixture_id,
                    fixture_digest=proof.fixture_digest,
                    measured_bucket_id=measured,
                )
            )

        installation = ProviderBucketClassifierInstallation(
            classifier_identity=registration.classifier_identity,
            classifier_version=registration.classifier_version,
            classifier_digest=registration.classifier_digest,
            conformance_fixture_set_digest=registration.conformance_fixture_set_digest,
            results=tuple(sorted(results, key=lambda item: item.fixture_id.encode())),
        )
        self._classifiers[registration.classifier_digest] = classifier
        self._installations[registration.classifier_digest] = installation
        return installation

    def restore(
        self,
        accepted: AcceptedProviderInterfaceRegistration,
        classifier: ProviderBucketClassifierProtocol,
        installation: ProviderBucketClassifierInstallation,
    ) -> None:
        """Reload a daemon-owned installation proof without re-executing fixtures."""
        registration = accepted.registration
        if (
            not isinstance(registration, ProviderInterfaceRegistration)
            or installation.classifier_digest != registration.classifier_digest
            or installation.classifier_identity != registration.classifier_identity
            or installation.classifier_version != registration.classifier_version
            or installation.conformance_fixture_set_digest
            != registration.conformance_fixture_set_digest
            or classifier.classifier_digest != registration.classifier_digest
            or classifier.classifier_identity != registration.classifier_identity
            or classifier.classifier_version != registration.classifier_version
            or tuple(
                (item.fixture_id, item.fixture_digest, item.measured_bucket_id)
                for item in installation.results
            )
            != tuple(
                sorted(
                    (item.fixture_id, item.fixture_digest, item.measured_bucket_id)
                    for item in registration.conformance_proofs
                )
            )
        ):
            raise ProviderClassifierInstallationRefused(
                "classifier_digest_mismatch", "retained installation differs from accepted fixtures"
            )
        self._classifiers[registration.classifier_digest] = classifier
        self._installations[registration.classifier_digest] = installation

    def require(self, classifier_digest: str) -> ProviderBucketClassifierProtocol:
        try:
            return self._classifiers[classifier_digest]
        except KeyError as exc:
            raise ProviderClassifierInstallationRefused(
                "classifier_not_installed",
                f"accepted classifier {classifier_digest} is unavailable or not fully re-proven",
            ) from exc

    def installation(self, classifier_digest: str) -> ProviderBucketClassifierInstallation:
        self.require(classifier_digest)
        return self._installations[classifier_digest]


# The runtime and discovery surface share this daemon-local installation registry.
# Accepted registrations remain governed; installed classifier code remains local.
PROVIDER_BUCKET_CLASSIFIER_REGISTRY = ProviderBucketClassifierRegistry()


def install_compiler_owned_provider_classifier(
    accepted: AcceptedProviderInterfaceRegistration,
) -> ProviderBucketClassifierInstallation | None:
    """Install the compiler-owned double for an interface that has one."""

    if accepted.registration.interface_digest == WEB_FETCH_INTERFACE_DIGEST:
        return PROVIDER_BUCKET_CLASSIFIER_REGISTRY.install(accepted, WebFetchBucketClassifier())
    # Both workspace.file revisions read the same host-owned bytes; a package
    # registration carries its own classifier code instead.
    if accepted.registration.interface_digest not in WORKSPACE_FILE_INTERFACE_DIGESTS or isinstance(
        accepted.registration, ProviderInterfaceRegistration
    ):
        return None
    return PROVIDER_BUCKET_CLASSIFIER_REGISTRY.install(
        accepted,
        WorkspaceFileBucketClassifier(),
    )


def admitted_bucket_selectors(
    provider: ProviderV2,
    implementation: ProviderImplementationRecord,
    registration: ProviderInterfaceRegistrationV1,
) -> tuple[str, ...]:
    """The input buckets one bound implementation admits: what it claims, as proven.

    A registration proves the buckets of every implementation bound onto it, so
    its proof menu can be wider than what this implementation declared; an input
    in a bucket the implementation did not claim is refused before it runs, as
    the provider runtime refuses it.
    """

    declared = next(
        (
            item
            for item in provider.runtime_artifact.manifest.implementations
            if item.interface_id == implementation.interface_id
            and item.interface_digest == implementation.interface_digest
            and item.entrypoint == implementation.entrypoint
        ),
        None,
    )
    proven = {proof.selector for proof in registration.conformance_proofs}
    claimed = (
        ()
        if declared is None
        else tuple(
            sorted(
                (item for item in set(declared.declared_input_buckets) if item in proven),
                key=str.encode,
            )
        )
    )
    if not claimed:
        raise ExecutionError(
            f"accepted Provider implementation {implementation.implementation_digest} claims "
            "no input bucket its interface registration proves"
        )
    return claimed


# The built-in workspace.file registration is the same compiler-owned bytes in
# every instance that seeds it, so its classifier is re-proven once here rather
# than waiting for an invoker: a degraded Provider lane cannot leave the
# in-process built-in without its classifier.
install_compiler_owned_provider_classifier(workspace_file_accepted_registration())


__all__ = [
    "CORE_PROVIDER_BUCKET_CONFORMANCE_FIXTURES_V1",
    "ProviderBucketClassifierProtocol",
    "ProviderBucketClassifierRegistry",
    "PROVIDER_BUCKET_CLASSIFIER_REGISTRY",
    "ProviderClassifierInstallationRefused",
    "admitted_bucket_selectors",
    "core_provider_bucket_conformance_fixtures",
    "install_compiler_owned_provider_classifier",
]
