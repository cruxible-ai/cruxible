"""The pilot's test providers are valid registrations under the wave 0 models.

Not a scenario: no transcript. Every :mod:`fixtures.providers` package is built
from the wave 0 example artifacts (``examples/base``, standing in for the base
kit until it ships) and must parse as ``PackageRegistration`` (schema 3) and
pass the registration law with no problems. Where the fixtures and the models
disagree, the models win and the fixtures change.

The two packages scenario 7 contrasts are checked against the base kind's
layout too: ``pilot-results-empty`` carries an exact copy (it covers, by the
equal-declaration fast path), ``pilot-results-thin`` carries none (the first
gap is the missing layout, before any type is compared).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest

from tests.pilot_contract_batch.fixtures import providers

BASE = providers.BaseKit.examples()

PACKAGES: dict[str, Callable[[], providers.PilotPackage]] = {
    "pilot-echo": providers.echo_package,
    "pilot-search": lambda: providers.search_package(BASE),
    "pilot-results": lambda: providers.results_package(BASE),
    "pilot-results-empty": lambda: providers.empty_results_package(BASE),
    "pilot-results-wide": lambda: providers.wide_results_package(BASE),
    "pilot-fetch-only": lambda: providers.fetch_only_package(BASE),
    "pilot-results-narrow": lambda: providers.narrow_request_package(BASE),
    "pilot-results-untyped": lambda: providers.untyped_result_package(BASE),
    "pilot-results-thin": lambda: providers.thin_layout_package(BASE),
    "pilot-other-000": lambda: providers.other_capability_packages(1)[0],
}


@pytest.mark.parametrize("name", sorted(PACKAGES))
def test_every_test_provider_is_a_valid_schema_3_registration(name: str) -> None:
    from cruxible_client.contracts.provider_registration import (
        PackageRegistration,
        evaluate_registration_law,
    )

    package = PACKAGES[name]()
    registration = PackageRegistration.model_validate(package.registration)

    assert package.provider_id == name
    assert evaluate_registration_law(registration) == ()
    assert registration.manifest.path == providers.MANIFEST_PATH
    digest = "sha256:" + hashlib.sha256(package.manifest_bytes).hexdigest()
    assert registration.manifest.digest == digest


def test_fifty_other_providers_are_valid_and_distinct() -> None:
    from cruxible_client.contracts.provider_registration import (
        PackageRegistration,
        evaluate_registration_law,
    )

    packages = providers.other_capability_packages(50)

    assert len({package.provider_id for package in packages}) == 50
    for package in packages:
        registration = PackageRegistration.model_validate(package.registration)
        assert evaluate_registration_law(registration) == ()


def _never(*_args: object) -> object:
    raise AssertionError("coverage compared a type; it should have stopped before")


def test_the_empty_derive_covers_the_base_kind_and_the_thin_one_does_not() -> None:
    from cruxible_client.contracts.capture_layouts import (
        ABSENT,
        DeclaredCaptureKind,
        LayoutGap,
        layout_covers,
    )
    from cruxible_client.contracts.provider_registration import PackageRegistration

    reader = DeclaredCaptureKind.model_validate(BASE.capture_kind(providers.RESULTS_KIND))

    def carried(package: providers.PilotPackage) -> DeclaredCaptureKind:
        registration = PackageRegistration.model_validate(package.registration)
        found = registration.declares.capture_kind(providers.RESULTS_KIND)
        assert found is not None
        return found

    empty = carried(providers.empty_results_package(BASE))
    thin = carried(providers.thin_layout_package(BASE))

    assert empty == reader
    assert layout_covers(empty, reader, type_fits=_never, row_fits=_never) is None  # type: ignore[arg-type]
    assert thin.layout is None
    assert layout_covers(thin, reader, type_fits=_never, row_fits=_never) == LayoutGap(  # type: ignore[arg-type]
        entry="layout", producer=ABSENT, reader="record sets results"
    )
