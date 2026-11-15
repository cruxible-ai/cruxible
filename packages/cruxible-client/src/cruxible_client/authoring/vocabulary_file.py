"""Export, plan and apply the vocabulary file (``.cruxible/vocabulary.yaml``).

Client-side, like every authoring front end: the daemon receives an ordinary
change set, never a path. ``export`` reads the accepted vocabulary at the head
and writes it in shorthand where shorthand round-trips, in the full ClaimType
input form otherwise, checking every entry by lowering it back to the accepted
digest. ``plan`` reads the vocabulary at the export the file names
(``based_on``), so an edit lowers onto -- and pins -- the predecessor the author
saw; a definition someone changed since then refuses at preflight, per
definition. ``apply`` submits the same members as one change set.

Cost: export and plan read the vocabulary once (O(kinds + fields), as
``cruxible stub`` does); an enum mapping reads only the Claims it moves. Nothing
here runs per write or per tick.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml
from pydantic import ValidationError

from cruxible_client import contracts as api
from cruxible_client._error_base import CoreError
from cruxible_client._safe_files import read_regular_file
from cruxible_client.authoring.floor_apply import write_workspace_file
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.authoring.inputs import ChangeSetInput, lower_authoring_input
from cruxible_client.contracts.authoring.models import ChangeSetAuthoringPayload
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_type_inputs import (
    ClaimTypeInputRecord,
    default_resolution_policy,
    lower_claim_type_input,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
    retired_successor,
)
from cruxible_client.contracts.claims import ClaimArtifact, LiteralClaimObject
from cruxible_client.contracts.compact_query import (
    QUERY_MAX_LIMIT,
    QueryFilterIn,
    QueryRequest,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.get_reads import GET_BATCH_MAX_REFS, GetBatchRequest
from cruxible_client.contracts.identity_normalizers import NoneNormalizer
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.shapes import (
    EnumType,
    SubjectType,
    coerce_field_type,
    parse_type,
    render_type,
    value_members,
)
from cruxible_client.contracts.subject_kinds import (
    SubjectKind,
    subject_kind_digest,
    subject_kind_path,
)
from cruxible_client.contracts.temporal import format_iso_duration_microseconds
from cruxible_client.contracts.vocabulary_file import (
    VOCABULARY_FILE_FORMAT,
    VocabularyApplyResult,
    VocabularyBaseline,
    VocabularyBaselineError,
    VocabularyClaimChange,
    VocabularyExportRefused,
    VocabularyExportResult,
    VocabularyField,
    VocabularyFile,
    VocabularyFileError,
    VocabularyKind,
    VocabularyMemberMapping,
    VocabularyPlan,
    VocabularyPlanEntry,
    VocabularyPreflight,
    load_vocabulary_file,
    read_vocabulary_header,
)
from cruxible_client.contracts.workspace_layout import (
    VOCABULARY_FILE_PATH,
    ensure_workspace_directory,
    workspace_directory_conflict,
)
from cruxible_client.contracts.write import write_accept
from cruxible_client.errors import PermissionDeniedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.authoring.sdk import Cruxible
    from cruxible_client.transport.http import CruxibleClient

#: The ClaimType input fields export tries to leave out, in this order: each is
#: dropped only when the entry still lowers to the accepted digest without it.
_DROP_ORDER = (
    "evidence_admission_policy",
    "resolution_policy",
    "admission_policy",
    "permitted_roles",
    "evidence_requirement",
    "revision_evidence",
    "canonical_form",
    "allowed_subject_kinds",
    "value",
    "precedence",
    "requires_complete_input",
    "incomplete_fallback",
    "referent_sensitivity",
    "pins",
    "evidence_freshness",
    "attestation_consequence_policy",
    "unsure_hold_for",
    "member_descriptions",
    "default_role",
    "inverse",
    "description",
)

#: The order a field's properties are written in: shorthands, then the full form.
_FIELD_ORDER = (
    "key",
    "enum",
    "link",
    "value",
    "many",
    "description",
    *(name for name in ClaimTypeInputRecord.model_fields if name not in {"predicate", "value"}),
)

_HEADER = (
    "# Cruxible vocabulary: each kind and its fields (ClaimTypes).",
    "# Written by `cruxible vocabulary export`. Edit it, then `cruxible vocabulary plan`",
    "# and `cruxible vocabulary apply`. A kind or field left out is untouched; only",
    "# `retired: true` retires. A field states what differs from the defaults",
    "# (`cruxible claim-type propose --template --expanded` shows them).",
)


# -- the accepted vocabulary ------------------------------------------------------------


@dataclass(frozen=True)
class AcceptedVocabulary:
    """Every live ClaimType and SubjectKind at one coordinate, with the kit that owns each."""

    coordinate: AcceptedCoordinate
    claim_types: dict[str, ClaimType]
    subject_kinds: dict[str, SubjectKind]
    kits: dict[str, str] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        """sha256 over the accepted digest of every live definition, by path."""

        preimage = {
            "claim_types": {
                claim_type_path(predicate): claim_type_digest(item).tagged
                for predicate, item in self.claim_types.items()
            },
            "subject_kinds": {
                subject_kind_path(kind): subject_kind_digest(item).tagged
                for kind, item in self.subject_kinds.items()
            },
        }
        return "sha256:" + hashlib.sha256(canonical_bytes(preimage)).hexdigest()

    def baseline(self) -> VocabularyBaseline:
        return VocabularyBaseline(
            git_oid=self.coordinate.git_oid,
            semantic_root=self.coordinate.semantic_root,
            generation_root=self.coordinate.generation_root,
            compiler_digest=self.coordinate.compiler_digest,
            vocabulary_digest=self.digest,
        )


def read_accepted_vocabulary(
    client: CruxibleClient,
    instance_id: str,
    *,
    at: AcceptedCoordinate | None,
    evaluation_time: str,
) -> AcceptedVocabulary:
    """One read of the live vocabulary: two paged orient sections and batched gets.

    ``at`` None reads the head; every later page is pinned to the coordinate
    the first one answered.
    """

    coordinate = at
    predicates: list[str] = []
    kits: dict[str, str] = {}
    for section in ("claim_types", "subject_kinds"):
        cursor: str | None = None
        while True:
            page = client.orient(
                instance_id,
                section=cast(Any, section),
                limit=api.ORIENT_MAX_LIMIT,
                cursor=cursor,
                at=coordinate,
                surface="sdk",
            )
            answered = AcceptedCoordinate.model_validate(page.coordinate.model_dump(mode="json"))
            if coordinate is not None and answered != coordinate:
                raise ValueError("the vocabulary read returned a different accepted coordinate")
            coordinate = answered
            if section == "claim_types":
                for row in page.claim_types or ():
                    predicates.append(row.predicate)
                    if row.kit is not None:
                        kits[f"ClaimType:{row.predicate}"] = row.kit
            else:
                for kind_row in page.subject_kinds or ():
                    predicates.append(f"SubjectKind:{kind_row.kind}")
                    if kind_row.kit is not None:
                        kits[f"SubjectKind:{kind_row.kind}"] = kind_row.kit
            if not page.truncated or page.next_cursor is None:
                break
            cursor = page.next_cursor
    assert coordinate is not None
    refs = [name if name.startswith("SubjectKind:") else f"ClaimType:{name}" for name in predicates]
    claim_types: dict[str, ClaimType] = {}
    subject_kinds: dict[str, SubjectKind] = {}
    for start in range(0, len(refs), GET_BATCH_MAX_REFS):
        batch = client.get_batch(
            instance_id,
            request=GetBatchRequest(
                refs=tuple(refs[start : start + GET_BATCH_MAX_REFS]),
                at=coordinate,
                evaluation_time=datetime.fromisoformat(evaluation_time),
            ),
        )
        for ref, result in zip(refs[start : start + GET_BATCH_MAX_REFS], batch.results):
            envelope = (result.proof or {}).get("envelope")
            if ref.startswith("SubjectKind:"):
                kind = SubjectKind.model_validate(envelope)
                subject_kinds[kind.subject_kind] = kind
            else:
                claim_type = ClaimType.model_validate(envelope)
                claim_types[claim_type.predicate] = claim_type
    return AcceptedVocabulary(
        coordinate=coordinate,
        claim_types=claim_types,
        subject_kinds=subject_kinds,
        kits=kits,
    )


def _content_digest(claim_type: ClaimType) -> str:
    """The digest of what a ClaimType says, apart from the lineage it lands in."""

    return claim_type_digest(
        claim_type.model_copy(update={"lifecycle": ArtifactLifecycle()})
    ).tagged


# -- lowering one entry -----------------------------------------------------------------


def _kept_members(entry: VocabularyField) -> list[tuple[str, str | None]]:
    if isinstance(entry.enum, dict):
        return [
            (member, meaning)
            for member, meaning in entry.enum.items()
            if not isinstance(meaning, VocabularyMemberMapping)
        ]
    return [(member, None) for member in entry.enum or ()]


def entry_input(
    predicate: str, entry: VocabularyField, predecessor: ClaimType | None
) -> ClaimTypeInputRecord:
    """The ClaimType input one field entry states, shorthands expanded.

    Over a predecessor it is an edit: what the entry leaves out keeps the
    accepted value (merge-patch). Over none, every default applies.
    """

    stated = dict(entry.stated)
    if entry.enum is not None:
        kept = _kept_members(entry)
        stated["value"] = {"kind": "enum", "members": [member for member, _ in kept]}
        stated["member_descriptions"] = [
            {"member": member, "description": meaning}
            for member, meaning in kept
            if meaning is not None
        ]
    if entry.link is not None:
        kinds = (entry.link,) if isinstance(entry.link, str) else entry.link
        stated["value"] = {
            "kind": "subject",
            "subject_kinds": sorted(set(kinds), key=lambda item: item.encode("utf-8")),
        }
    if "cardinality" not in stated and (entry.many is not None or predecessor is None):
        cardinality: Literal["one", "many"] = "many" if entry.many else "one"
        stated["cardinality"] = cardinality
        if (
            predecessor is not None
            and predecessor.cardinality != cardinality
            and "resolution_policy" not in stated
        ):
            # A stated change of cardinality takes the resolution policy a new
            # field of that cardinality takes, as ``ChangeSetDraft.field`` does.
            stated["resolution_policy"] = default_resolution_policy(cardinality)
    return ClaimTypeInputRecord.model_validate({"predicate": predicate, **stated})


def lower_entry(predicate: str, entry: VocabularyField, predecessor: ClaimType | None) -> ClaimType:
    """Lower one field entry: a new ClaimType, or the successor of ``predecessor``."""

    record = entry_input(predicate, entry, predecessor)
    tree = (
        {} if predecessor is None else {claim_type_path(predicate): render_claim_type(predecessor)}
    )
    return lower_claim_type_input(record, tree=tree)


# -- export -----------------------------------------------------------------------------


def _lowers_to(candidate: Mapping[str, object], target: str) -> bool:
    try:
        lowered = lower_claim_type_input(ClaimTypeInputRecord.model_validate(candidate), tree={})
    except (FormatError, ValidationError, ValueError):
        return False
    return _content_digest(lowered) == target


def _minimal_input(claim_type: ClaimType) -> dict[str, object]:
    """The fewest ClaimType input fields that lower back to this exact ClaimType."""

    full = claim_type.model_dump(mode="json", exclude={"artifact_format", "identity", "lifecycle"})
    target = _content_digest(claim_type)
    if not _lowers_to(full, target):
        raise VocabularyExportRefused(
            f"ClaimType:{claim_type.predicate} does not lower back from its own input form, so "
            "export would change what it means; read it with `cruxible get "
            f"ClaimType:{claim_type.predicate} --detail proof`"
        )
    current = dict(full)
    sources = _source_ids(current.get("evidence_admission_policy"))
    if sources:
        # A policy that is the short form's own rules for these sources is said
        # as the sources it anticipates.
        trial = {key: value for key, value in current.items() if key != "evidence_admission_policy"}
        trial["anticipated_source_ids"] = sources
        if _lowers_to(trial, target):
            current = trial
    for name in _DROP_ORDER:
        if name not in current:
            continue
        trial = {key: value for key, value in current.items() if key != name}
        if _lowers_to(trial, target):
            current = trial
    for name, value in tuple(current.items()):
        tidied = _tidy(value)
        if tidied != value and _lowers_to({**current, name: tidied}, target):
            current[name] = tidied
    return current


def _source_ids(policy: object) -> list[str]:
    if not isinstance(policy, Mapping) or not isinstance(policy.get("rules"), list):
        return []
    ids = [
        str(rule["rule_id"]).removeprefix("source-")
        for rule in cast(list[Mapping[str, object]], policy["rules"])
        if str(rule.get("rule_id", "")).startswith("source-")
    ]
    return sorted(set(ids), key=lambda item: item.encode("utf-8"))


def _tidy(value: object) -> object:
    """``value`` without wire tags and empty defaults, durations as ISO-8601.

    Kept only when the entry still lowers to the same ClaimType.
    """

    if isinstance(value, Mapping):
        if value.get("tag") == "playbill-duration-v1" and isinstance(
            value.get("microseconds"), int
        ):
            return format_iso_duration_microseconds(cast(int, value["microseconds"]))
        return {
            key: _tidy(item)
            for key, item in value.items()
            if key != "tag" and item is not None and item != []
        }
    if isinstance(value, list):
        return [_tidy(item) for item in value]
    return value


def _value_spelling(value: object) -> object:
    field_type = coerce_field_type(cast(Any, value))
    try:
        spelled = render_type(field_type)
        if parse_type(spelled) == field_type:
            return spelled
    except (FormatError, ValueError):
        pass
    return value


def _field_entry(claim_type: ClaimType, *, key: bool) -> dict[str, object]:
    """One field as the file writes it, checked by lowering it back."""

    minimal = _minimal_input(claim_type)
    entry: dict[str, object] = {}
    if key:
        entry["key"] = True
    value = minimal.pop("value", None)
    meanings = {
        str(item["member"]): str(item["description"])
        for item in cast(list[dict[str, object]], minimal.pop("member_descriptions", None) or [])
    }
    if value is not None:
        field_type = coerce_field_type(cast(Any, value))
        if isinstance(field_type, EnumType):
            entry["enum"] = (
                {member: meanings.get(member) for member in field_type.members}
                if meanings
                else list(field_type.members)
            )
        elif isinstance(field_type, SubjectType):
            kinds = list(field_type.subject_kinds)
            entry["link"] = kinds[0] if len(kinds) == 1 else kinds
        else:
            entry["value"] = _value_spelling(value)
    elif meanings:  # pragma: no cover - meanings need an enum value
        minimal["member_descriptions"] = [
            {"member": member, "description": meaning} for member, meaning in meanings.items()
        ]
    cardinality = minimal.pop("cardinality")
    minimal.pop("predicate")
    if cardinality == "many":
        entry["many"] = True
    entry.update(minimal)
    ordered = {name: entry[name] for name in _FIELD_ORDER if name in entry}
    parsed = VocabularyField.model_validate(ordered)
    if _content_digest(lower_entry(claim_type.predicate, parsed, None)) != _content_digest(
        claim_type
    ):
        raise VocabularyExportRefused(
            f"ClaimType:{claim_type.predicate}: its file entry does not lower back to the "
            "accepted definition"
        )
    return ordered


def _short_name(kind: str, predicate: str) -> str | None:
    prefix, _, name = predicate.rpartition(".")
    return name if prefix == kind else None


def _subject_kind_entry(
    kind: str, declared: SubjectKind, field_names: set[str]
) -> tuple[dict[str, object], tuple[str, ...]]:
    """The kind-level properties, and the fields marked ``key: true`` (in key order)."""

    entry: dict[str, object] = {}
    if declared.description is not None:
        entry["description"] = declared.description
    key_fields: tuple[str, ...] = ()
    if declared.minted:
        entry["minted"] = True
    else:
        names = [_short_name(kind, predicate) for predicate in declared.keys]
        if all(name is not None and name in field_names for name in names):
            key_fields = tuple(cast(list[str], names))
        else:
            entry["keys"] = list(declared.keys)
        rule = declared.identity_rule
        if getattr(rule, "uniqueness", "unique") == "hint":
            entry["uniqueness"] = "hint"
        normalizers = {
            (_short_name(kind, predicate) or predicate): declared.normalizer(predicate).model_dump(
                mode="json"
            )
            for predicate in declared.keys
            if not isinstance(declared.normalizer(predicate), NoneNormalizer)
        }
        if normalizers:
            entry["normalizers"] = normalizers
    return entry, key_fields


def _yaml_block(data: Mapping[str, object], indent: int) -> list[str]:
    if not data:
        return []
    text = yaml.safe_dump(
        dict(data), sort_keys=False, allow_unicode=True, default_flow_style=False, width=88
    )
    pad = " " * indent
    return [pad + line if line else line for line in text.rstrip("\n").split("\n")]


def _yaml_key(name: str) -> str:
    return yaml.safe_dump(name, allow_unicode=True).split("\n")[0]


def render_vocabulary_file(accepted: AcceptedVocabulary, *, instance_id: str) -> str:
    """The vocabulary file for one accepted vocabulary, byte-identical for the same one."""

    by_kind: dict[str, dict[str, ClaimType]] = {}
    for predicate in sorted(accepted.claim_types, key=lambda item: item.encode("utf-8")):
        kind, _, name = predicate.rpartition(".")
        by_kind.setdefault(kind, {})[name] = accepted.claim_types[predicate]
    for kind in accepted.subject_kinds:
        by_kind.setdefault(kind, {})
    lines = [
        *_HEADER,
        *_yaml_block(
            {
                "format": VOCABULARY_FILE_FORMAT,
                "instance": instance_id,
                "based_on": accepted.baseline().model_dump(mode="json"),
            },
            0,
        ),
        "kinds:" if by_kind else "kinds: {}",
    ]
    # Every field's entry first, so one refusal names every definition that
    # cannot be written faithfully, not only the first.
    kind_entries = {
        kind: _subject_kind_entry(kind, declared, set(by_kind[kind]))
        for kind, declared in accepted.subject_kinds.items()
    }
    entries: dict[str, dict[str, object]] = {}
    unwritable: list[str] = []
    for claim_type in accepted.claim_types.values():
        kind, _, name = claim_type.predicate.rpartition(".")
        key_names = kind_entries[kind][1] if kind in kind_entries else ()
        try:
            entries[claim_type.predicate] = _field_entry(claim_type, key=name in key_names)
        except VocabularyExportRefused:
            unwritable.append(f"ClaimType:{claim_type.predicate}")
    if unwritable:
        names = sorted(unwritable, key=lambda item: item.encode("utf-8"))
        raise VocabularyExportRefused(
            f"{len(names)} definition(s) do not lower back from their own input form, so export "
            f"would change what they mean: {', '.join(names)}; read each with `cruxible get "
            "<ref> --detail proof`"
        )
    for kind in sorted(by_kind, key=lambda item: item.encode("utf-8")):
        fields = by_kind[kind]
        props, key_fields = kind_entries.get(kind, ({}, ()))
        kit = accepted.kits.get(f"SubjectKind:{kind}")
        if kit is not None:
            lines.append(f"  # kit: {kit}")
        lines.append(f"  {_yaml_key(kind)}:")
        lines.extend(_yaml_block(props, 4))
        if not fields:
            if not props:  # pragma: no cover - a listed kind has fields or a SubjectKind
                lines[-1] += " {}"
            continue
        lines.append("    fields:")
        ordered = [*key_fields, *sorted(set(fields) - set(key_fields))]
        for name in ordered:
            claim_type = fields[name]
            entry = entries[claim_type.predicate]
            kit = accepted.kits.get(f"ClaimType:{claim_type.predicate}")
            if kit is not None:
                lines.append(f"      # kit: {kit}")
            if entry:
                lines.append(f"      {_yaml_key(name)}:")
                lines.extend(_yaml_block(entry, 8))
            else:
                lines.append(f"      {_yaml_key(name)}: {{}}")
    rendered = "\n".join(lines) + "\n"
    # What export writes, plan reads back: a rendering slip refuses here, not later.
    parsed = load_vocabulary_file(rendered)
    for kind, kind_entry in parsed.kinds.items():
        declared = accepted.subject_kinds.get(kind)
        if declared is not None and _merged_subject_kind(
            kind, kind_entry, None
        ) != _subject_kind_view(declared):
            raise VocabularyExportRefused(
                f"SubjectKind:{kind}: its file entry does not restate the accepted SubjectKind"
            )
    return rendered


def export_vocabulary(cx: Cruxible, *, force: bool = False) -> VocabularyExportResult:
    """Write the accepted vocabulary to ``.cruxible/vocabulary.yaml``.

    Replaces an existing file only when nothing in it would be lost: it
    parses and, against accepted state, states no change of its own (an edit
    already applied, or a definition only the export behind it held, is not
    lost). Otherwise it refuses, naming the pending changes, unless ``force``.
    Git is never consulted.
    """

    root = cx._workspace_root
    conflict = workspace_directory_conflict(root)
    if conflict is not None:
        raise VocabularyExportRefused(conflict)
    accepted = read_accepted_vocabulary(
        cx._client,
        cx._instance_id,
        at=cx.coordinate if cx._pinned else None,
        evaluation_time=cx._evaluation_time(),
    )
    rendered = render_vocabulary_file(accepted, instance_id=cx._instance_id).encode("utf-8")
    target = root / VOCABULARY_FILE_PATH
    try:
        existing: bytes | None = read_regular_file(target)
    except FileNotFoundError:
        existing = None
    if existing is not None and existing != rendered and not force:
        pending = _pending_edits(cx, existing, accepted)
        if pending:
            raise VocabularyExportRefused(
                f"{VOCABULARY_FILE_PATH} holds changes not in accepted state "
                f"({', '.join(pending)}): apply them (`cruxible vocabulary apply`), or pass "
                "--force to replace the file"
            )
    written = write_workspace_file(ensure_workspace_directory(root), VOCABULARY_FILE_PATH, rendered)
    kinds = {predicate.rpartition(".")[0] for predicate in accepted.claim_types} | set(
        accepted.subject_kinds
    )
    return VocabularyExportResult(
        path=VOCABULARY_FILE_PATH,
        status="written" if written else "unchanged",
        based_on=accepted.baseline(),
        kinds=len(kinds),
        fields=len(accepted.claim_types),
    )


# -- SubjectKinds -----------------------------------------------------------------------


def _normalizer_json(rule: object) -> dict[str, object]:
    return cast(dict[str, object], cast(Any, rule).model_dump(mode="json"))


def _subject_kind_view(declared: SubjectKind) -> dict[str, object]:
    """What a SubjectKind says, in one comparable form."""

    if declared.minted:
        return {"description": declared.description, "minted": True}
    return {
        "description": declared.description,
        "keys": list(declared.keys),
        "uniqueness": getattr(declared.identity_rule, "uniqueness", "unique"),
        "normalizers": {
            predicate: _normalizer_json(declared.normalizer(predicate))
            for predicate in declared.keys
            if not isinstance(declared.normalizer(predicate), NoneNormalizer)
        },
    }


def _merged_subject_kind(
    kind: str, entry: VocabularyKind, current: SubjectKind | None
) -> dict[str, object]:
    """The SubjectKind this entry states, merged onto the accepted one (left out: kept)."""

    stated = entry.model_fields_set
    base = {} if current is None else _subject_kind_view(current)
    key_fields = [f"{kind}.{name}" for name, item in entry.fields.items() if item.key]
    if entry.keys is not None and key_fields:
        raise VocabularyFileError(
            f"kinds.{kind}: keys and key: true both name its keys; use one",
            code="cruxible.vocabulary.kind_invalid",
        )
    keys = list(entry.keys) if entry.keys is not None else key_fields or None
    description = entry.description if "description" in stated else base.get("description")
    if entry.minted and keys:
        raise VocabularyFileError(
            f"kinds.{kind}: a kind is minted or keyed, not both",
            code="cruxible.vocabulary.kind_invalid",
        )
    if entry.minted or (keys is None and base.get("minted") and "minted" not in stated):
        if entry.uniqueness is not None or entry.normalizers:
            raise VocabularyFileError(
                f"kinds.{kind}: a minted kind states no uniqueness or normalizers",
                code="cruxible.vocabulary.kind_invalid",
            )
        return {"description": description, "minted": True}
    if keys is None:
        keys = cast(list[str] | None, base.get("keys"))
    if not keys:
        raise VocabularyFileError(
            f"kinds.{kind}: a new kind names its keys (key: true on its key fields) or "
            "minted: true",
            code="cruxible.vocabulary.kind_invalid",
        )
    uniqueness: str
    if entry.uniqueness is not None:
        uniqueness = entry.uniqueness
    elif base.get("keys"):
        uniqueness = cast(str, base.get("uniqueness", "unique"))
    else:
        uniqueness = "unique"
    if entry.normalizers is not None:
        normalizers: dict[str, object] = {}
        for given, rule in entry.normalizers.items():
            predicate = next(
                (key for key in keys if given == key or given == key.removeprefix(kind + ".")),
                None,
            )
            if predicate is None:
                raise VocabularyFileError(
                    f"kinds.{kind}.normalizers: {given!r} names no key of {kind}",
                    code="cruxible.vocabulary.kind_invalid",
                )
            if not isinstance(rule, NoneNormalizer):
                normalizers[predicate] = _normalizer_json(rule)
    else:
        prior = cast(dict[str, object], base.get("normalizers", {}))
        normalizers = {key: value for key, value in prior.items() if key in keys}
    return {
        "description": description,
        "keys": list(keys),
        "uniqueness": uniqueness,
        "normalizers": normalizers,
    }


def _subject_kind_member(
    kind: str,
    view: Mapping[str, object],
    *,
    retire: bool,
    predecessor: SubjectKind | None,
) -> dict[str, Any]:
    member: dict[str, Any] = {"kind": "subject_kind", "name": kind}
    if predecessor is not None:
        # The daemon refuses this member if the accepted SubjectKind moved since
        # the export read it, as a ClaimType succession's predecessor pin does.
        member["predecessor_digest"] = subject_kind_digest(predecessor).tagged
    if view.get("description") is not None:
        member["description"] = view["description"]
    if view.get("minted"):
        member["minted"] = True
    else:
        member["keys"] = list(cast(list[str], view["keys"]))
        member["uniqueness"] = view["uniqueness"]
        if view.get("normalizers"):
            member["normalizers"] = dict(cast(dict[str, object], view["normalizers"]))
    if retire:
        member["retire"] = True
    return member


# -- the plan ---------------------------------------------------------------------------


@dataclass
class _Planned:
    entries: list[VocabularyPlanEntry] = field(default_factory=list)
    claims: list[VocabularyClaimChange] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changes_only(self) -> list[VocabularyPlanEntry]:
        return [entry for entry in self.entries if entry.action != "not_in_file"]


def _changed(current: ClaimType, successor: ClaimType) -> tuple[str, ...]:
    before = current.model_dump(mode="json", exclude={"lifecycle"})
    after = successor.model_dump(mode="json", exclude={"lifecycle"})
    return tuple(
        sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    )


def _mappings(
    predicate: str,
    entry: VocabularyField,
    current: ClaimType | None,
) -> dict[str, VocabularyMemberMapping]:
    if not isinstance(entry.enum, dict):
        return {}
    mapped = {
        member: meaning
        for member, meaning in entry.enum.items()
        if isinstance(meaning, VocabularyMemberMapping)
    }
    if not mapped:
        return {}
    if current is None:
        raise VocabularyFileError(
            f"{predicate}: becomes and retire map members of an accepted enum, and "
            f"{predicate} is not accepted",
            code="cruxible.vocabulary.mapping_invalid",
        )
    accepted = value_members(current.value)
    kept = {member for member, _ in _kept_members(entry)}
    for member, mapping in mapped.items():
        if member not in accepted:
            raise VocabularyFileError(
                f"{predicate}: {member!r} is not a member of the accepted enum "
                f"({', '.join(accepted) or 'not an enum'})",
                code="cruxible.vocabulary.mapping_invalid",
            )
        if mapping.becomes is not None and mapping.becomes not in kept:
            raise VocabularyFileError(
                f"{predicate}: {member!r} becomes {mapping.becomes!r}, which the enum does "
                "not keep",
                code="cruxible.vocabulary.mapping_invalid",
            )
    return mapped


def _subject_text(claim: ClaimArtifact) -> str:
    path = claim.statement.subject.artifact_path
    kind, _, name = path.removeprefix("subjects/").removesuffix(".json").rpartition("/")
    return f"{kind}/{name}"


def _affected_claims(
    cx: Cruxible,
    *,
    at: AcceptedCoordinate,
    predicate: str,
    current: ClaimType,
    members: Sequence[str],
) -> list[str]:
    """The live Claims of ``predicate`` stating one of ``members``, by an indexed query.

    The cost is the Claims named, never the predicate's whole population.
    """

    found: set[str] = set()
    for kind in current.allowed_subject_kinds:
        cursor: str | None = None
        while True:
            page = cx._client.query(
                cx._instance_id,
                request=QueryRequest(
                    kind=kind,
                    where=(QueryFilterIn.model_validate({"field": predicate, "in": members}),),
                    select=(predicate,),
                    claims=True,
                    status=("live", "overturned", "ineligible"),
                    limit=QUERY_MAX_LIMIT,
                    cursor=cursor,
                    at=at,
                ),
            )
            for row in page.rows:
                cells = row.get("claims")
                if not isinstance(cells, Mapping):
                    continue
                for listed in cells.values():
                    for item in cast(Iterable[Any], listed or ()):
                        claim = item if isinstance(item, Mapping) else item.model_dump()
                        if claim.get("value") in members:
                            found.add(str(claim["claim"]).removeprefix("Claim:"))
            if not page.truncated or page.next_cursor is None:
                break
            cursor = page.next_cursor
    return sorted(found, key=lambda item: item.encode("ascii"))


def _read_claims(cx: Cruxible, at: AcceptedCoordinate, ids: Sequence[str]) -> list[ClaimArtifact]:
    from cruxible_client.authoring.sdk import _claim_from_public_view

    claims: list[ClaimArtifact] = []
    refs = [f"Claim:{claim_id}" for claim_id in ids]
    for start in range(0, len(refs), GET_BATCH_MAX_REFS):
        batch = cx._client.get_batch(
            cx._instance_id,
            request=GetBatchRequest(
                refs=tuple(refs[start : start + GET_BATCH_MAX_REFS]),
                at=at,
                evaluation_time=datetime.fromisoformat(cx._evaluation_time()),
            ),
        )
        for result in batch.results:
            claims.append(
                _claim_from_public_view(api.ClaimViewRecord.model_validate(result.proof or {}))
            )
    return claims


def _mapped_claims(
    cx: Cruxible,
    *,
    at: AcceptedCoordinate,
    predicate: str,
    current: ClaimType,
    mapped: Mapping[str, VocabularyMemberMapping],
) -> tuple[list[VocabularyClaimChange], list[dict[str, Any]], list[dict[str, Any]]]:
    """The Claim changes, succession dependents and sibling Claim members of a mapping."""

    ids = _affected_claims(cx, at=at, predicate=predicate, current=current, members=list(mapped))
    changes: list[VocabularyClaimChange] = []
    dependents: list[dict[str, Any]] = []
    siblings: list[dict[str, Any]] = []
    for claim in _read_claims(cx, at, ids):
        statement = claim.statement
        if claim.lifecycle.state != "live" or not isinstance(statement.object, LiteralClaimObject):
            continue
        value = statement.object.value
        mapping = mapped.get(value) if isinstance(value, str) else None
        if mapping is None:
            continue
        claim_id = claim.identity.name
        subject = _subject_text(claim)
        if mapping.retire is not None:
            changes.append(
                VocabularyClaimChange(
                    claim=claim_id,
                    subject=subject,
                    field=predicate,
                    value=str(value),
                    action="retire",
                    reason=mapping.retire,
                )
            )
            dependents.append(
                {
                    "tag": "playbill-claim-type-succession-dependent-v1",
                    "identity": {"kind": "Claim", "name": claim_id},
                    "disposition": "retire",
                    "claim_retirement_reason": mapping.retire,
                }
            )
            continue
        cited = {citation.capture_digest: citation.role for citation in claim.backing.citations}
        if len(cited) != 1:
            raise VocabularyFileError(
                f"{predicate}: Claim {claim_id} ({subject}) cites "
                f"{len(cited) or 'no'} Captures, so becomes cannot say it again with its own "
                "evidence; re-author it with cx.changes().succeed_claim_type(...)",
                code="cruxible.vocabulary.mapping_invalid",
            )
        ((capture, role),) = cited.items()
        changes.append(
            VocabularyClaimChange(
                claim=claim_id,
                subject=subject,
                field=predicate,
                value=str(value),
                action="re_author",
                becomes=mapping.becomes,
                capture=capture,
            )
        )
        dependents.append(
            {
                "tag": "playbill-claim-type-succession-dependent-v1",
                "identity": {"kind": "Claim", "name": claim_id},
                "disposition": "re_author",
                "successor_claim_id": claim_id,
            }
        )
        sibling: dict[str, Any] = {
            "kind": "claim",
            "subject": subject,
            "predicate": predicate,
            "object": {"kind": "literal", "value": mapping.becomes},
            "role": statement.role,
            "rationale": f"The vocabulary maps {predicate} {value} to {mapping.becomes}.",
            "source": {"kind": "existing_capture", "capture_digest": capture},
            "citation_role": "copy" if role == "copy" else "evidence",
            "revises": claim_id,
        }
        if statement.qualifier is not None:
            sibling["qualifier"] = statement.qualifier
        if statement.effective_from is not None:
            sibling["effective_from"] = statement.effective_from.isoformat()
        if statement.effective_until is not None:
            sibling["effective_until"] = statement.effective_until.isoformat()
        siblings.append(sibling)
    dependents.sort(key=lambda item: f"Claim:{item['identity']['name']}".encode("utf-8"))
    return changes, dependents, siblings


def _live_claims(cx: Cruxible, at: AcceptedCoordinate, predicate: str) -> int | None:
    """The live Claims of one ClaimType at ``at``, from the counter Claim writes maintain."""

    batch = cx._client.get_batch(
        cx._instance_id,
        request=GetBatchRequest(
            refs=(f"ClaimType:{predicate}",),
            detail="summary",
            at=at,
            evaluation_time=datetime.fromisoformat(cx._evaluation_time()),
        ),
    )
    count = getattr(batch.results[0].card, "live_claims", None) if batch.results else None
    return count if isinstance(count, int) else None


def _refuse_stranded_members(
    cx: Cruxible,
    *,
    at: AcceptedCoordinate,
    predicate: str,
    current: ClaimType,
    successor: ClaimType,
    mapped: Mapping[str, VocabularyMemberMapping],
) -> None:
    """An enum member dropped without a mapping must state no live Claim.

    Only the members the edit drops are queried (cost: the Claims stating
    them). Without this the preflight refuses each such Claim as an invalid
    value, with advice meant for a new field.
    """

    before, after = value_members(current.value), set(value_members(successor.value))
    dropped = [member for member in before if member not in after and member not in mapped]
    if not dropped:
        return
    stranded = _affected_claims(cx, at=at, predicate=predicate, current=current, members=dropped)
    if stranded:
        examples = " or ".join(
            f"`{member}: {{becomes: <member>}}` / `{member}: {{retire: superseded}}`"
            for member in dropped[:2]
        )
        raise VocabularyFileError(
            f"{predicate}: {len(stranded)} live Claim(s) state {', '.join(dropped)}, which the "
            f"edited enum drops; say what they become in the file's enum: {examples}",
            code="cruxible.vocabulary.mapping_required",
        )


def _plan_members(
    cx: Cruxible,
    file: VocabularyFile,
    accepted: AcceptedVocabulary,
    *,
    claims_at: AcceptedCoordinate | None = None,
) -> _Planned:
    """Plan the file against ``accepted`` (definitions and their pinned digests).

    ``claims_at`` is where Claims are read (the head: data written after the
    export is normal); it defaults to ``accepted``'s coordinate.
    """

    planned = _Planned()
    at = claims_at or accepted.coordinate
    named_types: set[str] = set()
    for kind in sorted(file.kinds, key=lambda item: item.encode("utf-8")):
        entry = file.kinds[kind]
        current_kind = accepted.subject_kinds.get(kind)
        definition = f"SubjectKind:{kind}"
        kit = accepted.kits.get(definition)
        if entry.retired:
            if current_kind is None:
                raise VocabularyFileError(
                    f"kinds.{kind}: retired: true, but {kind} has no accepted SubjectKind",
                    code="cruxible.vocabulary.kind_invalid",
                )
            planned.members.append(
                _subject_kind_member(
                    kind, _subject_kind_view(current_kind), retire=True, predecessor=current_kind
                )
            )
            planned.entries.append(
                VocabularyPlanEntry(definition=definition, action="retire", kit=kit)
            )
        elif entry.states_subject_kind:
            merged = _merged_subject_kind(kind, entry, current_kind)
            before = None if current_kind is None else _subject_kind_view(current_kind)
            if merged != before:
                planned.members.append(
                    _subject_kind_member(kind, merged, retire=False, predecessor=current_kind)
                )
                planned.entries.append(
                    VocabularyPlanEntry(
                        definition=definition,
                        action="add" if before is None else "replace",
                        changed=()
                        if before is None
                        else tuple(
                            sorted(
                                name
                                for name in set(merged) | set(before)
                                if merged.get(name) != before.get(name)
                            )
                        ),
                        kit=kit,
                    )
                )
        for name in sorted(entry.fields, key=lambda item: item.encode("utf-8")):
            item = entry.fields[name]
            predicate = f"{kind}.{name}"
            named_types.add(predicate)
            definition = f"ClaimType:{predicate}"
            kit = accepted.kits.get(definition)
            current = accepted.claim_types.get(predicate)
            if item.retired:
                if current is None:
                    raise VocabularyFileError(
                        f"kinds.{kind}.fields.{name}: retired: true, but {predicate} is not an "
                        "accepted field",
                        code="cruxible.vocabulary.field_invalid",
                    )
                planned.members.append(
                    {
                        "kind": "claim_type_succession",
                        "successor": retired_successor(current).model_dump(mode="json"),
                        "retire_dependents": item.reason or "superseded",
                    }
                )
                planned.entries.append(
                    VocabularyPlanEntry(
                        definition=definition,
                        action="retire",
                        kit=kit,
                        live_claims=_live_claims(cx, at, predicate),
                    )
                )
                continue
            mapped = _mappings(predicate, item, current)
            try:
                lowered = lower_entry(predicate, item, current)
            except (FormatError, ValidationError, ValueError) as exc:
                raise VocabularyFileError(
                    f"kinds.{kind}.fields.{name}: {exc}", code="cruxible.vocabulary.field_invalid"
                ) from exc
            if current is None:
                planned.members.append(
                    {"kind": "claim_type", "claim_type": lowered.model_dump(mode="json")}
                )
                planned.entries.append(VocabularyPlanEntry(definition=definition, action="add"))
                continue
            if _content_digest(lowered) == _content_digest(current):
                continue
            member: dict[str, Any] = {
                "kind": "claim_type_succession",
                "successor": lowered.model_dump(mode="json"),
                "carry_all": True,
            }
            _refuse_stranded_members(
                cx, at=at, predicate=predicate, current=current, successor=lowered, mapped=mapped
            )
            if mapped:
                changes, dependents, siblings = _mapped_claims(
                    cx, at=at, predicate=predicate, current=current, mapped=mapped
                )
                planned.claims.extend(changes)
                if dependents:
                    member["dependents"] = dependents
                planned.members.extend(siblings)
            planned.members.append(member)
            planned.entries.append(
                VocabularyPlanEntry(
                    definition=definition,
                    action="replace",
                    changed=_changed(current, lowered),
                    kit=kit,
                )
            )
    for predicate in sorted(set(accepted.claim_types) - named_types, key=lambda i: i.encode()):
        planned.entries.append(
            VocabularyPlanEntry(
                definition=f"ClaimType:{predicate}",
                action="not_in_file",
                kit=accepted.kits.get(f"ClaimType:{predicate}"),
            )
        )
    for kind in sorted(set(accepted.subject_kinds) - set(file.kinds), key=lambda i: i.encode()):
        planned.entries.append(
            VocabularyPlanEntry(
                definition=f"SubjectKind:{kind}",
                action="not_in_file",
                kit=accepted.kits.get(f"SubjectKind:{kind}"),
            )
        )
    return planned


def _pending_edits(cx: Cruxible, content: bytes, head: AcceptedVocabulary) -> list[str]:
    """The changes a vocabulary file states that accepted state does not hold.

    Each entry means what it means against the file's own export: an edit is
    lowered onto the definition that export held, and a new definition takes
    the defaults of a new one (its omissions are never edits to whatever the
    head holds under that name). The entry is pending unless that intended
    definition equals the export's (unedited) or the head's (applied); a
    retirement is applied only once the head no longer holds the definition.
    ``not_in_file`` never counts. An unreadable file is one pending change.
    When the header cannot establish what the file meant (its export cannot be
    read, or its vocabulary digest does not match), the file is kept: export
    refuses, naming the header and the repair. Definitions only, on every
    path: no Claim is read and no plan is run.
    """

    try:
        file = load_vocabulary_file(content)
    except VocabularyFileError as exc:
        return [f"the file cannot be read: {exc}"]
    if file.instance != cx._instance_id:
        return [f"the file was exported from instance {file.instance}"]
    try:
        exported: AcceptedVocabulary | None = read_accepted_vocabulary(
            cx._client,
            cx._instance_id,
            at=file.based_on.coordinate,
            evaluation_time=cx._evaluation_time(),
        )
    except CoreError:
        exported = None
    if exported is None or exported.digest != file.based_on.vocabulary_digest:
        raise VocabularyExportRefused(
            f"{VOCABULARY_FILE_PATH}: its based_on header does not match the export it names "
            f"(at {file.based_on.git_oid[:12]}), so what its entries mean cannot be "
            "established and its edits would be lost; restore the header, or pass --force to "
            "replace the file with accepted state"
        )
    pending: list[str] = []
    for kind in sorted(file.kinds, key=lambda item: item.encode("utf-8")):
        entry = file.kinds[kind]
        before_kind = exported.subject_kinds.get(kind)
        head_kind = head.subject_kinds.get(kind)
        if entry.retired:
            # Applied only once the head no longer holds it.
            if head_kind is not None:
                pending.append(f"SubjectKind:{kind}")
        elif entry.states_subject_kind:
            try:
                intended = _merged_subject_kind(kind, entry, before_kind)
            except VocabularyFileError as exc:
                pending.append(str(exc))
            else:
                views = [_subject_kind_view(item) for item in (before_kind, head_kind) if item]
                if intended not in views:
                    pending.append(f"SubjectKind:{kind}")
        for name in sorted(entry.fields, key=lambda item: item.encode("utf-8")):
            item = entry.fields[name]
            predicate = f"{kind}.{name}"
            before = exported.claim_types.get(predicate)
            now = head.claim_types.get(predicate)
            if item.retired:
                # Applied only once the head no longer holds it.
                if now is not None:
                    pending.append(f"ClaimType:{predicate}")
                continue
            try:
                lowered = _content_digest(lower_entry(predicate, item, before))
            except (FormatError, ValidationError, ValueError) as exc:
                pending.append(f"kinds.{kind}.fields.{name}: {exc}")
                continue
            if lowered not in {_content_digest(held) for held in (before, now) if held}:
                pending.append(f"ClaimType:{predicate}")
    return pending


def _read_file(root: Path, path: str | Path | None) -> tuple[str, bytes]:
    target = Path(path) if path is not None else Path(VOCABULARY_FILE_PATH)
    absolute = target if target.is_absolute() else root / target
    try:
        content = read_regular_file(absolute)
    except FileNotFoundError as exc:
        raise VocabularyFileError(
            f"{target} does not exist; `cruxible vocabulary export` writes it",
            code="cruxible.vocabulary.file_missing",
        ) from exc
    try:
        shown = str(absolute.resolve().relative_to(root.resolve()))
    except ValueError:
        shown = str(absolute)
    return shown, content


@dataclass(frozen=True)
class _PlannedFile:
    plan: VocabularyPlan
    file: VocabularyFile
    accepted: AcceptedVocabulary
    change_set: ChangeSetInput | None


def _plan(
    cx: Cruxible,
    path: str | Path | None,
    *,
    rationale: str | None = None,
    preview: bool = True,
) -> _PlannedFile:
    root = cx._workspace_root
    shown, content = _read_file(root, path)
    file = load_vocabulary_file(content)
    if file.instance != cx._instance_id:
        raise VocabularyBaselineError(
            f"{shown} was exported from instance {file.instance}, not {cx._instance_id}; "
            "export this instance's vocabulary"
        )
    coordinate = file.based_on.coordinate
    try:
        accepted = read_accepted_vocabulary(
            cx._client, cx._instance_id, at=coordinate, evaluation_time=cx._evaluation_time()
        )
    except CoreError as exc:
        raise VocabularyBaselineError(
            f"{shown} names an export at {coordinate.git_oid[:12]} this instance cannot read "
            f"({exc}); re-export with `cruxible vocabulary export` and merge your edits"
        ) from exc
    if accepted.digest != file.based_on.vocabulary_digest:
        raise VocabularyBaselineError(
            f"{shown}: based_on.vocabulary_digest does not match the vocabulary at "
            f"{coordinate.git_oid[:12]}; the header was edited: re-export and merge your edits"
        )
    pinned = cx.at(coordinate)
    # Definitions pin the export's digests; Claims are data, read at the head,
    # where the daemon's preflight will meet them.
    head = AcceptedCoordinate.model_validate(
        cx._client.whoami(cx._instance_id).coordinate.model_dump(mode="json")
    )
    planned = _plan_members(pinned, file, accepted, claims_at=head)
    change_set = (
        ChangeSetInput.model_validate(
            {
                "kind": "change_set",
                "members": planned.members,
                "rationale": rationale or "Apply the vocabulary file.",
            }
        )
        if planned.members
        else None
    )
    if change_set is None:
        preflight = VocabularyPreflight(verdict="not_run", note="nothing to change")
    elif not preview:
        preflight = VocabularyPreflight(verdict="not_run", note="apply runs its own preflight")
    else:
        try:
            result = cx._client.preview_authoring_input(
                cx._instance_id, input=change_set.model_dump(mode="json", exclude_unset=True)
            )
        except PermissionDeniedError as exc:
            preflight = VocabularyPreflight(
                verdict="not_run", note=f"the preflight needs governed write: {exc}"
            )
        else:
            preflight = VocabularyPreflight(
                verdict=result.verdict, frontier=result.frontier, lint=result.lint
            )
    plan = VocabularyPlan(
        path=shown,
        based_on=file.based_on,
        claims=tuple(planned.claims),
        entries=tuple(planned.entries),
        preflight=preflight,
    )
    return _PlannedFile(
        plan=plan,
        file=file,
        accepted=accepted,
        change_set=change_set,
    )


def plan_vocabulary(cx: Cruxible, path: str | Path | None = None) -> VocabularyPlan:
    """What applying the file would change; writes nothing."""

    return _plan(cx, path).plan


#: Preflight refusals that mean a definition the file edits moved since its export.
_MOVED_CODES = frozenset(
    {
        "cruxible.authoring.claim_type_succession_invalid",
        "cruxible.authoring.subject_kind_predecessor_moved",
    }
)

VocabularySurface = Literal["sdk", "cli", "mcp"]


def _moved_since_export(cx: Cruxible, planned: _PlannedFile) -> tuple[str, ...]:
    """The definitions the file replaces or retires whose head differs from its export.

    Read only on a refused apply: one get per edited definition, at the head.
    """

    moved: list[str] = []
    for entry in planned.plan.changes:
        if entry.action not in ("replace", "retire"):
            continue
        kind, _, name = entry.definition.partition(":")
        exported_type = planned.accepted.claim_types.get(name)
        exported_kind = planned.accepted.subject_kinds.get(name)
        try:
            batch = cx._client.get_batch(
                cx._instance_id,
                request=GetBatchRequest(
                    refs=(entry.definition,),
                    evaluation_time=datetime.fromisoformat(cx._evaluation_time()),
                ),
            )
            envelope = (batch.results[0].proof or {}).get("envelope")
            if kind == "ClaimType":
                head_digest = claim_type_digest(ClaimType.model_validate(envelope)).tagged
                was = None if exported_type is None else claim_type_digest(exported_type).tagged
            else:
                head_digest = subject_kind_digest(SubjectKind.model_validate(envelope)).tagged
                was = None if exported_kind is None else subject_kind_digest(exported_kind).tagged
        except (CoreError, ValidationError, ValueError):
            moved.append(entry.definition)  # gone from the head
            continue
        if head_digest != was:
            moved.append(entry.definition)
    return tuple(moved)


def _moved_repair(surface: VocabularySurface) -> str:
    """Re-export over the moved definitions, keeping the edit, in the surface's own calls."""

    if surface == "cli":
        export, plan, apply = (
            "cruxible vocabulary export --force",
            "cruxible vocabulary plan",
            "cruxible vocabulary apply",
        )
    elif surface == "mcp":
        export, plan, apply = (
            "cruxible_vocabulary_export(force=true)",
            "cruxible_vocabulary_plan()",
            "cruxible_vocabulary_apply()",
        )
    else:
        export, plan, apply = (
            "cx.vocabulary.export(force=True)",
            "cx.vocabulary.plan()",
            "cx.vocabulary.apply()",
        )
    return (
        f"Save your edits to {VOCABULARY_FILE_PATH} elsewhere, run {export} to read the "
        f"definitions as they are now, redo your edits, check them with {plan}, then {apply}"
    )


