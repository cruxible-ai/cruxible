"""The vocabulary file: one YAML file that states an instance's kinds and fields.

``.cruxible/vocabulary.yaml`` is an authoring input, never the authority: the
signed ledger stays the record. ``cruxible vocabulary export`` writes it from
accepted state (and is its only writer); ``plan`` diffs it against the export it
names in ``based_on``; ``apply`` submits that difference as one ordinary change
set. A kind or field the file leaves out is untouched; only ``retired: true``
retires.

A field is one :class:`~cruxible_client.contracts.claim_type_inputs.ClaimTypeInputRecord`
(its predicate is ``<kind>.<field>``) plus five shorthands:

- ``enum``: the members, as a list, or as ``{member: meaning}`` (``null`` for no
  meaning). A member written ``{becomes: other}`` leaves the enum and its live
  Claims are said again with ``other``; ``{retire: <reason>}`` leaves the enum
  and its live Claims retire.
- ``link``: a Subject-valued field: ``link: <kind>`` or ``link: [<kind>, ...]``.
- ``many``: ``true`` for a many-valued field (left out: one, or an edit keeps
  what is accepted).
- ``key``: ``true`` marks a key of the kind, in file order.
- ``retired``: ``true`` retires the field and its live Claims (``reason``
  names why; default ``superseded``).

A kind states its SubjectKind with ``description``, ``minted``, ``keys`` (the
full form, when its keys are not its own fields), ``uniqueness``,
``normalizers`` and ``retired``; a kind with only ``fields`` declares no
SubjectKind change.

The loader is strict: a duplicate key, an unknown key, an alias, a merge key, a
float, a date, a ``yes``/``no`` style boolean or an empty value refuses, with
its line.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    model_validator,
)

from cruxible_client.contracts import AuthoringLanding, ClaimTypeProposalLint
from cruxible_client.contracts.authoring.models import DiagnosticFrontier
from cruxible_client.contracts.claim_type_inputs import ClaimTypeInputRecord
from cruxible_client.contracts.claims import ClaimRetirementReason
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.identity_normalizers import KeyNormalizer
from cruxible_client.contracts.projection import AcceptedCoordinate

#: The format tag the file's ``format`` key carries.
VOCABULARY_FILE_FORMAT: Final = "cruxible-vocabulary-v1"

#: The shorthand keys a field entry may state beside its ClaimType input fields.
FIELD_SHORTHANDS: Final = ("key", "enum", "link", "many", "retired", "reason")

#: The keys a field entry may not state: the predicate is ``<kind>.<field>``.
_FIELD_FORBIDDEN = ("predicate",)


class VocabularyFileError(FormatError):
    """The vocabulary file cannot be read, or says something it cannot mean."""

    error_code = "cruxible.vocabulary.file_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.error_code = code or type(self).error_code
        super().__init__(f"{self.error_code}: {message}")


class VocabularyBaselineError(VocabularyFileError):
    """The export the file names is not one this instance can read."""

    error_code = "cruxible.vocabulary.baseline_invalid"


class VocabularyExportRefused(VocabularyFileError):
    """Export would lose edits, or cannot write the accepted vocabulary faithfully."""

    error_code = "cruxible.vocabulary.export_refused"


class VocabularyApplyRefused(VocabularyFileError):
    """The file's changes cannot be submitted as they stand."""

    error_code = "cruxible.vocabulary.apply_refused"


# -- strict YAML ------------------------------------------------------------------------

_PLAIN_INT = re.compile(r"-?(?:0|[1-9][0-9]*)")


