"""The seed bundle: a directory of authoring JSONs, grouped into proposals.

§10.5 already says what a bundle *is* -- "a namespaced seed bundle/manifest
containing ClaimTypes, Subjects, Claims, QueryDefinitions, Procedures,
Documents, and dependencies. Installing or upgrading it generates an ordinary
dependency-closed change set" -- and every word of that is a statement about
existing machinery. This module adds no admission path, no operation, and no
authority. It reads a bundle's bytes and answers exactly one question:

    which of the propose operations that already exist, in which order, carrying
    which entries, is the *fewest* proposals this bundle can legally become?

Why the answer is not "one"
---------------------------
A change set settles as one indivisible generation, so the temptation is to want
one proposal for the whole bundle. Two facts make that impossible and both are
load-bearing:

*The frozen v1 plan grammar grouped expert Claims under one plural operation.*
That authoring operation has since retired, but its name remains in the pure
plan bytes so historical plan digests do not change. Seed application is no
longer a sanctioned write surface; this module only renders the deterministic
historical grouping.

*A proposal settles against the base it was admitted at.* Two proposals opened
against one accepted head cannot both activate -- the second refuses with
"settlement base is not the current main ref" -- so a plan is a **sequence** and
the caller must approve and activate each group before the next is submitted.
That is why applying a bundle is one group per invocation and why approval and
activation stay outside this module entirely: they are separate governed acts
and a seeding convenience may not perform them.

Where the minimization actually comes from
------------------------------------------
Legacy Claim payloads declared dependency closures. A ClaimType or Subject that
one of those payloads carries needs no separate group in the frozen plan. So
the plan is:

* every Claim in the bundle -> **one** batch proposal;
* every ClaimType and Subject *carried* by one of those Claims -> **no**
  proposal, and the plan says so by name;
* everything else -> one proposal each, on the operation that already exists
  for it.

The minimization is therefore a fact about the bundle's own declared closures,
not a judgment this module makes. It never rewrites an authoring to add a
closure, because deciding that a Claim should carry a Subject is exactly the
kind of authoring decision the closure laws exist to adjudicate at admission.

Refusing rather than guessing
-----------------------------
A bundle that declares a ClaimType at top level *and* carries a different
ClaimType for the same predicate inside a Claim is asking for two byte strings
at one canonical path in one generation. There is no later moment at which those
could be reconciled, which is precisely the cross-authoring conflict
the former apply surface refused before reaching the proposal service. This
module still refuses it at plan time so the pure plan remains deterministic.

Nothing here touches a filesystem, a clock, or a network. The CLI reads the
directory and drives the operations; this module maps bytes to a plan, exactly
as the render lens maps accepted state to a tree and the stash maps regions to a
record.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.authoring.inputs import ClaimInput, ProcedureInput
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import ClaimStatement
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.subjects import SubjectShell

SEED_BUNDLE_DIGEST_DOMAIN: Final = "playbill-seed-plan-v1"
SEED_GROUP_OPERATION_DIGEST_DOMAIN: Final = "playbill-seed-group-operation-v1"

SEED_BODY_DIRECTORY: Final = "bodies"
"""Raw bytes stored through the ordinary body-store operation before any propose.