def apply_vocabulary(
    cx: Cruxible,
    path: str | Path | None = None,
    *,
    rationale: str | None = None,
    accept: Literal["if_allowed", "never"] = "if_allowed",
    surface: VocabularySurface = "sdk",
) -> VocabularyApplyResult:
    """Submit the file's changes as one ordinary change set, at the export's coordinate.

    Each edited field's successor, and each edited SubjectKind, pins the digest
    the export read, so a definition someone changed since then refuses at
    preflight; the result then names each such definition (``moved``) and
    ``next`` says how to redo the edit over the current definitions.
    """

    planned = _plan(cx, path, rationale=rationale, preview=False)
    if planned.change_set is None:
        return VocabularyApplyResult(status="unchanged", plan=planned.plan)
    pinned = cx.at(planned.file.based_on.coordinate)
    lowered = lower_authoring_input(planned.change_set)
    assert isinstance(lowered, ChangeSetAuthoringPayload)
    draft = pinned.changes(rationale=planned.change_set.rationale)
    for member in lowered.members:
        draft._member(member, {"kind": "vocabulary_file", "member": member.model_dump(mode="json")})
    intent = draft.submit(accept=write_accept(accept))
    preflight = intent._preflight
    plan = planned.plan.model_copy(
        update={
            "preflight": VocabularyPreflight(
                verdict="not_run" if preflight is None else preflight.verdict,
                frontier=None if preflight is None else preflight.frontier,
                lint=None if preflight is None else preflight.lint,
            )
        }
    )
    if intent.refused:
        assert preflight is not None
        codes = {
            item.code
            for item in (() if preflight.frontier is None else preflight.frontier.diagnostics)
        }
        moved = _moved_since_export(cx, planned) if codes & _MOVED_CODES else ()
        return VocabularyApplyResult(
            status="refused",
            plan=plan,
            frontier=preflight.frontier,
            moved=moved,
            next=_moved_repair(surface) if moved else None,
        )
    outcome = intent.outcome
    status: Literal["landed", "proposed"] = (
        "landed" if outcome is not None and outcome.status == "landed" else "proposed"
    )
    return VocabularyApplyResult(
        status=status,
        plan=plan,
        outcome=outcome,
        next="cruxible vocabulary export" if status == "landed" else None,
    )


