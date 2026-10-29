"""Acquisition rules: how fresh and how replayable a Source's Capture must
be, and what unavailability, staleness and an oversized read each do.

A Procedure declares ``acquisition_defaults`` per Source alias; a Line's
``acquisition`` replaces the default for any alias it names. The rule a run
admits for one Source is :class:`AcquisitionRule`. ``on_unavailable``,
``on_stale`` and ``on_oversized`` each choose ``refuse`` (the default) or
``omit``: an absorbable typed refusal, a Capture older than ``max_age``, or
one over the bound operation's byte budget, omits the Source from the run
instead of failing it (F05). ``permitted_replayability``, when declared, is a
hard eligibility gate, never an omit choice: a Source whose replay class is
outside the declared set is never selected (the old replayability
eligibility check, F05).
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
SourceReplayability = Literal["exact", "attested_only"]


def _sorted_unique_replayability(
    value: tuple[SourceReplayability, ...] | None,
) -> tuple[SourceReplayability, ...] | None:
    if value is None:
        return None
    if not value:
        raise ValueError("permitted_replayability must not be empty when declared")
    return tuple(sorted(set(value)))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AcquisitionDefault(_Strict):
    """One Source alias's rule as a Procedure or Line declares it."""

    max_age: CanonicalDuration | None = None
    on_unavailable: UnavailableBehavior = "refuse"
    on_stale: UnavailableBehavior = "refuse"
    on_oversized: UnavailableBehavior = "refuse"
    #: ``None`` permits every replay class; declared, it is the closed set a
    #: Source's Capture must replay under.
    permitted_replayability: tuple[SourceReplayability, ...] | None = None

    # One schema in both modes (it is served inside accepted Procedures); an
    # ISO-8601 duration string is also read, as authoring convenience.
    @field_validator("max_age", mode="before")
    @classmethod
    def _duration(cls, value: object) -> object:
        if isinstance(value, str):
            return CanonicalDuration(microseconds=parse_iso_duration_microseconds(value))
        return value

    @field_validator("permitted_replayability")
    @classmethod
    def _replayability(
        cls, value: tuple[SourceReplayability, ...] | None
    ) -> tuple[SourceReplayability, ...] | None:
        return _sorted_unique_replayability(value)


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
    on_stale: UnavailableBehavior = "refuse"
    on_oversized: UnavailableBehavior = "refuse"
    permitted_replayability: tuple[SourceReplayability, ...] | None = None

    @field_validator("permitted_replayability")
    @classmethod
    def _replayability(
        cls, value: tuple[SourceReplayability, ...] | None
    ) -> tuple[SourceReplayability, ...] | None:
        return _sorted_unique_replayability(value)


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
            on_stale=declared.on_stale,
            on_oversized=declared.on_oversized,
            permitted_replayability=declared.permitted_replayability,
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
