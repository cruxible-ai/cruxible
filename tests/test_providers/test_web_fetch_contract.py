"""Core owns the web.fetch contract: one registration per definition, one classifier.

Any package implementing web.fetch installs onto core's registration, so core's
classifier decides every run's bucket. The parity golden below pins that the
classifier measures exactly what the reference package's own classifier does,
over the vocabulary and fixtures the package ships.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    evaluate_provider_interface_law,
    provider_external_interface_definition_digest,
    provider_interface_digest,
    provider_interface_path,
)
from cruxible_core.providers.provider_classifiers import (
    ProviderBucketClassifierRegistry,
    core_provider_bucket_conformance_fixtures,
)
from cruxible_core.providers.web_fetch import (
    WEB_FETCH_FIXTURES,
    WEB_FETCH_INTERFACE_DEFINITION,
    WEB_FETCH_INTERFACE_DIGEST,
    WEB_FETCH_INTERFACE_DIGESTS,
    WEB_FETCH_INTERFACE_V2_DIGEST,
    WEB_FETCH_VOCABULARY,
    WebFetchBucketClassifier,
    classify_web_fetch,
    core_owned_interface_registration,
    web_fetch_interface_registration,
)
from tests.support.provider_checkout import provider_checkout_path

#: The definition file cruxible-provider-web 0.2.x ships, by its registration.json digest.
PACKAGE_DEFINITION_FILE_DIGEST = "149854808b63a69f2dd5687e3a59de3a64cb5e137d9f5409eee3ba4e21aeb124"

#: Input -> the bucket the web.fetch classifier measures (None: unclassifiable).
PARITY_GOLDEN: tuple[tuple[dict[str, Any], str | None], ...] = (
    (
        {"url": "https://example.org/articles/tide"},
        "source_kind=static_html;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/dashboard", "render": True},
        "source_kind=js_rendered;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/report.pdf", "render": True},
        "source_kind=js_rendered;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/report.PDF"},
        "source_kind=binary;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/blob", "expected_format": "bytes"},
        "source_kind=binary;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/feed.xml"},
        "source_kind=api_json;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/api/v1/items"},
        "source_kind=api_json;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/table", "expected_format": "csv"},
        "source_kind=api_json;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/page.v2/index", "expected_format": "html"},
        "source_kind=static_html;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/x", "credential_ref": "secret://k", "paced": True},
        "source_kind=static_html;access=authenticated;page_weight=light",
    ),
    (
        {"url": "https://example.org/x", "paced": True},
        "source_kind=static_html;access=rate_limited;page_weight=light",
    ),
    (
        {"url": "https://example.org/x", "max_bytes": 262144},
        "source_kind=static_html;access=public;page_weight=light",
    ),
    (
        {"url": "https://example.org/x", "max_bytes": 262145},
        "source_kind=static_html;access=public;page_weight=medium",
    ),
    (
        {"url": "https://example.org/x", "max_bytes": 2097152},
        "source_kind=static_html;access=public;page_weight=medium",
    ),
    (
        {"url": "https://example.org/x.json", "max_bytes": 33554432},
        "source_kind=api_json;access=public;page_weight=heavy",
    ),
    ({"url": ""}, None),
    ({"url": "https://example.org/x", "expected_format": "pdf"}, None),
    ({"url": "https://example.org/x", "max_bytes": 0}, None),
    ({"url": "https://example.org/x", "max_bytes": True}, None),
    ({"url": "https://example.org/x", "max_bytes": 33554433}, None),
    ({"render": True}, None),
)


def _bucket(classified: Any) -> str | None:
    if classified is None:
        return None
    return ";".join(
        f"{dimension.name}={classified[dimension.name]}"
        for dimension in WEB_FETCH_VOCABULARY.dimensions
    )


def test_core_owns_one_registration_per_web_fetch_definition() -> None:
    """v2 and v3 share the vocabulary, the four proofs and so the classifier."""

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
    assert v2.model_dump(exclude={"interface_bytes_hex", "interface_digest"}) == v3.model_dump(
        exclude={"interface_bytes_hex", "interface_digest"}
    )
    # The proof menu is fixed: changing it is a successor every pin must follow.
    assert [(proof.fixture_id, proof.selector) for proof in v3.conformance_proofs] == sorted(
        (
            ("web-fetch-api-json", "source_kind=api_json;access=*;page_weight=*"),
            ("web-fetch-rendered", "source_kind=js_rendered;access=public;page_weight=*"),
            ("web-fetch-static-light", "source_kind=static_html;access=*;page_weight=light"),
            ("web-fetch-static-medium", "source_kind=static_html;access=*;page_weight=medium"),
        ),
        key=lambda row: row[1].encode(),
    )
    assert core_owned_interface_registration(WEB_FETCH_INTERFACE_DIGEST) == v3
    assert core_owned_interface_registration(WEB_FETCH_INTERFACE_V2_DIGEST) == v2
    assert core_owned_interface_registration("sha256:" + "0" * 64) is None
    for registration in (v2, v3):
        path = provider_interface_path("web.fetch")
        law = evaluate_provider_interface_law(
            registration,
            path=path,
            predecessor=None,
            conformance_fixtures=core_provider_bucket_conformance_fixtures(),
        )
        assert law.verdict == "accepted", law.diagnostics
        installed = ProviderBucketClassifierRegistry().install(
            AcceptedProviderInterfaceRegistration(
                path=path,
                registration=registration,
                artifact_digest=provider_interface_digest(registration).tagged,
            ),
            WebFetchBucketClassifier(),
        )
        assert len(installed.results) == 4


def test_the_v3_definition_is_the_package_file_byte_for_byte() -> None:
    content = files("cruxible_core.providers").joinpath("web-fetch-interface-v3.json").read_bytes()
    assert hashlib.sha256(content).hexdigest() == PACKAGE_DEFINITION_FILE_DIGEST
    assert json.loads(content) == WEB_FETCH_INTERFACE_DEFINITION


@pytest.mark.parametrize(("value", "expected"), PARITY_GOLDEN)
def test_core_classifies_the_parity_golden(value: dict[str, Any], expected: str | None) -> None:
    assert _bucket(classify_web_fetch(value)) == expected
    if expected is None:
        with pytest.raises(ValueError):
            WebFetchBucketClassifier().classify(value, deadline=None)  # type: ignore[arg-type]
    else:
        assert WebFetchBucketClassifier().classify(value, deadline=None) == expected  # type: ignore[arg-type]


def _package_root() -> Path:
    checkout = provider_checkout_path()
    if checkout is None:
        pytest.skip("set CRUXIBLE_PROVIDERS_CHECKOUT to compare with cruxible-provider-web")
    return checkout / "packages/cruxible-provider-web/src/cruxible_provider_web"


def test_the_package_classifier_matches_core_on_the_parity_golden() -> None:
    """The reference package's own classifier measures every golden input as core does,
    over the same vocabulary, definition and (a subset of) fixtures."""

    pytest.importorskip("cruxible_provider_runtime", reason="the provider runtime toolchain")
    root = _package_root()
    spec = importlib.util.spec_from_file_location(
        "_web_fetch_package_interfaces", root / "interfaces.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for value, expected in PARITY_GOLDEN:
        assert _bucket(module.classify_web_fetch(value)) == expected, value
    assert module.FETCH_INTERFACE_DIGEST == WEB_FETCH_INTERFACE_DIGEST
    assert (root / "contracts/web.fetch.json").read_bytes() == (
        files("cruxible_core.providers").joinpath("web-fetch-interface-v3.json").read_bytes()
    )
    vocabulary = yaml.safe_load((root / "vocab/web.fetch.yaml").read_text())
    assert vocabulary.pop("status") == "draft"
    core = WEB_FETCH_VOCABULARY.model_dump(mode="json")
    assert core.pop("status") == "accepted"
    assert vocabulary == core
    fixtures = json.loads((root / "registration-fixtures/web.fetch.json").read_text())
    by_id = {
        item.fixture_id: item.model_dump(mode="json", exclude={"tag"})
        for item in WEB_FETCH_FIXTURES
    }
    assert fixtures and all(item == by_id[item["fixture_id"]] for item in fixtures)
    for item in fixtures:
        assert _bucket(classify_web_fetch(item["canonical_input"])) == item["measured_bucket_id"]
