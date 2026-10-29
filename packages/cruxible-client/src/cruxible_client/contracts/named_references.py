"""Revision-36 authoring conversions, restricted to declared reference fields."""

from __future__ import annotations

from cruxible_client.contracts.procedures.line_specs import LineSpec, NamedLineSpec
from cruxible_client.contracts.providers import (
    NamedProviderBuild,
    NamedProviderIdentity,
    ProviderBuild,
    ProviderV1,
)
from cruxible_client.contracts.query.definitions import NamedQueryDefinition, QueryDefinition
from cruxible_client.contracts.triggers import NamedTrigger, Trigger


def name_query(query: QueryDefinition) -> NamedQueryDefinition:
    return NamedQueryDefinition.model_validate(
        {
            **query.model_dump(mode="python", exclude={"pins", "artifact_format"}),
            "artifact_format": "cruxible-query-definition-v3",
        }
    )


def name_provider(provider: ProviderBuild) -> NamedProviderBuild:
    if isinstance(provider, NamedProviderBuild):
        return provider
    return NamedProviderBuild.model_validate(
        {
            **provider.model_dump(
                mode="json",
                by_alias=True,
                exclude={"pins", "artifact_format", "capture_contract_digests"},
            ),
            "capture_contracts": _capture_names(provider),
        }
    )


def _capture_names(provider: ProviderV1) -> list[dict[str, object]]:
    return [
        {"target": pin.target.model_dump(mode="json")}
        for pin in provider.pins
        if pin.role == "capture-contract"
        and pin.artifact_digest in provider.capture_contract_digests
    ]


def name_signing_provider(provider: ProviderV1) -> NamedProviderIdentity:
    """Migrate signing metadata on edit; package runtimes use name_provider."""
    if isinstance(provider, NamedProviderIdentity):
        return provider
    return NamedProviderIdentity.model_validate(
        {
            **provider.model_dump(
                mode="json", exclude={"pins", "artifact_format", "capture_contract_digests"}
            ),
            "capture_contracts": _capture_names(provider),
        }
    )


def name_line(line: LineSpec) -> NamedLineSpec:
    """A Line already pins exactly what it runs: there is one format, unchanged here."""

    return line


def name_trigger(trigger: Trigger) -> NamedTrigger:
    data = trigger.model_dump(mode="json", exclude={"pins"})
    data["artifact_format"] = "cruxible-trigger-v2"
    if data["target"]["kind"] == "line":
        data["target"]["line"] = {"target": data["target"]["line"]["target"]}
    schedule = data["schedule"]
    if schedule["kind"] == "capture_landing":
        schedule["event"].pop("capture_contract_digest", None)
    if schedule["kind"] == "window_close" and schedule["window"]["kind"] == "capture_event":
        schedule["window"]["event"].pop("capture_contract_digest", None)
    return NamedTrigger.model_validate(data)
