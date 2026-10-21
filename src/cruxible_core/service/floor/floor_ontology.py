"""The ontology on the floor: ``ontology/`` files describing the accepted definitions.

Every file is daemon-rendered from accepted definitions alone (no population
counts, no clock), so a coordinate fixes it:

- ``ontology/INDEX``: one row per kind a live SubjectKind or live ClaimType
  names: identity rule, field count, owner, description's first sentence;
- ``ontology/kinds/<kind>.md``: one card per such kind: its identity, then one
  section per live field (signature line first, so a grep hit on it describes
  itself, as a ``current/`` value line does), who settles it, its description,
  and the live Subject-valued fields of other kinds that point at it;
- ``ontology/claim-types/INDEX``: one row per live ClaimType;
- ``ontology/contracts/INDEX`` and ``ontology/contracts/<id>.md``: each live
  CaptureContract and the live ClaimTypes that admit or require it;
- ``ontology/queries/INDEX``: each live QueryDefinition's parameters and result;
- ``ontology/lines/INDEX``: each live Line, the live Triggers aimed at it and
  the live Mandates on its Procedure (enablement is operational: ``orient``);
- ``ontology/kits/INDEX``: each installed kit from its receipt.

``owner`` is ``kit=<id>`` (a receipt owns the path), ``kit=<id> edited`` (it
does, and the held definition differs from the installed content), or
``local``. Carried definitions are pinned, not owned, so they show ``local``.

Stamps. A file's ``changed_at`` is the latest sequence of any definition that
ever reached it (``floor_definitions``: each revision's reach is a function of
its own content), where a definition's own sequence also counts the receipts
and Mandates that ever annotated it (owner, settled by). A kind card also reads
the short-name shadows of its fields, and ``ontology/INDEX`` when each
ClaimType's kinds last changed. Every stamp is a function of the generation
alone, so a delta's integrity check holds. A generation that touches no
definition renders and delivers no ontology bytes; one that touches a
definition re-renders only the files that definition (or its annotators) ever
reached: O(changed definitions), with INDEX files re-rendered whole when one of
their rows' inputs moves, the format's accepted cost.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from cruxible_client.contracts.builtin_claim_types import concrete_subject_kinds
from cruxible_client.contracts.captures import CaptureContract
from cruxible_client.contracts.claim_types import (
    ClaimType,
    ClaimTypeFormatError,
    claim_type_contention,
    claim_type_path,
    effective_evidence_requirement,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.kits import KitInstalledArtifact, KitReceipt, kit_content_digest
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionRule
from cruxible_client.contracts.procedure_mandates import ProcedureMandate
from cruxible_client.contracts.procedures.line_specs import LineSpec
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.subject_kinds import SubjectKind, SubjectKindKeys
from cruxible_client.contracts.triggers import LineTarget, Trigger
from cruxible_core.derived.persistent import EMPTY_PCOUNTS, EMPTY_PSET, PCounts, PMap, PSet
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.floor.floor_content import member_ref
from cruxible_core.service.floor.floor_current import stamp_text
from cruxible_core.service.floor.floor_definitions import (
    CAPTURE_CONTRACTS_PREFIX,
    CLAIM_TYPES_PREFIX,
    CONTRACTS_VIEW,
    CURRENT_VIEW,
    KINDS_VIEW,
    LINES_PREFIX,
    MANDATES_PREFIX,
    RECEIPTS_PREFIX,
    TRIGGERS_PREFIX,
    DefinitionRevision,
    reach_moves,
    view_changed_at,
)

ONTOLOGY_PREFIX = "ontology/"
ONTOLOGY_INDEX = "ontology/INDEX"
CLAIM_TYPES_INDEX = "ontology/claim-types/INDEX"
CONTRACTS_INDEX = "ontology/contracts/INDEX"
QUERIES_INDEX = "ontology/queries/INDEX"
LINES_INDEX = "ontology/lines/INDEX"
KITS_INDEX = "ontology/kits/INDEX"
KINDS_DIRECTORY = "ontology/kinds/"
CONTRACTS_DIRECTORY = "ontology/contracts/"
INDEX_FILES: tuple[str, ...] = (
    ONTOLOGY_INDEX,
    CLAIM_TYPES_INDEX,
    CONTRACTS_INDEX,
    QUERIES_INDEX,
    LINES_INDEX,
    KITS_INDEX,
)
_LOCAL = "local"
_DESCRIPTION_CHARS = 160


def kind_card_path(kind: str) -> str:
    return f"{KINDS_DIRECTORY}{kind}.md"


def contract_card_path(contract_id: str) -> str:
    return f"{CONTRACTS_DIRECTORY}{contract_id}.md"


def _is_file(target: str) -> bool:
    return target.startswith(ONTOLOGY_PREFIX)


# -- names and reach ------------------------------------------------------------------


ContractNamer = Callable[[str], str | None]
"""A CaptureContract digest -> the identity it is a version of (immutable once accepted)."""


def admitted_contracts(claim_type: ClaimType, names: ContractNamer) -> tuple[str, ...]:
    """The CaptureContract names a ClaimType's evidence rules admit, in byte order.

    A v6 rule names contracts by identity; an older one names versions by
    digest, each a version of one identity forever, so the name a digest
    resolves to never changes once its version is accepted (an admitted pin
    always is). A digest that resolves to nothing shows ``unresolved:<12 hex>``.
    """

    found: set[str] = set()
    for rule in claim_type.evidence_admission_policy.rules:
        if isinstance(rule, ClaimEvidenceAdmissionRule):
            found.update(item.target.name for item in rule.capture_contracts)
        else:
            for digest in rule.capture_contract_digests:
                name = names(digest)
                found.add(
                    name if name is not None else f"unresolved:{digest.rpartition(':')[2][:12]}"
                )
    return tuple(sorted(found, key=lambda item: item.encode()))


def _kinds_of(claim_type: ClaimType) -> tuple[str, ...]:
    """The kind cards a ClaimType shows on: its Subjects' kinds and its objects' kinds."""

    return tuple(
        dict.fromkeys(
            (
                *concrete_subject_kinds(claim_type.allowed_subject_kinds),
                *concrete_subject_kinds(claim_type.allowed_object_subject_kinds),
            )
        )
    )


def ontology_reach(names: ContractNamer) -> Callable[[str, Any], frozenset[str]]:
    """Each definition revision's reach: the ontology files and definitions it shows on.

    A function of the revision's own content (and immutable contract names),
    so a path's ever-reached targets are a function of its history alone.
    Mandates and receipts reach definition paths: the files those show on
    carry the annotation (settled by, owner).
    """

    def reach(path: str, value: Any) -> frozenset[str]:
        if isinstance(value, ClaimType):
            contracts = [
                name
                for name in admitted_contracts(value, names)
                if not name.startswith("unresolved:")
            ]
            return frozenset(
                (
                    CLAIM_TYPES_INDEX,
                    *(kind_card_path(kind) for kind in _kinds_of(value)),
                    *(contract_card_path(name) for name in contracts),
                    *((CONTRACTS_INDEX,) if contracts else ()),
                )
            )
        if isinstance(value, SubjectKind):
            return frozenset((ONTOLOGY_INDEX, kind_card_path(value.subject_kind)))
        if isinstance(value, CaptureContract):
            return frozenset((CONTRACTS_INDEX, contract_card_path(value.identity.name)))
        if isinstance(value, KitReceipt):
            return frozenset(
                (KITS_INDEX, *(entry.path for entry in (*value.artifacts, *value.carried)))
            )
        if isinstance(value, QueryDefinition):
            return frozenset((QUERIES_INDEX,))
        if isinstance(value, LineSpec):
            return frozenset((LINES_INDEX,))
        if isinstance(value, Trigger):
            return (
                frozenset((LINES_INDEX,)) if isinstance(value.target, LineTarget) else frozenset()
            )
        if path.startswith(MANDATES_PREFIX):
            scoped: list[str] = []
            if isinstance(value, ProcedureMandate):
                for item in value.scope:
                    try:
                        scoped.append(claim_type_path(item.claim_type.target.name))
                    except ClaimTypeFormatError:
                        continue
            return frozenset((LINES_INDEX, *scoped))
        return frozenset()

    return reach


# -- the reverse indexes ---------------------------------------------------------------


@dataclass(frozen=True)
class OntologyIndex:
    """The reverse indexes a definition change is traced through, persistent.

    ``sources``: ontology file -> every definition path whose history ever
    reached it. ``annotators``: definition path -> the receipts and Mandates
    whose history ever reached it. ``shadows``: a short string -> the live
    ClaimType paths whose field shows it on some kind (``<kind>.<short>``).
    ``kinds_at``: the sequence at which each ClaimType's kinds last changed.
    """

    sources: PMap[str, PSet[str]] = field(default_factory=PMap)
    annotators: PMap[str, PSet[str]] = field(default_factory=PMap)
    shadows: PMap[str, PSet[str]] = field(default_factory=PMap)
    kinds_at: PCounts = EMPTY_PCOUNTS


def _shorts(claim_type: Any) -> Iterator[str]:
    """The short strings a live ClaimType's field shows on its Subjects' kinds."""

    if not isinstance(claim_type, ClaimType) or claim_type.lifecycle.state != "live":
        return
    for kind in concrete_subject_kinds(claim_type.allowed_subject_kinds):
        prefix = f"{kind}."
        if claim_type.predicate.startswith(prefix) and len(claim_type.predicate) > len(prefix):
            yield claim_type.predicate[len(prefix) :]


def _add(index: Any, key: str, member: str) -> None:
    index[key] = index.get(key, EMPTY_PSET).add(member)


def _remove(index: Any, key: str, member: str) -> None:
    members = index.get(key, EMPTY_PSET).remove(member)
    if members:
        index[key] = members
    else:
        index.discard(key)


def patch_ontology_index(
    index: OntologyIndex,
    moves: Iterable[tuple[str, DefinitionRevision | None, DefinitionRevision | None]],
) -> OntologyIndex:
    """``index`` with each ``(path, before, after)`` history move applied: O(the moves)."""

    sources = index.sources.evolver()
    annotators = index.annotators.evolver()
    shadows = index.shadows.evolver()
    kinds_at = index.kinds_at
    for path, before, after in moves:
        gained, lost = reach_moves(before, after)
        for target in lost:
            _remove(sources if _is_file(target) else annotators, target, path)
        for target in gained:
            _add(sources if _is_file(target) else annotators, target, path)
        if path.startswith(CLAIM_TYPES_PREFIX):
            for short in _shorts(None if before is None else before.value):
                _remove(shadows, short, path)
            for short in _shorts(None if after is None else after.value):
                _add(shadows, short, path)
            if before is not None:
                kinds_at = kinds_at.remove(before.view_at[KINDS_VIEW])
            if after is not None:
                kinds_at = kinds_at.add(after.view_at[KINDS_VIEW])
    return OntologyIndex(
        sources=sources.persistent(),
        annotators=annotators.persistent(),
        shadows=shadows.persistent(),
        kinds_at=kinds_at,
    )


def build_ontology_index(definitions: Mapping[str, DefinitionRevision]) -> OntologyIndex:
    return patch_ontology_index(
        OntologyIndex(), ((path, None, chain) for path, chain in definitions.items())
    )


# -- what a change reaches ---------------------------------------------------------------


@dataclass(frozen=True)
class OntologyState:
    """One generation's definitions with their reverse indexes, as the renderer reads them."""

    definitions: Mapping[str, DefinitionRevision]
    index: OntologyIndex
    names: ContractNamer

    def chain(self, path: str) -> DefinitionRevision | None:
        return self.definitions.get(path)

    def value(self, path: str) -> Any:
        chain = self.definitions.get(path)
        return None if chain is None else chain.value

    def sequence(self, path: str) -> int:
        chain = self.definitions.get(path)
        return 0 if chain is None else chain.sequence

    def effective(self, path: str) -> int:
        """A definition's latest sequence, counting the annotators that ever reached it."""

        best = self.sequence(path)
        for annotator in self.index.annotators.get(path, ()):
            best = max(best, self.sequence(annotator))
        return best

    def files_of(self, path: str) -> set[str]:
        chain = self.definitions.get(path)
        return set() if chain is None else {item for item in chain.reach if _is_file(item)}


