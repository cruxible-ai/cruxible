"""Acquisition rules: how fresh a Source's Capture must be, and what unavailability does.

A Procedure declares ``acquisition_defaults`` per Source alias; a Line's
``acquisition`` replaces the default for any alias it names. The rule a run
admits for one Source is :class:`AcquisitionRule` (its alias, ``max_age`` and
``on_unavailable``). ``on_unavailable: omit`` lets an *absorbable* typed
refusal of the bound operation omit the Source instead of failing the run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.temporal import parse_iso_duration_microseconds

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
ACQUISITION_RULES_DIGEST_DOMAIN = "cruxible-acquisition-rules-v1"

UnavailableBehavior = Literal["refuse", "omit"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AcquisitionDefault(_Strict):
    """One Source alias's rule as a Procedure or Line declares it."""

    max_age: CanonicalDuration | None = None
    on_unavailable: UnavailableBehavior = "refuse"

    # One schema in both modes (it is served inside accepted Procedures); an
    # ISO-8601 duration string is also read, as authoring convenience.
    @field_validator("max_age", mode="before")
    @classmethod
    def _duration(cls, value: object) -> object:
        if isinstance(value, str):
            return CanonicalDuration(microseconds=parse_iso_duration_microseconds(value))
        return value


def validate_acquisition_keys(
    value: Mapping[str, AcquisitionDefault],
) -> dict[str, AcquisitionDefault]:
    for alias in value:
        if not _ALIAS_RE.fullmatch(alias):
            raise ValueError(f"acquisition rules are keyed by Source alias, not {alias!r}")
    return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))


class AcquisitionRule(_Strict):
    """The rule one run admits for one Source alias."""

    input_name: str
    max_age: CanonicalDuration | None = None
    on_unavailable: UnavailableBehavior = "refuse"


def resolve_acquisition_rules(
    aliases: tuple[str, ...],
    *,
    defaults: Mapping[str, AcquisitionDefault],
    overrides: Mapping[str, AcquisitionDefault],
) -> dict[str, AcquisitionRule]:
    """Every Source alias's rule: the Line's override, else the Procedure's default."""

    rules: dict[str, AcquisitionRule] = {}
    for alias in aliases:
        declared = overrides.get(alias) or defaults.get(alias) or AcquisitionDefault()
        rules[alias] = AcquisitionRule(
            input_name=alias,
            max_age=declared.max_age,
            on_unavailable=declared.on_unavailable,
        )
    return rules


def acquisition_rules_digest(rules: Mapping[str, AcquisitionRule]) -> str:
    """The digest a run admission records for its resolved acquisition rules."""

    return typed_digest(
        ArtifactDigest,
        ACQUISITION_RULES_DIGEST_DOMAIN,
        {
            alias: rule.model_dump(mode="json")
            for alias, rule in sorted(rules.items(), key=lambda item: item[0].encode("utf-8"))
        },
    ).tagged


class AcquisitionInputDecision(_Strict):
    input_name: str
    disposition: Literal["selected", "omitted", "defaulted", "refused"]
    considered_capture_digests: tuple[str, ...] = ()
    selected_capture_digests: tuple[str, ...] = ()
    selected_cursors: tuple[str, ...] = ()
    default_value: object | None = None
    reason_codes: tuple[str, ...] = ()

    @field_validator("default_value", mode="before")
    @classmethod
    def _default(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)


__all__ = [
    "ACQUISITION_RULES_DIGEST_DOMAIN",
    "AcquisitionDefault",
    "AcquisitionInputDecision",
    "AcquisitionRule",
    "UnavailableBehavior",
    "acquisition_rules_digest",
    "resolve_acquisition_rules",
    "validate_acquisition_keys",
]
