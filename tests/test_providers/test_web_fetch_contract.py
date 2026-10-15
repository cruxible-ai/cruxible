"""Core owns the web.fetch contract: one frozen registration per definition.

Any package implementing web.fetch installs onto core's registration of the
definition it ships. The registrations are checked-in bytes; the bucket
vocabulary and proofs they still carry are inert, and nothing classifies runs.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_interfaces import (
    evaluate_provider_interface_law,
    parse_provider_interface,
    provider_external_interface_definition_digest,
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_core.providers.web_fetch import (
    WEB_FETCH_INTERFACE_DEFINITION,
    WEB_FETCH_INTERFACE_DIGEST,
    WEB_FETCH_INTERFACE_DIGESTS,
    WEB_FETCH_INTERFACE_V2_DIGEST,
    core_owned_interface_registration,
    web_fetch_interface_registration,
)
from tests.support.provider_checkout import provider_checkout_path

#: The definition file cruxible-provider-web 0.2.x ships, by its registration.json digest.
PACKAGE_DEFINITION_FILE_DIGEST = "149854808b63a69f2dd5687e3a59de3a64cb5e137d9f5409eee3ba4e21aeb124"
#: Core's registration artifact digests: accepted on live instances, pinned forever.
REGISTRATION_ARTIFACT_DIGESTS = {
    WEB_FETCH_INTERFACE_V2_DIGEST: (
        "sha256:f1a74eaf84d95b23e18070e55ca5f35901432869bab93c5202def745fcc98970"
    ),
    WEB_FETCH_INTERFACE_DIGEST: (
        "sha256:f6302734bbe87d342144d0444e136ca71e9cd899e12a5d6ea121ba4d97d9681c"
    ),
}


def test_core_owns_one_registration_per_web_fetch_definition() -> None:
    """v2 and v3 share the (inert) vocabulary, the four proofs and the classifier id."""

    v2 = web_fetch_interface_registration(WEB_FETCH_INTERFACE_V2_DIGEST)
    v3 = web_fetch_interface_registration()
    assert WEB_FETCH_INTERFACE_DIGESTS == {
        WEB_FETCH_INTERFACE_V2_DIGEST,
        WEB_FETCH_INTERFACE_DIGEST,
    }
    assert (v2.interface_digest, v3.interface_digest) == (
        WEB_FETCH_INTERFACE_V2_DIGEST,
        WEB_FETCH_INTERFACE_DIGEST,
    )
    assert v3.interface_digest == provider_external_interface_definition_digest(
        canonical_bytes(WEB_FETCH_INTERFACE_DEFINITION).hex(), domain="cruxible.interface.stub.v1"
    )
    assert v2.model_dump(
        exclude={"identity", "interface_bytes_hex", "interface_digest"}
    ) == v3.model_dump(exclude={"identity", "interface_bytes_hex", "interface_digest"})
    assert not hasattr(v3, "classifier_code")
    assert not hasattr(v3, "conformance_proofs")
    assert core_owned_interface_registration(WEB_FETCH_INTERFACE_DIGEST) == v3
    assert core_owned_interface_registration(WEB_FETCH_INTERFACE_V2_DIGEST) == v2
    assert core_owned_interface_registration("sha256:" + "0" * 64) is None
    for name, registration in (("v2", v2), ("v3", v3)):
        path = provider_interface_path(registration.identity.name)
        law = evaluate_provider_interface_law(registration, path=path, predecessor=None)
        assert law.verdict == "accepted", law.diagnostics
        content = files("cruxible_core.providers").joinpath(f"web-fetch-registration-{name}.json")
        legacy = parse_provider_interface(
            content.read_bytes(), path=provider_interface_path("web.fetch")
        )
        assert (
            provider_interface_digest(legacy).tagged
            == REGISTRATION_ARTIFACT_DIGESTS[registration.interface_digest]
        )
        assert render_provider_interface(legacy) == content.read_bytes()


def test_the_v3_definition_is_the_package_file_byte_for_byte() -> None:
    content = files("cruxible_core.providers").joinpath("web-fetch-interface-v3.json").read_bytes()
    assert hashlib.sha256(content).hexdigest() == PACKAGE_DEFINITION_FILE_DIGEST
    assert json.loads(content) == WEB_FETCH_INTERFACE_DEFINITION


def _package_root() -> Path:
    checkout = provider_checkout_path()
    if checkout is None:
        pytest.skip("set CRUXIBLE_PROVIDERS_CHECKOUT to compare with cruxible-provider-web")
    return checkout / "packages/cruxible-provider-web/src/cruxible_provider_web"


def test_the_reference_package_ships_cores_v3_definition() -> None:
    root = _package_root()
    assert (root / "contracts/web.fetch.json").read_bytes() == (
        files("cruxible_core.providers").joinpath("web-fetch-interface-v3.json").read_bytes()
    )
    assert (
        provider_external_interface_definition_digest(
            canonical_bytes(json.loads((root / "contracts/web.fetch.json").read_bytes())).hex(),
            domain="cruxible.interface.stub.v1",
        )
        == WEB_FETCH_INTERFACE_DIGEST
    )