A foreign-source citation names a content digest, and a Document names a body
digest; both must already be in the instance's CAS when the artifact citing them
is proposed. Committing the bytes beside the authoring that cites them is what
makes a bundle self-contained and its digests checkable by anyone reading it.
"""

SeedEntryKind = Literal[
    "claim_type",
    "subject",
    "document",
    "claim",
    "query_definition",
    "procedure",
]

SEED_ENTRY_DIRECTORIES: Final[Mapping[str, SeedEntryKind]] = {
    "claim-types": "claim_type",
    "subjects": "subject",
    "documents": "document",
    "claims": "claim",
    "query-definitions": "query_definition",
    "procedures": "procedure",
}
"""Bundle subdirectory -> what the JSONs inside it author. There is no manifest
file: the layout *is* the manifest, and the plan below is its rendering."""

SEED_GROUP_OPERATIONS: Final[Mapping[SeedEntryKind, str]] = {
    "claim_type": "playbill_propose_claim_type",
    "subject": "playbill_propose_subject",
    "document": "playbill_propose_document",
    "query_definition": "playbill_propose_query_definition",
    "procedure": "playbill_authoring_submit",
}
"""The served operation each group submits through. Every one of these existed
before this module did; the table is written out so that "zero new served ops"
is readable rather than asserted."""

_GROUP_ORDER: Final[tuple[SeedEntryKind, ...]] = (
    "claim_type",
    "subject",
    "document",
    "claim",
    "query_definition",
    "procedure",
)
"""Dependency order. Uncarried ClaimTypes and Subjects have to be accepted before
the Claims that reference without carrying them; a QueryDefinition pins the
ClaimType digests it projects, so it comes last. Documents depend on nothing in
the bundle but their own stored body, and sort where they are harmless."""


class SeedBundleError(PlaybillError):
    """A bundle could not be read, or could not be legally grouped."""


class _StrictSeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedBundleEntryV1(_StrictSeedModel):
    """One authoring JSON, named by the identity its grouping turns on."""

    tag: Literal["playbill-seed-bundle-entry-v1"] = "playbill-seed-bundle-entry-v1"
    path: str
    kind: SeedEntryKind
    identity: str
    payload: dict[str, Any]


_REF_SAFE: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")


def proposal_slug(group_id: str) -> str:
    """Fold one group id into the seed-plan v1 presentation-slug grammar.

    A group id names an artifact and is written for a person to select --
    `query_definition:project.work_items`. The v1 plan retains its short slug for
    byte compatibility and human display, but proposal refs are now machine-owned
    content addresses from :func:`seed_group_proposal_name`.
    """

    folded = "".join(item if item in _REF_SAFE else "-" for item in group_id.lower())
    trimmed = folded.strip("-.") or "group"
    return trimmed if trimmed[0].isalpha() else f"g-{trimmed}"


class SeedProposalGroupV1(_StrictSeedModel):
    """One proposal the plan will submit, and the reason it is exactly one."""

    tag: Literal["playbill-seed-proposal-group-v1"] = "playbill-seed-proposal-group-v1"
    group_id: str
    proposal_slug: str
    kind: SeedEntryKind
    operation: str
    entry_paths: tuple[str, ...] = Field(min_length=1)
    rationale: str

    @field_validator("entry_paths")
    @classmethod
    def _entry_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("a seed group's entry paths must be sorted and unique")
        return value


class SeedCarriedEntryV1(_StrictSeedModel):
    """One bundle entry that needs no proposal because a Claim already carries it."""

    tag: Literal["playbill-seed-carried-entry-v1"] = "playbill-seed-carried-entry-v1"
    path: str
    kind: SeedEntryKind
    identity: str
    carried_by: str
    """The Claim authoring whose declared closure admits it."""


class SeedPlanV1(_StrictSeedModel):
    """The whole grouping, before a byte is stored or a proposal is opened.

    Deterministic in the bundle's bytes alone: no clock, no accepted state, no
    instance. That is what lets `--plan` answer offline and lets a run manifest
    pin `plan_digest` as evidence that two arms seeded the same world.
    """

    tag: Literal["playbill-seed-plan-v1"] = "playbill-seed-plan-v1"
    proposal_name: str
    body_paths: tuple[str, ...] = ()
    groups: tuple[SeedProposalGroupV1, ...] = ()
    carried: tuple[SeedCarriedEntryV1, ...] = ()

    @field_validator("body_paths")
    @classmethod
    def _body_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("seed body paths must be sorted and unique")
        return value

    @property
    def group_ids(self) -> tuple[str, ...]:
        return tuple(item.group_id for item in self.groups)

    def group(self, group_id: str) -> SeedProposalGroupV1:
        for item in self.groups:
            if item.group_id == group_id:
                return item
        named = ", ".join(self.group_ids) or "none"
        raise SeedBundleError(f"no seed group named {group_id}; this bundle plans: {named}")

    def next_group_id(self, after: str) -> str | None:
        ids = self.group_ids
        index = ids.index(after)
        return ids[index + 1] if index + 1 < len(ids) else None


class SeedPlanResultV1(_StrictSeedModel):
    tag: Literal["playbill-seed-plan-result-v1"] = "playbill-seed-plan-result-v1"
    plan: SeedPlanV1
    plan_digest: str
    rendered: tuple[str, ...]


_UNDIGESTED_PLAN_FIELDS: Final = frozenset({"tag", "proposal_name"})
"""What the plan digest deliberately leaves out.