def _type_path(predicate: str) -> str | None:
    try:
        return claim_type_path(predicate)
    except ClaimTypeFormatError:
        return None


def reached_files(old: OntologyState, new: OntologyState, changed: Iterable[str]) -> set[str]:
    """Every ontology file whose stamp could read a changed path, in either generation."""

    reached: set[str] = set()
    for path in changed:
        if path not in old.definitions and path not in new.definitions:
            continue
        claim_type = path.startswith(CLAIM_TYPES_PREFIX)
        before, after = old.chain(path), new.chain(path)
        # A ClaimType shows on the contract files only through what it admits.
        contracts = not claim_type or view_changed_at(before, CONTRACTS_VIEW) != view_changed_at(
            after, CONTRACTS_VIEW
        )
        for side in (old, new):
            reached.update(
                item
                for item in side.files_of(path)
                if contracts or not item.startswith(CONTRACTS_DIRECTORY)
            )
            chain = side.chain(path)
            if chain is not None:
                for target in chain.reach:
                    if not _is_file(target):
                        # An annotator: the files its targets show on carry it.
                        reached.update(side.files_of(target))
            annotators = side.index.annotators.get(path, ())
            if path.startswith(RECEIPTS_PREFIX) or any(
                item.startswith(RECEIPTS_PREFIX) for item in annotators
            ):
                reached.add(KITS_INDEX)
        if not claim_type:
            continue
        if view_changed_at(before, KINDS_VIEW) != view_changed_at(after, KINDS_VIEW):
            reached.add(ONTOLOGY_INDEX)
        if view_changed_at(before, CURRENT_VIEW) != view_changed_at(after, CURRENT_VIEW):
            # Its liveness renames the fields it shadows on every kind.
            short = _predicate(path)
            for side in (old, new):
                for shadowed in side.index.shadows.get(short, ()):
                    reached.update(
                        item for item in side.files_of(shadowed) if item.startswith(KINDS_DIRECTORY)
                    )
    return reached


