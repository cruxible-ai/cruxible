"""Acquisition rules: how fresh and how replayable a Source's Capture must
be, and what unavailability, staleness and an oversized read each do.

Acquisition rules live on the Line only: a Line's ``acquisition`` declares a
rule per capture output, keyed ``<alias>.<capture field>``. The Procedure keeps
only what is intrinsic (each capture field's CaptureContract). A capture output
the Line names no rule for runs under :data:`CORE_DEFAULT_ACQUISITION_RULE`,
stated here and recorded on the admitted rule (``declared_by:
"core_default"``), never silently. The rule a run admits for one capture output
is :class:`AcquisitionRule`; each field of a call is decided by its own rule. ``on_unavailable``,
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

#: A capture output: ``<acquiring call alias>.<capture field>``, or for an
#: invoked child's, ``<invoke node path>.<alias>.<field>`` (as Line bindings key
#: a child's slots).
_CAPTURE_OUTPUT_RE = re.compile(
    r"^(?:[a-z][a-z0-9_.-]{0,127}\.)*[a-z][a-z0-9_]{0,127}\.[a-z][a-z0-9_]{0,127}$"
)
ACQUISITION_RULES_DIGEST_DOMAIN = "cruxible-acquisition-rules-v2"

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


#: F05/§4.1/§4.4: a permitted network origin, ``scheme://host[:port]``, no path.
_ORIGIN_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+$")


def _sorted_unique_origins(value: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not value:
        raise ValueError("origins must not be empty when declared")
    for origin in value:
        if not _ORIGIN_RE.fullmatch(origin):
            raise ValueError(f"{origin!r} is not a scheme://host[:port] origin")
    return tuple(sorted(set(value)))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LineAcquisitionRule(_Strict):
    """One capture output's rule as a Line declares it."""

    max_age: CanonicalDuration | None = None
    on_unavailable: UnavailableBehavior = "refuse"
    on_stale: UnavailableBehavior = "refuse"
    on_oversized: UnavailableBehavior = "refuse"
    #: ``None`` permits every replay class; declared, it is the closed set a
    #: Source's Capture must replay under.
    permitted_replayability: tuple[SourceReplayability, ...] | None = None
    #: F05/§4.1/§4.4: permitted network origins (``scheme://host[:port]``).
    #: ``None`` permits any; declared, it is the closed set core enforces on
    #: every field named ``url`` or ``final_url`` the bound operation's
    #: request or result carries, for this capture field alone.
    origins: tuple[str, ...] | None = None

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

    @field_validator("origins")
    @classmethod
    def _origins(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return _sorted_unique_origins(value)


#: The documented core default for a capture output its Line names no rule for:
#: any age (no ``max_age``); refuse the run when the Source is unavailable,
#: stale or oversized; any replay class; any network origin.
CORE_DEFAULT_ACQUISITION_RULE: LineAcquisitionRule = LineAcquisitionRule(
    max_age=None,
    on_unavailable="refuse",
    on_stale="refuse",
    on_oversized="refuse",
    permitted_replayability=None,
    origins=None,
)

#: Who declared an admitted rule: the run's Line, or the core default.
AcquisitionRuleOrigin = Literal["line", "core_default"]


def validate_acquisition_keys(
    value: Mapping[str, LineAcquisitionRule],
) -> dict[str, LineAcquisitionRule]:
    for key in value:
        if not _CAPTURE_OUTPUT_RE.fullmatch(key):
            raise ValueError(
                "acquisition rules are keyed by capture output <alias>.<field> (an invoked "
                f"child's: <invoke node path>.<alias>.<field>), not {key!r}"
            )
    return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))


def child_acquisition(
    rules: Mapping[str, LineAcquisitionRule], prefix: str
) -> dict[str, LineAcquisitionRule]:
    """The Line's rules for one invoked child's capture outputs, keyed by its own aliases."""

    return {
        key[len(prefix) :]: value
        for key, value in rules.items()
        if key.startswith(prefix) and key[len(prefix) :].count(".") == 1
    }


class AcquisitionRule(_Strict):
    """The rule one run admits for one capture output (``input_name`` is ``<alias>.<field>``).

    ``declared_by`` records whether the run's Line declared it or the capture
    output ran under :data:`CORE_DEFAULT_ACQUISITION_RULE`.
    """

    input_name: str
    declared_by: AcquisitionRuleOrigin
    max_age: CanonicalDuration | None = None
    on_unavailable: UnavailableBehavior = "refuse"
    on_stale: UnavailableBehavior = "refuse"
    on_oversized: UnavailableBehavior = "refuse"
    permitted_replayability: tuple[SourceReplayability, ...] | None = None
    origins: tuple[str, ...] | None = None

    @field_validator("permitted_replayability")
    @classmethod
    def _replayability(
        cls, value: tuple[SourceReplayability, ...] | None
    ) -> tuple[SourceReplayability, ...] | None:
        return _sorted_unique_replayability(value)

    @field_validator("origins")
    @classmethod
    def _origins(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return _sorted_unique_origins(value)


def resolve_acquisition_rules(
    aliases: tuple[str, ...],
    *,
    line_rules: Mapping[str, LineAcquisitionRule],
) -> dict[str, AcquisitionRule]:
    """Every capture output's rule: the Line's, else the stated core default."""

    rules: dict[str, AcquisitionRule] = {}
    for alias in aliases:
        line_rule = line_rules.get(alias)
        declared = CORE_DEFAULT_ACQUISITION_RULE if line_rule is None else line_rule
        rules[alias] = AcquisitionRule(
            input_name=alias,
            declared_by="core_default" if line_rule is None else "line",
            max_age=declared.max_age,
            on_unavailable=declared.on_unavailable,
            on_stale=declared.on_stale,
            on_oversized=declared.on_oversized,
            permitted_replayability=declared.permitted_replayability,
            origins=declared.origins,
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
    "CORE_DEFAULT_ACQUISITION_RULE",
    "AcquisitionRuleOrigin",
    "LineAcquisitionRule",
    "AcquisitionInputDecision",
    "AcquisitionRule",
    "UnavailableBehavior",
    "acquisition_rules_digest",
    "child_acquisition",
    "resolve_acquisition_rules",
    "validate_acquisition_keys",
]