# -- the file's baseline, for next and orient --------------------------------------------


def workspace_vocabulary_baseline(workspace: str | Path) -> str | bool | None:
    """The Git OID the workspace's vocabulary file was exported at.

    None when there is no file; False when the file exists but its header
    cannot be read. Reads only the header lines.
    """

    root = Path(workspace).expanduser().resolve()
    if workspace_directory_conflict(root) is not None:
        return None
    try:
        content = read_regular_file(root / VOCABULARY_FILE_PATH)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    baseline = read_vocabulary_header(content)
    return False if baseline is None else baseline.git_oid


def vocabulary_file_observation(workspace: str | Path) -> dict[str, object] | None:
    """The ``vocabulary_file`` part of a next workspace observation, or None for no file."""

    baseline = workspace_vocabulary_baseline(workspace)
    if baseline is None:
        return None
    return {
        "tag": "playbill-next-vocabulary-file-v1",
        "git_oid": baseline if isinstance(baseline, str) else None,
    }


class Vocabulary:
    """The workspace's vocabulary file: ``export``, ``plan`` and ``apply``.

    Next: ``cx.vocabulary.export()``, edit ``.cruxible/vocabulary.yaml``, then
    ``cx.vocabulary.plan()`` and ``cx.vocabulary.apply()``.
    """

    def __init__(self, cx: Cruxible) -> None:
        self._cx = cx

    def export(self, *, force: bool = False) -> VocabularyExportResult:
        """Write the accepted vocabulary to ``.cruxible/vocabulary.yaml``.

        Shorthand where it says the same thing, the full ClaimType input form
        otherwise; every entry is checked by lowering it back. Refuses to
        replace a file holding changes not in accepted state unless ``force``.
        Next: edit the file, then ``cx.vocabulary.plan()``.
        """

        return export_vocabulary(self._cx, force=force)

    def plan(self, path: str | Path | None = None) -> VocabularyPlan:
        """What applying the file would add, replace and retire; writes nothing.

        Reads at the export the file names. ``claims`` lists the Claims an enum
        mapping moves; ``preflight`` is the daemon's write-free check.
        Next: ``cx.vocabulary.apply()``.
        """

        return plan_vocabulary(self._cx, path)

    def apply(
        self,
        path: str | Path | None = None,
        *,
        rationale: str | None = None,
        accept: Literal["if_allowed", "never"] = "if_allowed",
    ) -> VocabularyApplyResult:
        """Submit the file's changes as one change set; lands when policy allows.

        Next: ``cx.vocabulary.export()`` to move the file's baseline to what landed.
        """

        return apply_vocabulary(self._cx, path, rationale=rationale, accept=accept)