def _predicate(type_path: str) -> str:
    namespace, _separator, name = type_path.removeprefix(CLAIM_TYPES_PREFIX).rpartition("/")
    return f"{namespace}.{name.removesuffix('.json')}"


def all_files(state: OntologyState) -> set[str]:
    """Every ontology file the state could show (cold): the INDEX files and every card."""

    return {*INDEX_FILES, *(path for path in state.index.sources if path not in INDEX_FILES)}


# -- reading the state -----------------------------------------------------------------------


def _live(value: Any) -> bool:
    lifecycle = getattr(value, "lifecycle", None)
    return lifecycle is not None and getattr(lifecycle, "state", None) == "live"


def _byte_sorted(items: Iterable[str]) -> list[str]:
    return sorted(items, key=lambda item: item.encode())


def _one_line(text: str, limit: int | None = None) -> str:
    line = " ".join(text.replace("\t", " ").split())
    if limit is not None and len(line) > limit:
        return line[: limit - 1].rstrip() + "…"
    return line


def _first_sentence(text: str | None) -> str:
    if not text:
        return "-"
    line = _one_line(text)
    head, separator, _rest = line.partition(". ")
    return _one_line(head + "." if separator else line, _DESCRIPTION_CHARS) or "-"


def _cell(text: str) -> str:
    """One TSV cell: tabs and line breaks never split a row."""

    return _one_line(text) or "-"