def _where(node: yaml.Node) -> str:
    mark = node.start_mark
    return f"line {mark.line + 1}, column {mark.column + 1}"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses whatever a reader could take two ways."""

    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node | None:
        event = cast(Any, self).peek_event()
        if isinstance(event, yaml.AliasEvent):
            raise VocabularyFileError(
                f"an alias (*{event.anchor}) at line {event.start_mark.line + 1}: write the "
                "value out"
            )
        anchor = getattr(event, "anchor", None)
        if anchor is not None:
            raise VocabularyFileError(
                f"an anchor (&{anchor}) at line {event.start_mark.line + 1}: write the value out"
            )
        return super().compose_node(parent, index)  # type: ignore[arg-type]

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise VocabularyFileError(f"expected a mapping at {_where(node)}")
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise VocabularyFileError(
                    f"a merge key (<<) at {_where(key_node)}: write the keys out"
                )
            key = cast(Any, self).construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise VocabularyFileError(
                    f"the key {key_node.value!r} at {_where(key_node)} is not text: quote it"
                )
            if key in mapping:
                raise VocabularyFileError(f"the key {key!r} appears twice ({_where(key_node)})")
            mapping[key] = cast(Any, self).construct_object(value_node, deep=deep)
        return mapping


def _strict_bool(loader: _StrictLoader, node: yaml.ScalarNode) -> bool:
    if node.value not in ("true", "false"):
        raise VocabularyFileError(
            f"{node.value!r} at {_where(node)} reads as a boolean: write true or false, or quote it"
        )
    return bool(node.value == "true")


def _strict_int(loader: _StrictLoader, node: yaml.ScalarNode) -> int:
    if not _PLAIN_INT.fullmatch(node.value):
        raise VocabularyFileError(
            f"{node.value!r} at {_where(node)} reads as a number: write plain digits, or quote it"
        )
    return int(node.value)


def _no_float(loader: _StrictLoader, node: yaml.ScalarNode) -> float:
    raise VocabularyFileError(
        f"{node.value!r} at {_where(node)} reads as a decimal number: quote it"
    )


def _no_timestamp(loader: _StrictLoader, node: yaml.ScalarNode) -> object:
    raise VocabularyFileError(f"{node.value!r} at {_where(node)} reads as a date: quote it")


def _strict_null(loader: _StrictLoader, node: yaml.ScalarNode) -> None:
    if node.value != "null":
        shown = "an empty value" if node.value == "" else repr(node.value)
        raise VocabularyFileError(
            f"{shown} at {_where(node)} reads as null: write null, or a value"
        )
    return None


#: Plain text a YAML 1.2 reader takes as a number though this one reads text.
_YAML12_NUMBER = re.compile(
    r"[-+]?0o[0-7]+"  # 1.2 octal
    r"|[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+"  # 1.2 scientific
)


def _strict_str(loader: _StrictLoader, node: yaml.ScalarNode) -> str:
    if node.style is None and _YAML12_NUMBER.fullmatch(node.value):
        raise VocabularyFileError(
            f"{node.value!r} at {_where(node)} reads as a number to some readers: quote it"
        )
    return str(node.value)


_StrictLoader.add_constructor("tag:yaml.org,2002:str", _strict_str)
_StrictLoader.add_constructor("tag:yaml.org,2002:bool", _strict_bool)
_StrictLoader.add_constructor("tag:yaml.org,2002:int", _strict_int)
_StrictLoader.add_constructor("tag:yaml.org,2002:float", _no_float)
_StrictLoader.add_constructor("tag:yaml.org,2002:timestamp", _no_timestamp)
_StrictLoader.add_constructor("tag:yaml.org,2002:null", _strict_null)


def load_strict_yaml(content: bytes | str) -> object:
    """Parse YAML, refusing every spelling a reader could take two ways."""

    text = content.decode("utf-8") if isinstance(content, bytes) else content
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except VocabularyFileError:
        raise
    except yaml.YAMLError as exc:
        raise VocabularyFileError(f"not valid YAML: {exc}") from exc


# -- the file ---------------------------------------------------------------------------


class _StrictFileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VocabularyBaseline(_StrictFileModel):
    """The export a file was written from: its accepted coordinate and vocabulary digest."""

    git_oid: StrictStr
    semantic_root: StrictStr
    generation_root: StrictStr
    compiler_digest: StrictStr
    #: sha256 over the accepted digest of every live ClaimType and SubjectKind.
    vocabulary_digest: StrictStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _coordinate_shape(self) -> VocabularyBaseline:
        # Each coordinate field takes the accepted coordinate's own spelling,
        # so a malformed header reads as invalid instead of reaching a request.
        self.coordinate  # noqa: B018 - validates
        return self

    @property
    def coordinate(self) -> AcceptedCoordinate:
        return AcceptedCoordinate(
            git_oid=self.git_oid,
            semantic_root=self.semantic_root,
            generation_root=self.generation_root,
            compiler_digest=self.compiler_digest,
        )


class VocabularyMemberMapping(_StrictFileModel):
    """What an enum member leaving the enum does to its live Claims: exactly one of these."""

    becomes: StrictStr | None = None
    retire: ClaimRetirementReason | None = None

    @model_validator(mode="after")
    def _one(self) -> VocabularyMemberMapping:
        if (self.becomes is None) == (self.retire is None):
            raise ValueError("a leaving member states becomes: <member> or retire: <reason>")
        return self


EnumMeaning = StrictStr | None | VocabularyMemberMapping


class VocabularyField(_StrictFileModel):
    """One field: its shorthands, and the ClaimType input fields it states in full."""

    key: StrictBool | None = None
    enum: dict[str, EnumMeaning] | tuple[StrictStr, ...] | None = None
    link: StrictStr | tuple[StrictStr, ...] | None = None
    many: StrictBool | None = None
    retired: StrictBool | None = None
    reason: ClaimRetirementReason | None = None
    #: The ClaimType input fields this entry states, as written (validated on read).
    stated: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("a field is a mapping of its properties ({} for every default)")
        shorthands = {key: value[key] for key in FIELD_SHORTHANDS if key in value}
        stated = {key: item for key, item in value.items() if key not in FIELD_SHORTHANDS}
        for key in _FIELD_FORBIDDEN:
            if key in stated:
                raise ValueError(f"{key} is not stated: a field's predicate is <kind>.<field>")
        unknown = sorted(set(stated) - set(ClaimTypeInputRecord.model_fields))
        if unknown:
            raise ValueError(
                f"unknown field propert{'y' if len(unknown) == 1 else 'ies'} "
                f"{', '.join(unknown)}; a field takes {', '.join(FIELD_SHORTHANDS)} or a "
                "ClaimType input field"
            )
        return {**shorthands, "stated": stated}

    @model_validator(mode="after")
    def _consistent(self) -> VocabularyField:
        stated = self.stated
        if self.enum is not None and self.link is not None:
            raise ValueError("a field is an enum or a link, not both")
        if (self.enum is not None or self.link is not None) and "value" in stated:
            raise ValueError("enum and link state the value; drop value")
        if self.enum is not None and "member_descriptions" in stated:
            raise ValueError("enum states the member meanings; drop member_descriptions")
        if self.many is not None and "cardinality" in stated:
            raise ValueError("many states the cardinality; drop cardinality")
        if self.key and self.many:
            raise ValueError("a key holds one value; drop many: true")
        if self.reason is not None and not self.retired:
            raise ValueError("reason goes with retired: true")
        if isinstance(self.enum, dict) and not any(
            not isinstance(item, VocabularyMemberMapping) for item in self.enum.values()
        ):
            raise ValueError("an enum keeps at least one member")
        # Type-check the full form now, so a bad property names its field here.
        ClaimTypeInputRecord.model_validate({"predicate": "vocabulary.file", **stated})
        return self


class VocabularyKind(_StrictFileModel):
    """One kind: its SubjectKind decisions and its fields."""

    description: StrictStr | None = None
    minted: StrictBool | None = None
    keys: tuple[StrictStr, ...] | None = None
    uniqueness: Literal["unique", "hint"] | None = None
    normalizers: dict[str, KeyNormalizer] | None = None
    retired: StrictBool | None = None
    fields: dict[str, VocabularyField] = Field(default_factory=dict)

    @property
    def states_subject_kind(self) -> bool:
        """Whether this entry says anything about the kind's SubjectKind."""

        own = self.model_fields_set - {"fields"}
        return bool(own) or any(entry.key for entry in self.fields.values())