The digest answers "is this the same world?", so it commits to the bundle's
entries, bodies, and grouping and to nothing about the invocation. The proposal
name is the operator's label for one application; two harnesses seeding the
identical bundle under different names have seeded the identical world, and a
digest that disagreed would be measuring the wrong thing."""


def seed_plan_digest(plan: SeedPlanV1) -> Sha256Value:
    """Digest the plan, so a run manifest can pin the world it seeds."""

    return typed_digest(
        Sha256Value,
        SEED_BUNDLE_DIGEST_DOMAIN,
        {
            key: value
            for key, value in plan.model_dump(mode="json").items()
            if key not in _UNDIGESTED_PLAN_FIELDS
        },
    )


def seed_group_operation_digest(
    plan: SeedPlanV1,
    group: SeedProposalGroupV1,
) -> Sha256Value:
    """Bind one planned group to its exact, name-independent bundle content."""

    return typed_digest(
        Sha256Value,
        SEED_GROUP_OPERATION_DIGEST_DOMAIN,
        {
            "plan_digest": seed_plan_digest(plan).tagged,
            "group_id": group.group_id,
        },
    )


def seed_group_proposal_name(plan: SeedPlanV1, group: SeedProposalGroupV1) -> str:
    """Return the machine-owned proposal-ref leaf for one seed operation."""

    return f"seed-{seed_group_operation_digest(plan, group).value}"


# -- reading a bundle -------------------------------------------------------


def _decode(path: str, content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeedBundleError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SeedBundleError(f"{path} must hold one JSON object")
    return dict(payload)


def _identity_of(path: str, kind: SeedEntryKind, payload: Mapping[str, Any]) -> str:
    """Name one entry by the identity its grouping turns on, and nothing more.

    Deliberately partial. Grouping needs each entry's canonical identity and,
    for a Claim, the statement that identity comes from; it does not need the
    entry to be *admissible*, and checking that here would mean re-implementing
    the admission laws in a seeding convenience. A bundle whose ClaimType is
    well-formed but unacceptable is refused by the propose operation, with the
    law's own diagnostics, which is the only place that answer is authoritative.

    So the models validated here are exactly the ones this module reads fields
    out of, and every one of them lives inside this package.
    """

    try:
        if kind == "claim_type":
            return ClaimType.model_validate(payload).predicate
        if kind == "subject":
            subject = SubjectShell.model_validate(payload)
            return f"{subject.subject_kind}/{subject.subject_id}"
        if kind == "document":
            return DocumentShell.model_validate(payload).identity
        if kind == "query_definition":
            return QueryDefinitionV1.model_validate(payload).identity.name
        if kind == "procedure":
            procedure = ProcedureInput.model_validate(payload)
            return str(procedure.definition["name"])
        if payload.get("kind") == "claim":
            claim = ClaimInput.model_validate(payload)
            return f"{claim.subject}#{claim.predicate}"
        statement = ClaimStatement.model_validate(payload.get("statement"))
    except ValueError as exc:
        raise SeedBundleError(f"{path} is not a well-formed {kind} authoring: {exc}") from exc
    return f"{statement.subject.artifact_path}#{statement.predicate}"


def read_seed_bundle(files: Mapping[str, bytes]) -> tuple[SeedBundleEntryV1, ...]:
    """Read a bundle's files into typed, byte-sorted entries.

    ``files`` is keyed by bundle-relative POSIX path. A file outside the known
    directories refuses rather than being ignored: silently skipping part of a
    bundle would make "this bundle was applied" untrue in a way nobody could see.
    """

    entries: list[SeedBundleEntryV1] = []
    for path in byte_sorted(tuple(files)):
        if path.startswith(f"{SEED_BODY_DIRECTORY}/"):
            continue
        directory, separator, name = path.partition("/")
        kind = SEED_ENTRY_DIRECTORIES.get(directory)
        if not separator or kind is None:
            known = ", ".join(sorted([*SEED_ENTRY_DIRECTORIES, SEED_BODY_DIRECTORY]))
            raise SeedBundleError(
                f"{path} is not in a seed bundle directory; a bundle holds only {known}"
            )
        if not name.endswith(".json"):
            raise SeedBundleError(f"{path} is not an authoring JSON; {directory}/ holds *.json")
        payload = _decode(path, files[path])
        entries.append(
            SeedBundleEntryV1(
                path=path,
                kind=kind,
                identity=_identity_of(path, kind, payload),
                payload=payload,
            )
        )
    return tuple(entries)


# -- grouping ---------------------------------------------------------------


def _carried_closures(
    entries: tuple[SeedBundleEntryV1, ...],
) -> tuple[dict[str, tuple[bytes, str]], dict[str, tuple[bytes, str]]]:
    """Index what the bundle's Claims already declare they carry.

    Returns `(claim types, subjects)`, each mapping identity -> (canonical bytes,
    the Claim path that carries it). Two Claims carrying byte-identical copies
    deduplicate exactly as the batch service deduplicates them; two carrying
    different bytes is the cross-authoring conflict, refused here by name.
    """

    claim_types: dict[str, tuple[bytes, str]] = {}
    subjects: dict[str, tuple[bytes, str]] = {}

    def record(
        into: dict[str, tuple[bytes, str]],
        identity: str,
        payload: Mapping[str, Any],
        source: str,
        label: str,
    ) -> None:
        content = canonical_bytes(dict(payload))
        existing = into.get(identity)
        if existing is not None and existing[0] != content:
            raise SeedBundleError(
                f"{source} and {existing[1]} carry different {label} artifacts for {identity}; "
                "two byte strings cannot occupy one canonical path in one change set"
            )
        into.setdefault(identity, (content, source))

    for entry in entries:
        if entry.kind != "claim":
            continue
        payload = entry.payload
        own_type = payload.get("claim_type_artifact")
        if isinstance(own_type, dict):
            record(claim_types, str(own_type.get("predicate")), own_type, entry.path, "ClaimType")
        for value in payload.get("dependency_claim_types") or ():
            if isinstance(value, dict):
                record(claim_types, str(value.get("predicate")), value, entry.path, "ClaimType")

        own_subject = payload.get("subject_shell")
        candidates = [own_subject, *(payload.get("dependency_subject_shells") or ())]
        for value in candidates:
            if not isinstance(value, dict):
                continue
            identity = f"{value.get('subject_kind')}/{value.get('subject_id')}"
            record(subjects, identity, value, entry.path, "Subject")

    return claim_types, subjects


def plan_seed_bundle(files: Mapping[str, bytes], *, proposal_name: str) -> SeedPlanV1:
    """Group one bundle into the fewest proposals its own closures allow."""

    if not proposal_name.strip():
        raise SeedBundleError("a seed bundle application needs a proposal name")
    entries = read_seed_bundle(files)
    body_paths = byte_sorted(
        tuple(path for path in files if path.startswith(f"{SEED_BODY_DIRECTORY}/"))
    )

    seen: dict[tuple[str, str], str] = {}
    for entry in entries:
        key = (entry.kind, entry.identity)
        if key in seen:
            raise SeedBundleError(
                f"{entry.path} and {seen[key]} both declare the {entry.kind} {entry.identity}; "
                "a bundle declares each artifact once"
            )
        seen[key] = entry.path

    carried_types, carried_subjects = _carried_closures(entries)
    carried: list[SeedCarriedEntryV1] = []
    grouped: dict[SeedEntryKind, list[SeedProposalGroupV1]] = {kind: [] for kind in _GROUP_ORDER}
    input_claims: list[SeedBundleEntryV1] = []

    for entry in entries:
        index = carried_types if entry.kind == "claim_type" else carried_subjects
        if entry.kind in {"claim_type", "subject"} and entry.identity in index:
            content, source = index[entry.identity]
            if canonical_bytes(entry.payload) != content:
                raise SeedBundleError(
                    f"{entry.path} declares a {entry.kind} for {entry.identity} that differs from "
                    f"the one {source} carries; two byte strings cannot occupy one canonical path "
                    "in one change set"
                )
            carried.append(
                SeedCarriedEntryV1(
                    path=entry.path,
                    kind=entry.kind,
                    identity=entry.identity,
                    carried_by=source,
                )
            )
            continue
        if entry.kind == "claim":
            input_claims.append(entry)
            continue
        grouped[entry.kind].append(
            SeedProposalGroupV1(
                group_id=f"{entry.kind}:{entry.identity}",
                proposal_slug=proposal_slug(f"{entry.kind}:{entry.identity}"),
                kind=entry.kind,
                operation=SEED_GROUP_OPERATIONS[entry.kind],
                entry_paths=(entry.path,),
                rationale=(
                    (
                        "one Procedure input per existing coordinator intent; no plural "
                        "Procedure authoring operation exists"
                    )
                    if entry.kind == "procedure"
                    else (
                        f"one {entry.kind} per proposal: the served surface has a singular "
                        "propose operation for it and no plural one"
                    )
                ),
            )
        )

    legacy_claims = tuple(
        entry.path for entry in input_claims if entry.payload.get("kind") != "claim"
    )
    if legacy_claims:
        rendered = ", ".join(legacy_claims)
        raise SeedBundleError(
            "legacy Claim authoring is retired; convert these entries to kind='claim' "
            f"ClaimInput payloads before planning: {rendered}"
        )

    grouped["claim"].extend(
        SeedProposalGroupV1(
            group_id=f"claim_input:{entry.identity}",
            proposal_slug=proposal_slug(f"claim_input:{entry.identity}"),
            kind="claim",
            operation="playbill_authoring_submit",
            entry_paths=(entry.path,),
            rationale=(
                "one ergonomic Claim input per coordinator intent; its accepted artifact remains "
                "an ordinary Claim"
            ),
        )
        for entry in input_claims
    )

    return SeedPlanV1(
        proposal_name=proposal_name,
        body_paths=body_paths,
        groups=tuple(group for kind in _GROUP_ORDER for group in grouped[kind]),
        carried=tuple(sorted(carried, key=lambda item: item.path.encode("utf-8"))),
    )


def render_seed_plan(plan: SeedPlanV1) -> tuple[str, ...]:
    """The human rendering: what will be proposed, in order, and what rides along."""

    lines = [
        f"Seed plan for {plan.proposal_name}: {len(plan.groups)} proposal(s), "
        f"{len(plan.body_paths)} body file(s), digest {seed_plan_digest(plan).tagged}"
    ]
    for index, group in enumerate(plan.groups, start=1):
        lines.append(f"{index}. {group.group_id}  [{group.operation}]  {group.rationale}")
        lines.extend(f"     {path}" for path in group.entry_paths)
    for item in plan.carried:
        lines.append(f"carried  {item.path}  admitted by {item.carried_by}; no proposal of its own")
    return tuple(lines)


def read_seed_bundle_files(root: Path) -> dict[str, bytes]:
    """Read one bundle without following symlinks or escaping its root."""

    try:
        bundle_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SeedBundleError(f"Seed bundle directory is unavailable: {root}") from exc
    if not bundle_root.is_dir():
        raise SeedBundleError(f"Not a seed bundle directory: {root}")
    files: dict[str, bytes] = {}
    for path in sorted(bundle_root.rglob("*")):
        if path.is_symlink():
            raise SeedBundleError(f"Seed bundles may not contain symlinks: {path}")
        if path.is_file():
            files[path.relative_to(bundle_root).as_posix()] = path.read_bytes()
    if not files:
        raise SeedBundleError(f"The seed bundle at {root} is empty")
    return files


def plan_seed_directory(root: Path, *, proposal_name: str) -> SeedPlanResultV1:
    files = read_seed_bundle_files(root)
    plan = plan_seed_bundle(files, proposal_name=proposal_name)
    if not plan.groups:
        raise SeedBundleError(f"The seed bundle at {root} declares nothing to propose")
    return SeedPlanResultV1(
        plan=plan,
        plan_digest=seed_plan_digest(plan).tagged,
        rendered=render_seed_plan(plan),
    )


__all__ = [
    "SEED_BODY_DIRECTORY",
    "SEED_BUNDLE_DIGEST_DOMAIN",
    "SEED_GROUP_OPERATION_DIGEST_DOMAIN",
    "SEED_ENTRY_DIRECTORIES",
    "SEED_GROUP_OPERATIONS",
    "SeedBundleEntryV1",
    "SeedBundleError",
    "SeedCarriedEntryV1",
    "SeedEntryKind",
    "SeedPlanV1",
    "SeedPlanResultV1",
    "SeedProposalGroupV1",
    "plan_seed_bundle",
    "plan_seed_directory",
    "proposal_slug",
    "read_seed_bundle",
    "read_seed_bundle_files",
    "render_seed_plan",
    "seed_group_operation_digest",
    "seed_group_proposal_name",
    "seed_plan_digest",
]
