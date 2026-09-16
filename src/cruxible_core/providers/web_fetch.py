"""Frozen web.fetch interface inputs and classifier, mirrored against the adapter.

This is compiler-owned conformance data, not an installed Provider or a grant.
A deployment still accepts its own exact implementation and runtime closure.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import CanonicalValue, canonical_bytes
from cruxible_client.contracts.provider_interfaces import (
    ProviderBucketConformanceFixtureProofV1,
    ProviderBucketConformanceFixtureV1,
    ProviderBucketVocabularyV1,
    ProviderInterfaceRegistrationV1,
    provider_bucket_classifier_digest,
    provider_bucket_fixture_digest,
    provider_bucket_fixture_set_digest,
    provider_bucket_vocabulary_digest,
    provider_external_interface_definition_digest,
)

WEB_FETCH_INTERFACE_PREIMAGE = {
    "contracts": {
        "input": {
            "allow_extra": False,
            "fields": {
                "credential_header": {
                    "default": "authorization",
                    "optional": True,
                    "type": "string",
                },
                "credential_ref": {"optional": True, "type": "string"},
                "expected_format": {
                    "default": "auto",
                    "enum": ["auto", "html", "json", "csv", "text", "bytes"],
                    "optional": True,
                    "type": "string",
                },
                "extract": {"default": True, "optional": True, "type": "bool"},
                "logical_source": {"default": "web.response", "optional": True, "type": "string"},
                "max_bytes": {"default": 262144, "optional": True, "type": "integer"},
                "paced": {"default": False, "optional": True, "type": "bool"},
                "render": {"default": False, "optional": True, "type": "bool"},
                "url": {"type": "string"},
            },
        },
        "output": "playbill-provider-result-to-external-capture-v1",
    },
    "effect_class": "external_read",
    "interface_id": "web.fetch",
    "refusals": [
        "provider_declined",
        "unresolved_secret_ref",
        "environment_divergence",
        "cross_origin_credentialed_redirect",
        "unsupported_redirect_scheme",
        "redirect_limit",
    ],
    "version": 2,
}
WEB_FETCH_INTERFACE_DIGEST = (
    "sha256:9769f47abc5ac2dae6d6c623a9f9abf01afde699de48768a755f40a0334a1ade"
)
WEB_FETCH_VOCABULARY = ProviderBucketVocabularyV1.model_validate(
    {
        "description": "Retrieve the content of a single web resource. Buckets "
        "separate the cases where a fetcher's competence genuinely "
        "differs: whether the content exists in the first response, "
        "what it costs to get at, and how much of it there is.",
        "dimensions": [
            {
                "classes": [
                    {
                        "description": "content present in the initial HTML response",
                        "id": "static_html",
                    },
                    {
                        "description": "content assembled client-side; requires a browser engine",
                        "id": "js_rendered",
                    },
                    {
                        "description": "a structured JSON or XML endpoint rather than a page",
                        "id": "api_json",
                    },
                    {
                        "description": "a non-text payload such as a PDF, image, or archive",
                        "id": "binary",
                    },
                ],
                "description": "how the content is produced by the origin",
                "name": "source_kind",
            },
            {
                "classes": [
                    {"description": "no credentials and no interactive gate", "id": "public"},
                    {
                        "description": "requires a credential delivered by secret-ref",
                        "id": "authenticated",
                    },
                    {
                        "description": "public but throttled; retrieval must pace itself",
                        "id": "rate_limited",
                    },
                ],
                "description": "what the origin requires before it will serve the resource",
                "name": "access",
            },
            {
                "classes": [
                    {"description": "at most 256 KiB", "id": "light"},
                    {"description": "more than 256 KiB and at most 2 MiB", "id": "medium"},
                    {"description": "more than 2 MiB", "id": "heavy"},
                ],
                "description": "transferred size of the resource",
                "name": "page_weight",
            },
        ],
        "interface_id": "web.fetch",
        "status": "accepted",
        "version": 1,
    }
)
WEB_FETCH_FIXTURES = (
    ProviderBucketConformanceFixtureV1(
        fixture_id="web-fetch-api-json",
        canonical_input={"url": "https://fixture.invalid/api/v1/measurements.json"},
        measured_bucket_id="source_kind=api_json;access=public;page_weight=light",
    ),
    ProviderBucketConformanceFixtureV1(
        fixture_id="web-fetch-rendered",
        canonical_input={"url": "https://fixture.invalid/dashboard", "render": True},
        measured_bucket_id="source_kind=js_rendered;access=public;page_weight=light",
    ),
    ProviderBucketConformanceFixtureV1(
        fixture_id="web-fetch-static-light",
        canonical_input={"url": "https://fixture.invalid/articles/tide-gauge-recalibration"},
        measured_bucket_id="source_kind=static_html;access=public;page_weight=light",
    ),
    ProviderBucketConformanceFixtureV1(
        fixture_id="web-fetch-static-medium",
        canonical_input={
            "url": "https://fixture.invalid/reports/water-quality",
            "max_bytes": 1048576,
        },
        measured_bucket_id="source_kind=static_html;access=public;page_weight=medium",
    ),
)
WEB_FETCH_SELECTORS = {
    "web-fetch-api-json": "source_kind=api_json;access=*;page_weight=*",
    "web-fetch-rendered": "source_kind=js_rendered;access=public;page_weight=*",
    "web-fetch-static-light": "source_kind=static_html;access=*;page_weight=light",
    "web-fetch-static-medium": "source_kind=static_html;access=*;page_weight=medium",
}
DEFAULT_MAX_BYTES = 262144
MAX_RESPONSE_BYTES = 33554432
LIGHT_CEILING_BYTES = 262144
MEDIUM_CEILING_BYTES = 2097152
_BINARY_SUFFIXES = {
    ".docx",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".pptx",
    ".tar",
    ".webp",
    ".xlsx",
    ".zip",
}
_STRUCTURED_SUFFIXES = {".xml", ".rss", ".atom", ".csv", ".json"}


def _path_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.lower()


def page_weight_class(byte_count: int) -> str:
    """The weight class a byte count falls in. Used for input and for response."""

    if byte_count <= LIGHT_CEILING_BYTES:
        return "light"
    if byte_count <= MEDIUM_CEILING_BYTES:
        return "medium"
    return "heavy"


def classify_web_fetch(payload: Mapping[str, Any]) -> Mapping[str, str] | None:
    """Derive a ``web.fetch`` bucket from the run input.

    ``source_kind`` follows the caller's explicit ``render`` flag first — asking
    for a browser is a statement about the resource — then the URL's own shape.
    ``access`` follows what the run carries: a credential ref means an
    authenticated fetch, a declared pace means a throttled origin.
    ``page_weight`` is the declared ``max_bytes`` cap, because weight is not
    knowable before retrieval; the adapter refuses if the response comes back
    heavier than the bucket the run was admitted into, so the declaration is
    checked rather than trusted.
    """

    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    path = _path_of(url)
    suffix = path[path.rfind(".") :] if "." in path.rsplit("/", 1)[-1] else ""

    expected_format = payload.get("expected_format", "auto")
    if expected_format not in {"auto", "html", "json", "csv", "text", "bytes"}:
        return None
    if bool(payload.get("render", False)):
        source_kind = "js_rendered"
    elif expected_format == "bytes" or suffix in _BINARY_SUFFIXES:
        source_kind = "binary"
    elif expected_format in {"json", "csv"} or suffix in _STRUCTURED_SUFFIXES or "/api/" in path:
        source_kind = "api_json"
    else:
        source_kind = "static_html"

    if payload.get("credential_ref"):
        access = "authenticated"
    elif bool(payload.get("paced", False)):
        access = "rate_limited"
    else:
        access = "public"

    max_bytes = payload.get("max_bytes", DEFAULT_MAX_BYTES)
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 0 < max_bytes <= MAX_RESPONSE_BYTES
    ):
        return None
    return {
        "source_kind": source_kind,
        "access": access,
        "page_weight": page_weight_class(max_bytes),
    }


def web_fetch_interface_registration() -> ProviderInterfaceRegistrationV1:
    proofs = tuple(
        sorted(
            (
                ProviderBucketConformanceFixtureProofV1(
                    selector=WEB_FETCH_SELECTORS[f.fixture_id],
                    fixture_id=f.fixture_id,
                    fixture_digest=provider_bucket_fixture_digest(f),
                    measured_bucket_id=f.measured_bucket_id,
                )
                for f in WEB_FETCH_FIXTURES
            ),
            key=lambda p: p.selector.encode(),
        )
    )
    proof_digest = provider_bucket_fixture_set_digest(proofs)
    content = canonical_bytes(WEB_FETCH_INTERFACE_PREIMAGE).hex()
    vocabulary = canonical_bytes(WEB_FETCH_VOCABULARY.model_dump(mode="json")).hex()
    return ProviderInterfaceRegistrationV1(
        identity=ArtifactIdentity(kind="ProviderInterface", name="web.fetch"),
        interface_id="web.fetch",
        interface_bytes_hex=content,
        interface_digest_domain="cruxible.interface.stub.v1",
        interface_digest=provider_external_interface_definition_digest(
            content, domain="cruxible.interface.stub.v1"
        ),
        vocabulary_bytes_hex=vocabulary,
        vocabulary_digest=provider_bucket_vocabulary_digest(vocabulary),
        classifier_identity="cruxible.core.web.fetch",
        classifier_version=1,
        classifier_digest=provider_bucket_classifier_digest(
            classifier_identity="cruxible.core.web.fetch",
            classifier_version=1,
            conformance_fixture_set_digest=proof_digest,
        ),
        conformance_fixture_set_digest=proof_digest,
        conformance_proofs=proofs,
        effect_class="external_read",
    )


class WebFetchBucketClassifier:
    classifier_identity = "cruxible.core.web.fetch"
    classifier_version = 1

    @property
    def classifier_digest(self) -> str:
        return web_fetch_interface_registration().classifier_digest

    def classify(self, canonical_input: CanonicalValue) -> str:
        if not isinstance(canonical_input, dict):
            raise ValueError("web.fetch input must be an object")
        classified = classify_web_fetch(canonical_input)
        if classified is None:
            raise ValueError("web.fetch input cannot be classified")
        return ";".join(
            f"{dimension.name}={classified[dimension.name]}"
            for dimension in WEB_FETCH_VOCABULARY.dimensions
        )