class VocabularyFile(_StrictFileModel):
    """The whole file, as read."""

    format: Literal["cruxible-vocabulary-v1"]
    instance: StrictStr
    based_on: VocabularyBaseline
    kinds: dict[str, VocabularyKind] = Field(default_factory=dict)


def _validation_message(error: ValidationError) -> str:
    details = []
    for item in error.errors(include_url=False):
        path = ".".join(str(part) for part in item["loc"])
        details.append(f"{path or '(file)'}: {item['msg']}")
    return "; ".join(details)


def load_vocabulary_file(content: bytes | str) -> VocabularyFile:
    """Read one vocabulary file strictly; every refusal names where."""

    data = load_strict_yaml(content)
    if not isinstance(data, dict):
        raise VocabularyFileError("the file is a mapping with format, instance, based_on, kinds")
    try:
        return VocabularyFile.model_validate(data)
    except ValidationError as exc:
        raise VocabularyFileError(_validation_message(exc)) from exc


def read_vocabulary_header(content: bytes | str) -> VocabularyBaseline | None:
    """The ``based_on`` header alone, read from the lines before ``kinds:``; None if unreadable.

    Cheap by construction: export writes the header first, so this parses only
    those lines, never the kinds.
    """

    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    head: list[str] = []
    for line in text.splitlines():
        if line.startswith("kinds:"):
            break
        head.append(line)
    try:
        data = load_strict_yaml("\n".join(head))
        if not isinstance(data, dict) or data.get("format") != VOCABULARY_FILE_FORMAT:
            return None
        return VocabularyBaseline.model_validate(data.get("based_on"))
    except (VocabularyFileError, ValidationError):
        return None


