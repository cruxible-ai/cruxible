"""Core's frozen web.fetch definitions and the contract registration of each.

Core owns the ``web.fetch`` contract: each definition revision it knows (v2, and
v3 as ``cruxible-provider-web`` 0.2.x ships it) is a contract, and its
registration is the contract alone (``ProviderInterfaceContract``), named by
its contract key. Every package implementing one of these definitions binds that
same registration, so any implementation of the contract binds the same
ProviderInterface artifact and a Blueprint slot typed by it takes any of them.

This is not an installed Provider or a grant. A deployment still accepts its own
exact implementation and runtime closure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from typing import Any, Final

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import WEB_FETCH_V3_INTERFACE_DIGEST
from cruxible_client.contracts.provider_interfaces import (
    ProviderInterfaceContract,
    provider_external_interface_definition_digest,
    provider_interface_contract,
)

#: The v2 operation definition, as core first froze it.
WEB_FETCH_INTERFACE_V2_DEFINITION: dict[str, Any] = {
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
WEB_FETCH_INTERFACE_V2_DIGEST = (
    "sha256:9769f47abc5ac2dae6d6c623a9f9abf01afde699de48768a755f40a0334a1ade"
)
_INTERFACE_DOMAIN: Final = "cruxible.interface.stub.v1"
#: The v3 operation definition, byte-for-byte the package's
#: ``contracts/web.fetch.json`` (cruxible-provider-web 0.2.x); its canonical bytes
#: reproduce ``WEB_FETCH_INTERFACE_DIGEST`` (checked at import).
WEB_FETCH_INTERFACE_DEFINITION: dict[str, Any] = json.loads(
    files("cruxible_core.providers").joinpath("web-fetch-interface-v3.json").read_bytes()
)
WEB_FETCH_INTERFACE_DIGEST = WEB_FETCH_V3_INTERFACE_DIGEST
#: Interface digest -> the definition revision core owns under it.
_DEFINITIONS: Mapping[str, Mapping[str, Any]] = {
    WEB_FETCH_INTERFACE_V2_DIGEST: WEB_FETCH_INTERFACE_V2_DEFINITION,
    WEB_FETCH_INTERFACE_DIGEST: WEB_FETCH_INTERFACE_DEFINITION,
}
#: Every web.fetch definition core owns a registration of.
WEB_FETCH_INTERFACE_DIGESTS = frozenset(_DEFINITIONS)
if any(
    provider_external_interface_definition_digest(
        canonical_bytes(dict(definition)).hex(), domain=_INTERFACE_DOMAIN
    )
    != digest
    for digest, definition in _DEFINITIONS.items()
):  # pragma: no cover - import-time guard on checked-in bytes
    raise RuntimeError("a web.fetch interface definition drifted from its frozen digest")


def web_fetch_interface_registration(
    interface_digest: str = WEB_FETCH_INTERFACE_DIGEST,
) -> ProviderInterfaceContract:
    """The contract registration of one web.fetch definition revision (v3 by default)."""

    return _registration(interface_digest)


@cache
def _registration(interface_digest: str) -> ProviderInterfaceContract:
    definition = _DEFINITIONS[interface_digest]
    return provider_interface_contract(
        interface_id="web.fetch",
        interface_bytes_hex=canonical_bytes(dict(definition)).hex(),
        interface_digest_domain=_INTERFACE_DOMAIN,
        interface_digest=interface_digest,
        effect_class="external_read",
    )
