"""Accepted-state semantic discovery over the governed naming layer.

The vocabulary is rebuilt from accepted facts at the resolved coordinate, so a
discovery page is a pure function of that coordinate and the request. Refusal
conventions match the sibling expand service: a coordinate that is not an
accepted one is refused before any state is read.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.canonical import ArtifactDigest
from cruxible_client.contracts.claim_types import ClaimType, parse_claim_type
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.line_specs import AcceptedLineSpec
from cruxible_client.contracts.provider_contracts import (
    ProviderOperationContract,
    read_provider_operation_contract,
)
from cruxible_client.contracts.provider_interfaces import (
    ProviderEffectClass,
    ProviderInterfaceRegistrationAny,
    parse_provider_interface,
    provider_interface_digest,
)
from cruxible_client.contracts.providers import ProviderV2, parse_provider, provider_digest
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinition,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_core.evidence.source_readers import ExternalSourceReaderProtocol
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.query.backends import ClaimQueryFactsV1, subject_query_view
from cruxible_core.query.semantic_discovery import (
    DiscoveryVocabularyV1,
    build_discovery_vocabulary,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.claims.claim_types import CLAIM_TYPE_PATH_PREFIX
from cruxible_core.service.discovery.query import build_accepted_query_facts
from cruxible_core.service.discovery.query_definitions import QUERY_DEFINITION_PATH_PREFIX


class _StrictDiscoveryServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderInterfaceImplementationV1(_StrictDiscoveryServiceModel):
    """One live Provider implementing an interface, with the pins a Source node needs."""

    tag: Literal["playbill-provider-interface-implementation-v1"] = (
        "playbill-provider-interface-implementation-v1"
    )
    provider_identity: str
    provider_artifact_digest: str
    implementation_digest: str

    @field_validator("provider_artifact_digest", "implementation_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class ProviderInterfaceEntryV1(_StrictDiscoveryServiceModel):
    tag: Literal["playbill-provider-interface-entry-v1"] = "playbill-provider-interface-entry-v1"
    identity: str
    artifact_digest: str
    artifact_kind: Literal["ProviderInterface"] = "ProviderInterface"
    pin_role: Literal["provider-interface"] = "provider-interface"
    interface_digest: str
    vocabulary_digest: str
    classifier_digest: str
    effect_class: ProviderEffectClass
    classifier_status: Literal["installed", "not_installed"]
    interface_basis: Literal["accepted_registration"] = "accepted_registration"
    # Additive: the live Providers implementing this interface, so an author can
    # pin a graph-v4 Source node from the served inventory alone.
    providers: tuple[ProviderInterfaceImplementationV1, ...] = ()
    operation_contract: ProviderOperationContract | None = None

    @field_validator(
        "artifact_digest",
        "interface_digest",
        "vocabulary_digest",
        "classifier_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


@dataclass(frozen=True)
class AcceptedProviderInterface:
    """One live accepted interface: its registration and its inventory entry."""

    registration: ProviderInterfaceRegistrationAny
    entry: ProviderInterfaceEntryV1


def _provider_interfaces(
    tree: Mapping[str, bytes],
    *,
    installed_classifier_digests: frozenset[str],
) -> tuple[AcceptedProviderInterface, ...]:
    implementations: dict[str, list[ProviderInterfaceImplementationV1]] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("providers/"):
            continue
        provider = parse_provider(tree[path], path=path)
        if provider.lifecycle.state != "live" or not isinstance(provider, ProviderV2):
            continue
        for implementation in provider.implementations:
            implementations.setdefault(implementation.interface_id, []).append(
                ProviderInterfaceImplementationV1(
                    provider_identity=provider.identity.qualified,
                    provider_artifact_digest=provider_digest(provider).tagged,
                    implementation_digest=implementation.implementation_digest,
                )
            )
    entries: list[AcceptedProviderInterface] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("provider-interfaces/"):
            continue
        registration = parse_provider_interface(tree[path], path=path)
        if registration.lifecycle.state != "live":
            continue
        entry = ProviderInterfaceEntryV1(
            providers=tuple(
                sorted(
                    implementations.get(registration.interface_id, ()),
                    key=lambda item: (
                        item.provider_identity.encode("utf-8"),
                        item.implementation_digest.encode("ascii"),
                    ),
                )
            ),
            identity=registration.identity.qualified,
            artifact_digest=provider_interface_digest(registration).tagged,
            interface_digest=registration.interface_digest,
            operation_contract=(
                read_provider_operation_contract(registration.interface_bytes_hex)
                if "contracts" in json.loads(bytes.fromhex(registration.interface_bytes_hex))
                else None
            ),
            vocabulary_digest=registration.vocabulary_digest,
            classifier_digest=registration.classifier_digest,
            effect_class=registration.effect_class,
            classifier_status=(
                "installed"
                if registration.classifier_digest in installed_classifier_digests
                else "not_installed"
            ),
        )
        entries.append(AcceptedProviderInterface(registration=registration, entry=entry))
    return tuple(sorted(entries, key=lambda item: item.entry.identity.encode("utf-8")))


def accepted_provider_interfaces(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    installed_classifier_digests: frozenset[str] = frozenset(),
) -> tuple[AcceptedProviderInterface, ...]:
    """Every live accepted provider interface at one coordinate, sorted by identity.

    The one inventory source: ``discover(profile="interfaces")`` serves its
    entries, and ``orient(section="interfaces")`` its compact rows.
    """

    with instance.bind_accepted_projection(coordinate) as projection:
        tree = {
            row.path: projection.typed.member_bytes(row.path)
            for kind in ("provider", "provider-interface")
            for row in projection.typed.envelopes(kind=kind)
        }
    return _provider_interfaces(tree, installed_classifier_digests=installed_classifier_digests)


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def accepted_claim_types(tree: Mapping[str, bytes]) -> tuple[ClaimType, ...]:
    """Return every accepted ClaimType in byte-sorted ledger-path order."""

    return tuple(
        parse_claim_type(tree[path], path=path)
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if path.startswith(CLAIM_TYPE_PATH_PREFIX)
    )


def accepted_query_definitions(
    tree: Mapping[str, bytes],
) -> tuple[AcceptedQueryDefinition, ...]:
    """Return every accepted QueryDefinition in byte-sorted ledger-path order."""

    definitions: list[AcceptedQueryDefinition] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith(QUERY_DEFINITION_PATH_PREFIX):
            continue
        query = parse_query_definition(tree[path], path=path)
        definitions.append(
            AcceptedQueryDefinition(
                path=path,
                query=query,
                artifact_digest=query_definition_digest(query).tagged,
            )
        )
    return tuple(definitions)


def build_accepted_discovery_vocabulary(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    facts: ClaimQueryFactsV1 | None = None,
    procedures: Iterable[AcceptedProcedure] = (),
    line_specs: Iterable[AcceptedLineSpec] = (),
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> DiscoveryVocabularyV1:
    """Project the accepted naming layer at one coordinate into a vocabulary."""

    resolved_facts = facts or build_accepted_query_facts(
        instance,
        coordinate=coordinate,
        external_readers=external_readers,
    )
    with instance.bind_accepted_projection(coordinate) as projection:
        tree = {
            row.path: projection.typed.member_bytes(row.path)
            for kind in ("claim-type", "query-definition")
            for row in projection.typed.envelopes(kind=kind)
        }
    return build_discovery_vocabulary(
        view=subject_query_view(resolved_facts),
        facts=resolved_facts,
        claim_types=accepted_claim_types(tree),
        definitions=accepted_query_definitions(tree),
        procedures=procedures,
        line_specs=line_specs,
    )


__all__ = [
    "AcceptedProviderInterface",
    "ProviderInterfaceEntryV1",
    "accepted_claim_types",
    "accepted_provider_interfaces",
    "accepted_query_definitions",
    "build_accepted_discovery_vocabulary",
]