# -- results ----------------------------------------------------------------------------


class _StrictResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VocabularyExportResult(_StrictResultModel):
    """What ``vocabulary export`` wrote."""

    tag: Literal["cruxible-vocabulary-export-v1"] = "cruxible-vocabulary-export-v1"
    path: str
    status: Literal["written", "unchanged"]
    based_on: VocabularyBaseline
    kinds: int = Field(ge=0)
    fields: int = Field(ge=0)


VocabularyAction = Literal["add", "replace", "retire", "not_in_file"]


class VocabularyPlanEntry(_StrictResultModel):
    """One definition the file adds, replaces or retires, or one it does not name."""

    definition: str = Field(description="ClaimType:<predicate> or SubjectKind:<kind>.")
    action: VocabularyAction
    #: The properties that differ from the accepted definition (replace only).
    changed: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    #: The installed kit that owns the accepted definition.
    kit: str | None = Field(default=None, exclude_if=lambda value: value is None)
    #: A retired field: how many live Claims retire with it (at the head).
    live_claims: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)


class VocabularyClaimChange(_StrictResultModel):
    """One live Claim an enum mapping says again, or retires."""

    claim: str
    subject: str
    field: str
    value: str
    action: Literal["re_author", "retire"]
    becomes: str | None = Field(default=None, exclude_if=lambda value: value is None)
    reason: ClaimRetirementReason | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    #: The Capture the re-authored Claim cites again.
    capture: str | None = Field(default=None, exclude_if=lambda value: value is None)


class VocabularyPreflight(_StrictResultModel):
    """The daemon's write-free preflight of the change set, or why it did not run."""

    verdict: Literal["passed", "refused", "not_run"]
    frontier: DiagnosticFrontier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    lint: ClaimTypeProposalLint | None = Field(default=None, exclude_if=lambda value: value is None)
    note: str | None = Field(default=None, exclude_if=lambda value: value is None)


class VocabularyPlan(_StrictResultModel):
    """What applying the file would change, against the export it names.

    ``claims`` comes first: the live Claims an enum mapping re-authors or
    retires. ``entries`` lists each definition added, replaced or retired, then
    each one the instance holds that the file does not name (informational:
    left out is never retired).
    """

    tag: Literal["cruxible-vocabulary-plan-v1"] = "cruxible-vocabulary-plan-v1"
    path: str
    based_on: VocabularyBaseline
    claims: tuple[VocabularyClaimChange, ...] = ()
    entries: tuple[VocabularyPlanEntry, ...] = ()
    preflight: VocabularyPreflight

    @property
    def changes(self) -> tuple[VocabularyPlanEntry, ...]:
        """The entries apply would submit (every entry but ``not_in_file``)."""

        return tuple(entry for entry in self.entries if entry.action != "not_in_file")


class VocabularyApplyResult(_StrictResultModel):
    """What ``vocabulary apply`` submitted, and how it landed."""

    tag: Literal["cruxible-vocabulary-apply-v1"] = "cruxible-vocabulary-apply-v1"
    status: Literal["landed", "proposed", "refused", "unchanged"]
    plan: VocabularyPlan
    outcome: AuthoringLanding | None = Field(default=None, exclude_if=lambda value: value is None)
    frontier: DiagnosticFrontier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    next: str | None = Field(default=None, exclude_if=lambda value: value is None)


__all__ = [
    "FIELD_SHORTHANDS",
    "VOCABULARY_FILE_FORMAT",
    "VocabularyAction",
    "VocabularyApplyRefused",
    "VocabularyApplyResult",
    "VocabularyBaseline",
    "VocabularyBaselineError",
    "VocabularyClaimChange",
    "VocabularyExportRefused",
    "VocabularyExportResult",
    "VocabularyField",
    "VocabularyFile",
    "VocabularyFileError",
    "VocabularyKind",
    "VocabularyMemberMapping",
    "VocabularyPlan",
    "VocabularyPlanEntry",
    "VocabularyPreflight",
    "load_strict_yaml",
    "load_vocabulary_file",
    "read_vocabulary_header",
]