def _duration(microseconds: int) -> str:
    for unit, size in (("d", 86_400_000_000), ("h", 3_600_000_000), ("m", 60_000_000)):
        if microseconds and microseconds % size == 0:
            return f"{microseconds // size}{unit}"
    if microseconds % 1_000_000 == 0:
        return f"{microseconds // 1_000_000}s"
    return f"{microseconds}us"


def _time(value: Any) -> str:
    return str(value.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _ref(path: str) -> str:
    """How a definition path is named where ``get`` resolves it."""

    return member_ref(path)


class _Owners:
    """Owner labels at one generation, from the receipts that ever reached a path."""

    def __init__(self, state: OntologyState) -> None:
        self._state = state
        self._content: dict[int, str | None] = {}

    def entry(self, path: str) -> tuple[str, KitInstalledArtifact] | None:
        for annotator in _byte_sorted(self._state.index.annotators.get(path, ())):
            receipt = self._state.value(annotator)
            if not isinstance(receipt, KitReceipt):
                continue
            for entry in receipt.artifacts:
                if entry.path == path:
                    return receipt.kit_id, entry
        return None

    def _held_content(self, chain: DefinitionRevision) -> str | None:
        key = id(chain)
        if key not in self._content:
            self._content[key] = (
                None if chain.raw is None else kit_content_digest(json.loads(chain.raw))
            )
        return self._content[key]

    def edited(self, path: str, entry: KitInstalledArtifact) -> bool:
        chain = self._state.chain(path)
        if chain is None or chain.raw is None:
            return True
        held = self._held_content(chain)
        installed = entry.content_digest
        if installed is None:
            # A receipt from before content digests: the bytes it installed.
            from cruxible_core.claims.closure import parse_dependency_artifact

            state = parse_dependency_artifact(path, chain.raw)
            if state is None or state.artifact_digest != entry.installed_digest:
                return True
            return False
        return installed != held

    def label(self, path: str) -> str:
        found = self.entry(path)
        if found is None:
            return _LOCAL
        kit_id, entry = found
        return f"kit={kit_id} edited" if self.edited(path, entry) else f"kit={kit_id}"


def _live_values(state: OntologyState, paths: Iterable[str], kind: type) -> list[tuple[str, Any]]:
    found = []
    for path in paths:
        value = state.value(path)
        if isinstance(value, kind) and _live(value):
            found.append((path, value))
    return found


# -- one ClaimType as a card section and an INDEX row ------------------------------------------


def _value_type(claim_type: ClaimType) -> str:
    if claim_type.object_kind == "subject":
        kinds = claim_type.allowed_object_subject_kinds
        return "subject:" + ",".join(kinds) if kinds else "subject"
    if claim_type.object_kind == "exact_content":
        return "exact_content"
    schema: Mapping[str, Any] = claim_type.literal_schema or {}
    if "enum" in schema or "const" in schema:
        raw = schema["enum"] if "enum" in schema else [schema["const"]]
        members = raw if isinstance(raw, list) else [raw]
        return (
            "enum("
            + "|".join(str(item) if isinstance(item, str) else json.dumps(item) for item in members)
            + ")"
        )
    declared = schema.get("type")
    fmt = schema.get("format")
    if declared == "string" and fmt in ("date", "date-time"):
        return "date" if fmt == "date" else "datetime"
    if isinstance(declared, list):
        return "|".join(str(item) for item in declared)
    return str(declared) if declared is not None else "any"


def _roles(claim_type: ClaimType) -> str:
    roles = tuple(claim_type.permitted_roles)
    if len(roles) == 1:
        return f"role {roles[0]}"
    default = f" (default {claim_type.default_role})" if claim_type.default_role else ""
    return f"roles {'|'.join(roles)}{default}"


def _evidence(claim_type: ClaimType, names: ContractNamer) -> str:
    contracts = admitted_contracts(claim_type, names)
    requirement = effective_evidence_requirement(claim_type)
    shown = ", ".join(contracts) if contracts else "-"
    return shown if requirement == "self" else f"{shown} ({requirement})"


def _contention(claim_type: ClaimType) -> str:
    contention = claim_type_contention(claim_type)
    text = f"{contention.selector}/{contention.on_conflict}"
    if contention.corroboration:
        text += f" corroborate {contention.corroboration}"
    if contention.freeze:
        text += f" freeze {contention.freeze}"
    return text


def _stale_after(claim_type: ClaimType) -> str | None:
    freshness = claim_type.evidence_freshness
    return None if freshness is None else _duration(freshness.stale_after.microseconds)


def _settlers(state: OntologyState, path: str, predicate: str) -> list[str]:
    """The live settle Mandates whose typed scope pins this ClaimType, one line each."""

    lines = []
    for mandate_path in _byte_sorted(state.index.annotators.get(path, ())):
        mandate = state.value(mandate_path)
        if not isinstance(mandate, ProcedureMandate) or not _live(mandate):
            continue
        if mandate.grants != "settle":
            continue
        for item in mandate.scope:
            if item.claim_type.target.name != predicate:
                continue
            condition = (
                ""
                if mandate.condition is None
                else f"  condition query:{mandate.condition.query.target.name}"
            )
            lines.append(
                f"settled by: mandate {mandate.identity.name}  "
                f"Procedure:{mandate.procedure.target.name}  "
                f"settle {'|'.join(item.change_kinds)}{condition}  "
                f"valid {_time(mandate.valid_from)}..{_time(mandate.expires_at)}"
                + ("  suspended" if mandate.suspended else "")
            )
    return lines or ["settled by: review"]


def _description_lines(text: str | None) -> list[str]:
    """A description as prose lines; none can read as a section header."""

    if not text:
        return []
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.rstrip()
        lines.append("\\" + line if line.startswith("#") else line)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _field_section(
    state: OntologyState,
    owners: _Owners,
    path: str,
    claim_type: ClaimType,
    kind: str,
    live_predicates: Collection[str],
) -> list[str]:
    short = short_field_name(claim_type.predicate, kind, live_predicates)
    signature = [
        f"{short}: {_value_type(claim_type)}",
        claim_type.cardinality,
        _roles(claim_type),
        f"evidence {_evidence(claim_type, state.names)}",
    ]
    stale = _stale_after(claim_type)
    if stale is not None:
        signature.append(f"stale_after {stale}")
    signature.append(f"contention {_contention(claim_type)}")
    lines = [
        f"## {short}  {claim_type.predicate}  owner={owners.label(path)}",
        "  ".join(signature),
        *_settlers(state, path, claim_type.predicate),
        *_description_lines(claim_type.description),
        *(
            f"member {item.member}: {_one_line(item.description)}"
            for item in claim_type.member_descriptions
        ),
    ]
    return lines


def _claim_type_row(state: OntologyState, owners: _Owners, path: str, claim_type: ClaimType) -> str:
    return "\t".join(
        (
            claim_type.predicate,
            ",".join(claim_type.allowed_subject_kinds) or "-",
            _cell(_value_type(claim_type)),
            claim_type.cardinality,
            claim_type.default_role or "-",
            _cell(_evidence(claim_type, state.names)),
            _stale_after(claim_type) or "-",
            _contention(claim_type),
            owners.label(path),
        )
    )


# -- the files ------------------------------------------------------------------------------


def _header(title: str, count: int, noun: str, columns: str, changed_at: int) -> str:
    return f"# {title} INDEX  {count} {noun}  columns: {columns}  {stamp_text(changed_at)}"


def _render(lines: Iterable[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _identity(subject_kind: SubjectKind | None) -> str:
    if subject_kind is None:
        return "undeclared"
    rule = subject_kind.identity_rule
    if isinstance(rule, SubjectKindKeys):
        return f"keys: {', '.join(rule.keys)} ({rule.uniqueness})"
    return "minted"


class _Renderer:
    """Renders one generation's ontology files on demand, sharing its lookups."""

    def __init__(self, state: OntologyState) -> None:
        self.state = state
        self.owners = _Owners(state)
        self._live_predicates: frozenset[str] | None = None

    def live_predicates(self) -> frozenset[str]:
        if self._live_predicates is None:
            self._live_predicates = frozenset(
                value.predicate
                for _path, value in _live_values(
                    self.state, self.state.index.sources.get(CLAIM_TYPES_INDEX, ()), ClaimType
                )
            )
        return self._live_predicates

    def _shown(self, predicate: str) -> int:
        path = _type_path(predicate)
        return 0 if path is None else view_changed_at(self.state.chain(path), CURRENT_VIEW)

    def _sources(self, path: str) -> PSet[str]:
        found: PSet[str] = self.state.index.sources.get(path, EMPTY_PSET)
        return found

    def _contribution(self, source: str, path: str) -> int:
        """When ``source`` last moved what ``path`` shows of it."""

        if source.startswith(CLAIM_TYPES_PREFIX) and path.startswith(CONTRACTS_DIRECTORY):
            return view_changed_at(self.state.chain(source), CONTRACTS_VIEW)
        return self.state.effective(source)

    def stamp(self, path: str) -> int:
        state = self.state
        best = max((self._contribution(item, path) for item in self._sources(path)), default=0)
        if path == ONTOLOGY_INDEX:
            best = max(best, state.index.kinds_at.max(0))
        elif path == KITS_INDEX:
            for receipt in self._sources(path):
                chain = state.chain(receipt)
                for target in () if chain is None else chain.reach:
                    if not _is_file(target):
                        best = max(best, state.sequence(target))
        elif path.startswith(KINDS_DIRECTORY):
            # Each field's short name, as a field here or as an edge pointing
            # here, moves with its shadow's liveness.
            kind = path.removeprefix(KINDS_DIRECTORY).removesuffix(".md")
            for _type_file, claim_type in self._kind_types(path):
                pointed = kind in claim_type.allowed_object_subject_kinds
                for source in concrete_subject_kinds(claim_type.allowed_subject_kinds):
                    if source != kind and not pointed:
                        continue
                    prefix = f"{source}."
                    if claim_type.predicate.startswith(prefix):
                        best = max(best, self._shown(claim_type.predicate[len(prefix) :]))
        return best

    def _kind_types(self, card: str) -> list[tuple[str, ClaimType]]:
        return _live_values(
            self.state,
            (item for item in self._sources(card) if item.startswith(CLAIM_TYPES_PREFIX)),
            ClaimType,
        )

    def _subject_kind(self, kind: str) -> tuple[str, SubjectKind] | None:
        try:
            from cruxible_client.contracts.subject_kinds import subject_kind_path

            path = subject_kind_path(kind)
        except CruxibleError:
            return None
        value = self.state.value(path)
        return (path, value) if isinstance(value, SubjectKind) and _live(value) else None

    # -- existence and bytes

    def render(self, path: str, changed_at: int) -> bytes | None:
        """The file's bytes at this generation, or None when it is not on the floor."""

        if path == ONTOLOGY_INDEX:
            return self._ontology_index(changed_at)
        if path == CLAIM_TYPES_INDEX:
            return self._claim_types_index(changed_at)
        if path == CONTRACTS_INDEX:
            return self._contracts_index(changed_at)
        if path == QUERIES_INDEX:
            return self._queries_index(changed_at)
        if path == LINES_INDEX:
            return self._lines_index(changed_at)
        if path == KITS_INDEX:
            return self._kits_index(changed_at)
        if path.startswith(KINDS_DIRECTORY):
            return self._kind_card(path, changed_at)
        if path.startswith(CONTRACTS_DIRECTORY):
            return self._contract_card(path, changed_at)
        return None

    def _kind_set(self) -> dict[str, tuple[str | None, SubjectKind | None, int]]:
        """Every kind a live SubjectKind or live ClaimType names -> (SubjectKind, its fields)."""

        kinds: dict[str, tuple[str | None, SubjectKind | None, int]] = {}
        for path, value in _live_values(self.state, self._sources(ONTOLOGY_INDEX), SubjectKind):
            kinds[value.subject_kind] = (path, value, 0)
        for _path, claim_type in _live_values(
            self.state, self._sources(CLAIM_TYPES_INDEX), ClaimType
        ):
            for kind in concrete_subject_kinds(claim_type.allowed_subject_kinds):
                declared, subject_kind, fields = kinds.get(kind, (None, None, 0))
                kinds[kind] = (declared, subject_kind, fields + 1)
        return kinds

    def _ontology_index(self, changed_at: int) -> bytes:
        kinds = self._kind_set()
        rows = []
        for kind in _byte_sorted(kinds):
            path, subject_kind, fields = kinds[kind]
            rows.append(
                "\t".join(
                    (
                        kind,
                        _identity(subject_kind),
                        str(fields),
                        _LOCAL if path is None else self.owners.label(path),
                        _first_sentence(None if subject_kind is None else subject_kind.description),
                    )
                )
            )
        return _render(
            (
                _header(
                    "ontology",
                    len(rows),
                    "kinds",
                    "kind, identity, fields, owner, description",
                    changed_at,
                ),
                *rows,
            )
        )

    def _kind_card(self, card: str, changed_at: int) -> bytes | None:
        kind = card.removeprefix(KINDS_DIRECTORY).removesuffix(".md")
        types = self._kind_types(card)
        fields = [
            (path, item)
            for path, item in types
            if kind in concrete_subject_kinds(item.allowed_subject_kinds)
        ]
        declared = self._subject_kind(kind)
        if declared is None and not fields:
            return None
        live = self.live_predicates()
        owner = _LOCAL if declared is None else self.owners.label(declared[0])
        subject_kind = None if declared is None else declared[1]
        lines = [f"# {kind}  ontology  owner={owner}  {stamp_text(changed_at)}"]
        identity = _identity(subject_kind)
        lines.append(
            f"identity: {identity}"
            + ("" if subject_kind is None else f"  # SubjectKind:{subject_kind.subject_kind}")
        )
        if subject_kind is not None and subject_kind.description:
            lines.append(f"description: {_one_line(subject_kind.description)}")
        sections = sorted(
            fields,
            key=lambda item: (
                short_field_name(item[1].predicate, kind, live).encode(),
                item[1].predicate.encode(),
            ),
        )
        for path, claim_type in sections:
            lines.append("")
            lines.extend(_field_section(self.state, self.owners, path, claim_type, kind, live))
        pointing = sorted(
            (
                (short_field_name(item.predicate, source, live), source, item)
                for _path, item in types
                if kind in item.allowed_object_subject_kinds
                for source in concrete_subject_kinds(item.allowed_subject_kinds)
            ),
            key=lambda row: (row[0].encode(), row[1].encode(), row[2].predicate.encode()),
        )
        if pointing:
            lines.extend(("", "## pointed at by"))
            lines.extend(
                f"{short} <- {source}  {item.predicate}  {item.cardinality}"
                for short, source, item in pointing
            )
        return _render(lines)

    def _claim_types_index(self, changed_at: int) -> bytes:
        rows = sorted(
            (
                _claim_type_row(self.state, self.owners, path, value)
                for path, value in _live_values(
                    self.state, self._sources(CLAIM_TYPES_INDEX), ClaimType
                )
            ),
            key=lambda row: row.encode(),
        )
        return _render(
            (
                _header(
                    "claim-types",
                    len(rows),
                    "claim types",
                    "predicate, subject kinds, type, cardinality, default role, evidence, "
                    "stale_after, contention, owner",
                    changed_at,
                ),
                *rows,
            )
        )

    def _admitters(self, contract_id: str) -> tuple[list[str], list[str]]:
        """The live ClaimTypes admitting a contract, and those that also require a Capture."""

        admitted: list[str] = []
        required: list[str] = []
        for _path, claim_type in _live_values(
            self.state,
            (
                item
                for item in self._sources(contract_card_path(contract_id))
                if item.startswith(CLAIM_TYPES_PREFIX)
            ),
            ClaimType,
        ):
            if contract_id not in admitted_contracts(claim_type, self.state.names):
                continue
            admitted.append(claim_type.predicate)
            if effective_evidence_requirement(claim_type) == "captured":
                required.append(claim_type.predicate)
        return _byte_sorted(admitted), _byte_sorted(required)

    def _contract(self, contract_id: str) -> tuple[str, CaptureContract] | None:
        path = f"{CAPTURE_CONTRACTS_PREFIX}{contract_id}.json"
        value = self.state.value(path)
        return (path, value) if isinstance(value, CaptureContract) and _live(value) else None

    def _contract_card(self, card: str, changed_at: int) -> bytes | None:
        contract_id = card.removeprefix(CONTRACTS_DIRECTORY).removesuffix(".md")
        found = self._contract(contract_id)
        if found is None:
            return None
        path, contract = found
        admitted, required = self._admitters(contract_id)
        lines = [
            f"# CaptureContract:{contract_id}  ontology  owner={self.owners.label(path)}  "
            f"{stamp_text(changed_at)}",
            f"grade: {contract.epistemic_grade}",
            f"source kinds: {', '.join(contract.allowed_source_kinds) or '-'}",
            f"logical sources: {', '.join(contract.logical_source_identities) or '-'}",
            f"materialization: {', '.join(contract.allowed_materialization_modes) or '-'}",
            f"evidence kinds: {', '.join(contract.evidence_kinds) or '-'}",
            "",
            "## admitted by",
            *admitted,
            "",
            "## required by",
            *required,
        ]
        return _render(lines)

    def _contracts_index(self, changed_at: int) -> bytes:
        rows = []
        for path, contract in _live_values(
            self.state,
            (
                item
                for item in self._sources(CONTRACTS_INDEX)
                if item.startswith(CAPTURE_CONTRACTS_PREFIX)
            ),
            CaptureContract,
        ):
            admitted, required = self._admitters(contract.identity.name)
            rows.append(
                "\t".join(
                    (
                        contract.identity.name,
                        contract.epistemic_grade,
                        ",".join(contract.allowed_source_kinds) or "-",
                        ",".join(contract.logical_source_identities) or "-",
                        ",".join(contract.evidence_kinds) or "-",
                        str(len(admitted)),
                        str(len(required)),
                        self.owners.label(path),
                    )
                )
            )
        rows.sort(key=lambda row: row.encode())
        return _render(
            (
                _header(
                    "contracts",
                    len(rows),
                    "contracts",
                    "contract, grade, source kinds, logical sources, evidence kinds, "
                    "admitted by, required by, owner",
                    changed_at,
                ),
                *rows,
            )
        )

    def _queries_index(self, changed_at: int) -> bytes:
        rows = []
        for path, query in _live_values(self.state, self._sources(QUERIES_INDEX), QueryDefinition):
            parameters = ", ".join(
                f"{item.name}{'' if item.required else '?'}:{item.value_type}"
                for item in query.parameters
            )
            rows.append(
                "\t".join(
                    (
                        query.identity.name,
                        parameters or "-",
                        f"{query.result_shape} {query.result_cardinality}",
                        _first_sentence(query.description),
                        self.owners.label(path),
                    )
                )
            )
        rows.sort(key=lambda row: row.encode())
        return _render(
            (
                _header(
                    "queries",
                    len(rows),
                    "queries",
                    "query, parameters, result, description, owner",
                    changed_at,
                ),
                *rows,
            )
        )

    def _lines_index(self, changed_at: int) -> bytes:
        sources = self._sources(LINES_INDEX)
        lines = _live_values(
            self.state, (item for item in sources if item.startswith(LINES_PREFIX)), LineSpec
        )
        triggers: dict[str, list[str]] = {}
        for _path, trigger in _live_values(
            self.state, (item for item in sources if item.startswith(TRIGGERS_PREFIX)), Trigger
        ):
            if isinstance(trigger.target, LineTarget):
                triggers.setdefault(trigger.target.line.target.name, []).append(
                    f"{trigger.identity.name}({trigger.schedule.kind})"
                )
        mandates: dict[str, list[str]] = {}
        for path in sources:
            if not path.startswith(MANDATES_PREFIX):
                continue
            mandate = self.state.value(path)
            if mandate is None or not _live(mandate):
                continue
            grants = (
                mandate.grants
                if isinstance(mandate, ProcedureMandate)
                else ("settle" if mandate.rung == 3 else "propose")
            )
            mandates.setdefault(mandate.procedure.target.name, []).append(
                f"{mandate.identity.name} {grants} "
                f"valid {_time(mandate.valid_from)}..{_time(mandate.expires_at)}"
                + (" suspended" if getattr(mandate, "suspended", False) else "")
            )
        rows = []
        for _path, line in lines:
            accepts = []
            if line.trigger_input is not None:
                accepts.append(f"input={line.trigger_input}")
            if line.trigger_event is not None:
                accepts.append(f"event={line.trigger_event.capture_contract_identity.name}")
            name = line.identity.name
            procedure = line.procedure.target.name
            rows.append(
                "\t".join(
                    (
                        name,
                        procedure,
                        line.max_authority,
                        " ".join(accepts) or "-",
                        ", ".join(_byte_sorted(triggers.get(name, ()))) or "-",
                        "; ".join(_byte_sorted(mandates.get(procedure, ()))) or "-",
                    )
                )
            )
        rows.sort(key=lambda row: row.encode())
        return _render(
            (
                _header(
                    "lines",
                    len(rows),
                    "lines",
                    "line, procedure, max_authority, trigger input, triggers, mandates",
                    changed_at,
                ),
                *rows,
            )
        )

    def _kits_index(self, changed_at: int) -> bytes:
        rows = []
        for path in self._sources(KITS_INDEX):
            chain = self.state.chain(path)
            receipt = None if chain is None else chain.value
            if not isinstance(receipt, KitReceipt) or not receipt.artifacts:
                continue
            assert chain is not None
            edited: list[str] = []
            retired: list[str] = []
            live = 0
            for entry in receipt.artifacts:
                held = self.state.value(entry.path)
                if held is not None and not _live(held):
                    retired.append(_ref(entry.path))
                elif held is not None:
                    live += 1
                if self.owners.edited(entry.path, entry):
                    edited.append(
                        _ref(entry.path)
                        + (
                            " (missing)"
                            if held is None
                            else " (retired)"
                            if not _live(held)
                            else ""
                        )
                    )
            rows.append(
                "\t".join(
                    (
                        receipt.kit_id,
                        receipt.version,
                        ",".join(receipt.owns) or "-",
                        str(chain.first),
                        str(chain.sequence),
                        str(live),
                        ", ".join(_byte_sorted(item.identity for item in receipt.kept)) or "-",
                        ", ".join(_byte_sorted(edited)) or "-",
                        ", ".join(_byte_sorted(retired)) or "-",
                        ", ".join(
                            _byte_sorted(
                                f"{item.provider_id}@{item.version}" for item in receipt.providers
                            )
                        )
                        or "-",
                    )
                )
            )
        rows.sort(key=lambda row: row.encode())
        return _render(
            (
                _header(
                    "kits",
                    len(rows),
                    "kits",
                    "kit, version, owns, installed gen, receipt gen, definitions, kept, "
                    "edited, retired, providers",
                    changed_at,
                ),
                *rows,
            )
        )


# -- rendering into the floor ---------------------------------------------------------------


def render_ontology(
    state: OntologyState,
    held: Callable[[str], tuple[bytes, int] | None],
    candidates: Iterable[str],
    *,
    fresh: Callable[[str, bytes, int], None],
    drop: Callable[[str], None],
) -> int:
    """Re-render each candidate file whose stamp moved; returns how many were rendered.

    A file whose stamp held is the same bytes (its inputs did not move), so it
    is kept without rendering; one that is no longer on the floor is dropped.
    """

    renderer = _Renderer(state)
    rendered = 0
    for path in _byte_sorted(candidates):
        changed_at = renderer.stamp(path)
        kept = held(path)
        if kept is not None and kept[1] == changed_at:
            continue
        content = renderer.render(path, changed_at)
        rendered += 1
        if content is None:
            drop(path)
        else:
            fresh(path, content, changed_at)
    return rendered


__all__ = [
    "CLAIM_TYPES_INDEX",
    "CONTRACTS_INDEX",
    "INDEX_FILES",
    "KITS_INDEX",
    "LINES_INDEX",
    "ONTOLOGY_INDEX",
    "ONTOLOGY_PREFIX",
    "QUERIES_INDEX",
    "OntologyIndex",
    "OntologyState",
    "admitted_contracts",
    "all_files",
    "build_ontology_index",
    "contract_card_path",
    "kind_card_path",
    "ontology_reach",
    "patch_ontology_index",
    "reached_files",
    "render_ontology",
]