def _connected(client: CruxibleClient, instance_id: str, workspace: str | Path) -> Cruxible:
    from cruxible_client.authoring.sdk import Cruxible

    return Cruxible._from_client(client, instance_id=instance_id, workspace=Path(workspace))


def vocabulary_export_for(
    client: CruxibleClient, instance_id: str, *, workspace: str | Path, force: bool = False
) -> VocabularyExportResult:
    """``export`` over an open client: the CLI's and MCP server's entry point."""

    return export_vocabulary(_connected(client, instance_id, workspace), force=force)


def vocabulary_plan_for(
    client: CruxibleClient,
    instance_id: str,
    *,
    workspace: str | Path,
    path: str | Path | None = None,
) -> VocabularyPlan:
    """``plan`` over an open client: the CLI's and MCP server's entry point."""

    return plan_vocabulary(_connected(client, instance_id, workspace), path)


def vocabulary_apply_for(
    client: CruxibleClient,
    instance_id: str,
    *,
    workspace: str | Path,
    path: str | Path | None = None,
    rationale: str | None = None,
    accept: Literal["if_allowed", "never"] = "if_allowed",
    surface: VocabularySurface = "cli",
) -> VocabularyApplyResult:
    """``apply`` over an open client: the CLI's and MCP server's entry point."""

    return apply_vocabulary(
        _connected(client, instance_id, workspace),
        path,
        rationale=rationale,
        accept=accept,
        surface=surface,
    )


__all__ = [
    "AcceptedVocabulary",
    "Vocabulary",
    "apply_vocabulary",
    "entry_input",
    "export_vocabulary",
    "lower_entry",
    "plan_vocabulary",
    "read_accepted_vocabulary",
    "render_vocabulary_file",
    "vocabulary_apply_for",
    "vocabulary_export_for",
    "vocabulary_file_observation",
    "vocabulary_plan_for",
    "workspace_vocabulary_baseline",
]
