"""The compile contract: edit, compile, accept are three distinct gates.

§11.9.4 in one sentence: **editing never silently proposes.** A dirty region is
a local draft and stays one until somebody compiles, and a compiled ChangeSet is
a candidate and stays one until somebody accepts. This module is the middle
gate, and it is the only one of the three that exists as code -- the other two
are the absence of code, which is the point.

What compile actually does
--------------------------
It *prepares proposal input*. It classifies what the working tree changed,
performs the **semantic three-way** classification against the render baseline
and the current head, and assembles one set of ordinary direct-Claim authorings
for the ordinary multi-Claim propose surface. It admits nothing, settles
nothing, and computes no candidate digest. Every semantic judgement --
cardinality, reuse, closure, supersession, authority -- belongs to the evaluator
behind that surface, and a compile that thought otherwise would be a second law.

Those authorings are emitted as canonical **wire mappings**, not as the service
layer's own input model, and that is structural rather than stylistic: this
package may not import the service layer at all (the PC-F3 boundary test holds
the whole list), so there is no name in scope here through which a compile could
submit, admit, or settle anything. Compile hands its caller material; the caller
carries it to the served operation, which validates it into the one input model
that owns that shape.

The three-way, and who performs it
----------------------------------
Compile binds the baseline at the accepted generation **G** the render was a
checkout of, and reports per address whether the accepted artifact is unchanged
at the current head **H**, changed at H, or gone at H. It then submits with
``base=G``. That is the whole of the mapping: the proposal receive path already
computes ``is_rebase = head != base`` and runs the §3.4 three-way member rebase,
so a compile that binds G gets the deterministic rebase or the typed member
conflict *from the machinery that owns it*. Nothing here reimplements a rebase,
and no textual diff decides anything -- the line comparison this module does
perform locates prose an author added, and prose an author added is not an
admissibility question.

Rebase and review evidence
--------------------------
A rebase produces a different candidate digest, and approvals are stored under
the digest they signed. So a prior approval does not merely *count for less*
against a rebased candidate -- it is not found at all. `superseded_by_rebase` is
the name this module gives that fact when it reports it
(:func:`native_review_currency`); it is not a new mechanism, and there was never
a moment at which a stale approval could have been counted.

Deletion is never inferred
--------------------------
No path here produces ``retire=True``. A deleted region, file, or directory
reaches this module as a notice and leaves it as a notice, because §11.9.3 makes
removal a loss of working material and withdrawal an explicit disposition. The
one disposition that *does* mean "I have decided against this" is ``withdraw``,
and what it withdraws is a local draft that was never accepted.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    file_digest,
    typed_digest,
)
from cruxible_client.contracts.claim_type_structure import claim_type_structural_signature
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.claims import (
    ClaimObject,
    ClaimStatement,
    ExactContentClaimObject,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_statement_digest,
)
from cruxible_client.contracts.descriptor_claim_types import descriptor_claim_type
from cruxible_client.contracts.discovery import (
    DiscoveryHintsV1,
    ProposedSemanticInterfaceV1,
    ReuseCandidateV1,
    ReuseDispositionV1,
    SemanticReuseInterfaceV1,
    VocabularyReuseRequestV1,
    evaluate_vocabulary_reuse,
    normalize_discovery_term,
)
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_reuse_signature
from cruxible_core.playbill.native.context import RenderContextV1
from cruxible_core.playbill.native.grammar import (
    NativeDiagnosticV1,
    NativeDraftDisposition,
    NativeDraftMarkerV1,
    NativeRenderError,
    extract_prose,
)
from cruxible_core.playbill.native.lens import build_native_render
from cruxible_core.playbill.native.manifest import NativeRenderManifestV1
from cruxible_core.playbill.native.parse import NativeParsedRegionV1, parse_native_tree
from cruxible_core.playbill.native.state import NativeAcceptedStateV1, NativeClaimRecordV1
from cruxible_core.playbill.projection import AcceptedCoordinate

DRAFT_DIGEST_DOMAIN = "playbill-native-draft-v1"
DISTINCT_FROM_PREDICATE = "semantic.distinct_from"
ALIAS_PREDICATE = "semantic.alias"

NativeThreeWayOutcome = Literal["unchanged_at_head", "changed_at_head", "absent_at_head"]
NativeMemberKind = Literal[
    "locator_successor",
    "unbound_native_draft",
    "generated_distinct_from",
    "generated_alias",
]

_MINIMUM_PROSE_TOKEN = 3
_TOKEN_TRIM = " \t`*_#>()[],.;:!?\"'"


class _StrictCompileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeCompileError(NativeRenderError):
    """Compilation could not proceed, and the reason is stated rather than guessed."""


class NativeDraftDispositionV1(_StrictCompileModel):
    """One author disposition, however it reached the compiler.

    In-file (a class-3 draft marker) and on the command line are two spellings of
    one act. They produce this same record, so no law is enforced twice and the
    file is never the only place a disposition can live.
    """

    tag: Literal["playbill-native-draft-disposition-v1"] = "playbill-native-draft-disposition-v1"
    draft_id: str
    kind: NativeDraftDisposition
    predicate: str | None = None
    value: str | None = None
    subject_kind: str | None = None
    subject_id: str | None = None
    target_path: str | None = None
    alias: str | None = None

    @field_validator("draft_id")
    @classmethod
    def _draft_id(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @classmethod
    def from_marker(cls, draft_id: str, marker: NativeDraftMarkerV1) -> "NativeDraftDispositionV1":
        return cls(
            draft_id=draft_id,
            kind=marker.disposition,
            predicate=marker.predicate,
            value=marker.value,
            subject_kind=marker.subject_kind,
            subject_id=marker.subject_id,
            target_path=marker.target_path,
            alias=marker.alias,
        )

    @property
    def subject_address(self) -> SemanticAddress | None:
        if self.target_path is not None:
            return SemanticAddress.whole_artifact(self.target_path)
        if self.subject_kind is not None and self.subject_id is not None:
            return SemanticAddress.whole_artifact(
                f"subjects/{self.subject_kind}/{self.subject_id}.yaml"
            )
        return None


class NativeCompileRefusalV1(_StrictCompileModel):
    """One typed reason a compile did not produce a proposal.

    A refusal names what is required next. "Refused" without a required action is
    a dead end an agent cannot act on, and the whole point of compiling headlessly
    is that the next move is legible without a human reading a diff.
    """

    tag: Literal["playbill-native-compile-refusal-v1"] = "playbill-native-compile-refusal-v1"
    code: str
    path: str | None = None
    region_id: str | None = None
    draft_id: str | None = None
    address: SemanticAddress | None = None
    message: str
    required_action: str
    candidates: tuple[str, ...] = ()

    @field_validator("candidates")
    @classmethod
    def _candidates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("named refusal candidates must be sorted and unique")
        return value


class NativeThreeWayV1(_StrictCompileModel):
    """One address, judged against the baseline at G and the accepted head at H.

    ``outcome`` is a *classification*, never a resolution. `changed_at_head` says
    the proposal receive path will rebase; whether that rebase succeeds or
    reports a member conflict is the rebase machinery's answer, given at submit,
    and this record deliberately does not predict it.
    """

    tag: Literal["playbill-native-three-way-v1"] = "playbill-native-three-way-v1"
    address: SemanticAddress
    claim_path: str
    region_ids: tuple[str, ...]
    baseline_artifact_digest: str
    head_artifact_digest: str | None
    outcome: NativeThreeWayOutcome

    @field_validator("region_ids")
    @classmethod
    def _regions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("three-way region identities must be sorted and unique")
        return value

    @property
    def rebase_expected(self) -> bool:
        return self.outcome == "changed_at_head"


class NativeDraftCandidateV1(_StrictCompileModel):
    """One deterministic reuse candidate for an unlocated draft (§11.3)."""

    tag: Literal["playbill-native-draft-candidate-v1"] = "playbill-native-draft-candidate-v1"
    address: SemanticAddress
    identity: str
    kind: str
    label: str
    blocking: bool
    match_bases: tuple[str, ...]

    @classmethod
    def from_reuse(cls, candidate: ReuseCandidateV1) -> "NativeDraftCandidateV1":
        return cls(
            address=candidate.address,
            identity=candidate.identity.qualified,
            kind=candidate.kind,
            label=candidate.label,
            blocking=candidate.blocking,
            match_bases=byte_sorted(tuple(item.basis for item in candidate.match_basis)),
        )


class NativeCompileDraftV1(_StrictCompileModel):
    """Unlocated prose, its deterministic candidates, and what the author said.

    The prose itself is carried as text because a draft has no accepted identity
    to point at -- there is nothing else it could be. It becomes the Claim's
    rationale, which is the one place free prose is already a first-class part of
    an authored Claim.
    """

    tag: Literal["playbill-native-compile-draft-v1"] = "playbill-native-compile-draft-v1"
    draft_id: str
    path: str
    text: str
    line_numbers: tuple[int, ...]
    disposition: NativeDraftDispositionV1 | None = None
    candidates: tuple[NativeDraftCandidateV1, ...] = ()
    generated_distinct_from: tuple[SemanticAddress, ...] = ()

    @property
    def blocking_candidates(self) -> tuple[NativeDraftCandidateV1, ...]:
        return tuple(item for item in self.candidates if item.blocking)


class NativeCompileMemberV1(_StrictCompileModel):
    """One authored Claim the compile would submit, and why it exists."""

    tag: Literal["playbill-native-compile-member-v1"] = "playbill-native-compile-member-v1"
    kind: NativeMemberKind
    address: SemanticAddress | None = None
    claim_path: str | None = None
    draft_id: str | None = None
    predicate: str
    subject_path: str
    region_ids: tuple[str, ...] = ()
    authoring: dict[str, Any]


class NativeCompileResultV1(_StrictCompileModel):
    """One compile: what it would propose, what it refuses, and what it noticed.

    Preview and submit read this same value. There is exactly one compile
    function and two renderings of its result, so a preview cannot describe a
    proposal different from the one submission would make.
    """

    tag: Literal["playbill-native-compile-result-v1"] = "playbill-native-compile-result-v1"
    instance_id: str
    baseline: AcceptedCoordinate
    head: AcceptedCoordinate
    lens_id: str
    lens_version: int = Field(ge=1)
    evaluation_time: str
    three_way: tuple[NativeThreeWayV1, ...] = ()
    drafts: tuple[NativeCompileDraftV1, ...] = ()
    members: tuple[NativeCompileMemberV1, ...] = ()
    refusals: tuple[NativeCompileRefusalV1, ...] = ()
    notices: tuple[NativeDiagnosticV1, ...] = ()

    @property
    def compilable(self) -> bool:
        """A compile submits only when nothing refused and something changed."""

        return not self.refusals and bool(self.members)

    @property
    def rebase_expected(self) -> bool:
        return any(item.rebase_expected for item in self.three_way)

    @property
    def authorings(self) -> tuple[dict[str, Any], ...]:
        return tuple(item.authoring for item in self.members)

    @property
    def retirements(self) -> tuple[str, ...]:
        """Always empty: no compile path infers a retirement (§11.9.3)."""

        return tuple(
            str(item.claim_path)
            for item in self.members
            if item.authoring.get("retire") and item.claim_path is not None
        )


class NativeReviewCurrencyV1(_StrictCompileModel):
    """Whether collected review evidence binds the candidate that would settle.

    Headless by construction: it is a statement about two digests and a set of
    attestations, so the §11.9.7 forge check that renders it later adds no
    semantics and this answer needs no forge to be true.
    """

    tag: Literal["playbill-native-review-currency-v1"] = "playbill-native-review-currency-v1"
    proposal_id: str
    candidate_digest: str
    parent_semantic_root: str
    bound_candidate_digest: str | None = None
    status: Literal["current", "not_reviewed", "superseded_by_rebase"]
    binding_signer_ids: tuple[str, ...] = ()
    superseded_signer_ids: tuple[str, ...] = ()
    required_action: str

    @model_validator(mode="after")
    def _status_shape(self) -> "NativeReviewCurrencyV1":
        if self.status == "current" and not self.binding_signer_ids:
            raise ValueError("current review evidence must name the signers that bind it")
        if self.status == "superseded_by_rebase" and (
            self.bound_candidate_digest is None
            or self.bound_candidate_digest == self.candidate_digest
        ):
            raise ValueError("superseded review evidence must bind a different candidate")
        if self.superseded_signer_ids and self.status != "superseded_by_rebase":
            raise ValueError("only superseded review evidence names superseded signers")
        return self


# -- reading an edited region back ----------------------------------------


def _region_body(dir_state: Mapping[str, bytes], region: NativeParsedRegionV1) -> str:
    content = dir_state.get(region.path)
    if content is None:  # pragma: no cover - the parse read this file to find the region
        raise NativeCompileError(f"edited region {region.region_id} names an unreadable file")
    overlay = region.line_overlay
    return content[overlay.start_byte : overlay.end_byte].decode("utf-8", errors="replace")


def _object_from_text(baseline: ClaimObject, text: str) -> ClaimObject:
    """Invert :func:`~cruxible_core.playbill.native.lens._object_lines`, exactly.

    The lens is the single producer of a value's spelling, so this is its single
    inverse, and it reads the shape from the *accepted* object rather than
    guessing from the text -- a literal that happens to look like a path is still
    a literal, because the ClaimType said so.
    """

    body = text.strip()
    if isinstance(baseline, LiteralClaimObject):
        try:
            return LiteralClaimObject(value=json.loads(body))
        except (ValueError, TypeError) as exc:
            raise NativeCompileError(
                f"an edited literal value must stay canonical JSON; quote a string value ({exc})"
            ) from exc
    if isinstance(baseline, SubjectClaimObject):
        return SubjectClaimObject(address=SemanticAddress.whole_artifact(body))
    if isinstance(baseline, ExactContentClaimObject):
        digest, _separator, span = body.partition(" bytes ")
        if not span:
            return ExactContentClaimObject(content_digest=digest.strip())
        start, _dash, end = span.partition("-")
        try:
            bounds = ContentSpan(
                content_digest=digest.strip(),
                start_byte=int(start),
                end_byte=int(end),
            )
        except (ValueError, TypeError) as exc:
            raise NativeCompileError(
                "an edited exact-content value must read as DIGEST bytes START-END"
            ) from exc
        return ExactContentClaimObject(content_digest=digest.strip(), span=bounds)
    raise NativeCompileError("unknown Claim object kind in the native lens")


def _qualifier_from_text(text: str) -> str | None:
    body = text.strip()
    return None if body in {"", "(none)"} else body


# -- accepted state, as the compiler needs to ask it ----------------------


def _authoring(
    *,
    statement: ClaimStatement,
    rationale: str,
    handoffs: tuple[dict[str, str], ...],
    claim_id: str | None = None,
    predecessor_artifact_digest: str | None = None,
    subject_shell: SubjectShell | None = None,
    claim_type_artifact: ClaimType | None = None,
) -> dict[str, Any]:
    """Build one direct-Claim authoring in the exact shape the propose surface takes.

    ``retire`` is written out as ``False`` rather than left to a default. It is
    the one field that could turn an edit into a retirement, §11.9.3 says no
    compile may ever infer one, and a law that holds because a default happened
    to be right is weaker than a law you can read in the payload.
    """

    return {
        "tag": "playbill-direct-claim-authoring-v1",
        "statement": statement.model_dump(mode="json"),
        "rationale": rationale,
        "claim_id": claim_id,
        "predecessor_artifact_digest": predecessor_artifact_digest,
        "retire": False,
        "subject_shell": (None if subject_shell is None else subject_shell.model_dump(mode="json")),
        "claim_type_artifact": (
            None if claim_type_artifact is None else claim_type_artifact.model_dump(mode="json")
        ),
        "existing_statement_handoffs": [dict(item) for item in handoffs],
    }


def _claims_by_path(state: NativeAcceptedStateV1) -> dict[str, NativeClaimRecordV1]:
    return {item.path: item for item in state.claims}


def _claim_types_by_predicate(state: NativeAcceptedStateV1) -> dict[str, ClaimType]:
    resolved: dict[str, ClaimType] = {}
    for record in state.claim_types:
        try:
            claim_type = ClaimType.model_validate(record.envelope)
        except ValueError:  # pragma: no cover - an accepted ClaimType always reassembles
            continue
        resolved[claim_type.predicate] = claim_type
    return resolved


def _handoffs_for(
    state: NativeAcceptedStateV1,
    *,
    subject: SemanticAddress,
    predicate: str,
) -> tuple[dict[str, str], ...]:
    """Disposition every live same-subject/predicate statement the base holds.

    The propose surface requires this set to be exact, and it reads it from the
    accepted base -- so the compiler reads the same base and states
    ``not_tested`` for each. An edit to a rendered field is a restatement of a
    value, not a test of the statement it supersedes, and claiming otherwise
    would put evidence in the ledger that nobody produced.
    """

    handoffs = [
        {
            "statement_digest": claim_statement_digest(record.claim.statement).tagged,
            "disposition": "not_tested",
        }
        for record in state.claims
        if record.claim.lifecycle.state == "live"
        and record.claim.statement.subject == subject
        and record.claim.statement.predicate == predicate
    ]
    return tuple(sorted(handoffs, key=lambda item: item["statement_digest"].encode("ascii")))


def _accepted_interfaces(state: NativeAcceptedStateV1) -> tuple[SemanticReuseInterfaceV1, ...]:
    """Build the §11.3 reuse surface from one accepted read.

    The evaluator builds the same surface from the candidate tree at proposal
    time; this is the client-side read of it, over the projections the served
    surface already returns, so a draft gets the same candidates before it is
    submitted as its members will be judged against after.
    """

    descriptors: dict[str, dict[str, set[str]]] = {}

    def terms_for(address: SemanticAddress) -> dict[str, set[str]]:
        key = canonical_bytes(address.model_dump(mode="json")).decode("utf-8")
        return descriptors.setdefault(key, {"aliases": set(), "tags": set(), "relations": set()})

    for record in state.claims:
        claim = record.claim
        if claim.lifecycle.state != "live":
            continue
        predicate = claim.statement.predicate
        if predicate in {ALIAS_PREDICATE, "semantic.tag"} and isinstance(
            claim.statement.object, LiteralClaimObject
        ):
            value = claim.statement.object.value
            if not isinstance(value, str):
                continue
            field = "aliases" if predicate == ALIAS_PREDICATE else "tags"
            terms_for(claim.statement.subject)[field].add(value)
        elif predicate in {DISTINCT_FROM_PREDICATE, "semantic.related_to"} and isinstance(
            claim.statement.object, SubjectClaimObject
        ):
            terms_for(claim.statement.subject)["relations"].add(
                claim.statement.object.address.artifact_path
            )
            terms_for(claim.statement.object.address)["relations"].add(
                claim.statement.subject.artifact_path
            )

    def sorted_terms(values: set[str]) -> tuple[str, ...]:
        return tuple(sorted(values, key=lambda item: item.encode("utf-8")))

    interfaces: list[SemanticReuseInterfaceV1] = []
    for shell in state.subjects:
        # A Subject read projects its identity, not its parts. `kind/id` is the
        # identity's own shape, so the reuse token is read off it rather than
        # requiring a projection field that the served surface does not publish.
        name = shell.identity.removeprefix("Subject:")
        subject_id = name.rpartition("/")[2]
        if not subject_id:
            continue
        identity = ArtifactIdentity(kind="Subject", name=name)
        address = SemanticAddress.whole_artifact(shell.path)
        found = terms_for(address)
        interfaces.append(
            SemanticReuseInterfaceV1(
                address=address,
                identity=identity,
                kind="subject",
                label=identity.qualified,
                canonical_tokens=(subject_id,),
                structural_signature_digest=subject_reuse_signature(identity),
                aliases=sorted_terms(found["aliases"]),
                tags=sorted_terms(found["tags"]),
                relation_labels=sorted_terms(found["relations"]),
            )
        )
    for predicate, claim_type in sorted(_claim_types_by_predicate(state).items()):
        namespace, _separator, name = predicate.rpartition(".")
        address = SemanticAddress.whole_artifact(f"claim-types/{namespace}/{name}.yaml")
        found = terms_for(address)
        interfaces.append(
            SemanticReuseInterfaceV1(
                address=address,
                identity=claim_type.identity,
                kind="claim-type",
                label=predicate,
                canonical_tokens=byte_sorted((predicate, predicate.rpartition(".")[2])),
                structural_signature_digest=claim_type_structural_signature(claim_type.structure),
                aliases=sorted_terms(found["aliases"]),
                tags=sorted_terms(found["tags"]),
                relation_labels=sorted_terms(found["relations"]),
            )
        )
    return tuple(
        sorted(
            interfaces,
            key=lambda item: canonical_bytes(item.address.model_dump(mode="json")),
        )
    )


# -- unlocated prose -------------------------------------------------------


def _prose_tokens(text: str) -> tuple[str, ...]:
    tokens = {
        normalized
        for raw in text.split()
        if len(normalized := normalize_discovery_term(raw.strip(_TOKEN_TRIM)))
        >= _MINIMUM_PROSE_TOKEN
    }
    return byte_sorted(tuple(tokens))


def _added_prose(
    working: bytes, baseline: bytes, *, path: str
) -> tuple[
    tuple[tuple[int, str], ...],
    NativeDraftMarkerV1 | None,
    tuple[NativeDiagnosticV1, ...],
]:
    """Report the out-of-region lines the working file has and the baseline lacks.

    A multiset difference rather than a merge: a line the render emitted stays
    the render's, a line an author added is the author's, and no three-way text
    reconciliation is attempted or needed. This locates prose; it decides
    nothing about admissibility, which is the §11.9.4 line between the textual
    UX layer and semantic law.
    """

    current = extract_prose(working, path=path)
    original = extract_prose(baseline, path=path)
    remaining = Counter(text.strip() for _line, text in original.lines if text.strip())
    added: list[tuple[int, str]] = []
    for number, text in current.lines:
        stripped = text.strip()
        if not stripped:
            continue
        if remaining[stripped] > 0:
            remaining[stripped] -= 1
            continue
        added.append((number, stripped))
    return tuple(added), current.draft_marker, current.diagnostics


def _draft_digest(path: str, lines: Sequence[str]) -> str:
    return typed_digest(
        Sha256Value,
        DRAFT_DIGEST_DOMAIN,
        {"lines": list(lines), "path": path},
    ).tagged


# -- the compiler ----------------------------------------------------------


class _Assembly:
    """Mutable accumulation for one compile; the result it returns is frozen."""

    def __init__(self) -> None:
        self.three_way: list[NativeThreeWayV1] = []
        self.drafts: list[NativeCompileDraftV1] = []
        self.members: list[NativeCompileMemberV1] = []
        self.refusals: list[NativeCompileRefusalV1] = []
        self.notices: list[NativeDiagnosticV1] = []

    def refuse(self, **fields: Any) -> None:
        self.refusals.append(NativeCompileRefusalV1(**fields))


_REGION_GUARD_CODES: Mapping[str, str] = {
    "foreign_observed": "foreign_region_not_mutable",
    "orientation": "orientation_region_not_mutable",
}

_DRAFT_GUARD_CODES: Mapping[str, str] = {
    "foreign_observed": "foreign_draft_not_mutable",
    "orientation": "orientation_draft_not_compilable",
}


def _guarded_source(
    manifest: NativeRenderManifestV1,
    path: str,
    codes: Mapping[str, str],
) -> tuple[str, str] | None:
    """Return the refusal code and the accepted source binding, when not editable.

    §11.9.4 puts the native/foreign guard in **compiler input classification**,
    derived from the accepted source binding rather than from anything a caller
    says. The manifest is that binding: it declares each file's disposition and
    its logical source, and neither is recoverable from a filename. A path the
    manifest does not carry is not foreign -- it is untracked, which is a
    different answer with a different consequence.
    """

    entry = manifest.file_for(path)
    if entry is None or entry.disposition == "native_editable":
        return None
    return (
        codes[entry.disposition],
        f"{entry.disposition} source {entry.source.plane}:{entry.source.identity}",
    )


def _compile_regions(
    assembly: _Assembly,
    *,
    dir_state: Mapping[str, bytes],
    manifest: NativeRenderManifestV1,
    dirty: Sequence[NativeParsedRegionV1],
    baseline_state: NativeAcceptedStateV1,
    head_state: NativeAcceptedStateV1,
) -> None:
    baseline_claims = _claims_by_path(baseline_state)
    head_claims = _claims_by_path(head_state)
    grouped: dict[str, list[NativeParsedRegionV1]] = {}
    for region in dirty:
        guarded = _guarded_source(manifest, region.path, _REGION_GUARD_CODES)
        if guarded is not None:
            code, binding = guarded
            assembly.refuse(
                code=code,
                path=region.path,
                region_id=region.region_id,
                address=region.address,
                message=(
                    f"{region.path} is observed under a {binding}; foreign-source drift is "
                    "evidence-currency degradation and never a write back to the source"
                ),
                required_action=(
                    "Revert the edit and propose the change on the surface that owns the "
                    "source; a foreign region is categorically not a mutation target."
                ),
            )
            continue
        grouped.setdefault(region.address.artifact_path, []).append(region)

    for claim_path in sorted(grouped, key=lambda item: item.encode("utf-8")):
        regions = sorted(grouped[claim_path], key=lambda item: item.region_id.encode("ascii"))
        region_ids = byte_sorted(tuple(item.region_id for item in regions))
        baseline_record = baseline_claims.get(claim_path)
        if baseline_record is None:
            assembly.refuse(
                code="claim_baseline_unknown",
                path=regions[0].path,
                address=regions[0].address,
                message=(
                    f"the render baseline names {claim_path}, but the accepted read at the "
                    "baseline generation does not carry it"
                ),
                required_action=(
                    "Re-render against the current accepted generation; this baseline no "
                    "longer describes accepted state."
                ),
            )
            continue

        head_record = head_claims.get(claim_path)
        outcome: NativeThreeWayOutcome
        if head_record is None:
            outcome = "absent_at_head"
        elif head_record.artifact_digest == baseline_record.artifact_digest:
            outcome = "unchanged_at_head"
        else:
            outcome = "changed_at_head"
        assembly.three_way.append(
            NativeThreeWayV1(
                address=regions[0].address,
                claim_path=claim_path,
                region_ids=region_ids,
                baseline_artifact_digest=baseline_record.artifact_digest,
                head_artifact_digest=None if head_record is None else head_record.artifact_digest,
                outcome=outcome,
            )
        )
        if outcome == "absent_at_head":
            assembly.refuse(
                code="claim_absent_at_head",
                path=regions[0].path,
                address=regions[0].address,
                message=(
                    f"{claim_path} is accepted at the render baseline but absent at the "
                    "current head, so this edit has no predecessor to succeed"
                ),
                required_action=(
                    "Re-render to see what the head says about this Claim, then edit the "
                    "field the head actually carries."
                ),
            )
            continue

        statement = baseline_record.claim.statement
        edited: list[str] = []
        failed = False
        for region in regions:
            body = _region_body(dir_state, region)
            try:
                if region.region_kind == "statement_value":
                    statement = statement.model_copy(
                        update={"object": _object_from_text(statement.object, body)}
                    )
                elif region.region_kind == "statement_qualifier":
                    statement = statement.model_copy(
                        update={"qualifier": _qualifier_from_text(body)}
                    )
                else:  # pragma: no cover - only editable kinds ever go dirty
                    raise NativeCompileError(
                        f"region kind {region.region_kind} is derived and carries no proposal"
                    )
            except (NativeCompileError, ValueError) as exc:
                failed = True
                assembly.refuse(
                    code="edited_field_not_readable",
                    path=region.path,
                    region_id=region.region_id,
                    address=region.address,
                    message=f"the edited {region.region_kind} field does not read back: {exc}",
                    required_action=(
                        "Restore the field's rendered shape and edit the value inside it; "
                        "an editable field is free-form only within its own type."
                    ),
                )
            else:
                edited.append(region.region_kind)

        if failed:
            continue
        assembly.members.append(
            NativeCompileMemberV1(
                kind="locator_successor",
                address=regions[0].address,
                claim_path=claim_path,
                predicate=statement.predicate,
                region_ids=region_ids,
                subject_path=statement.subject.artifact_path,
                authoring=_authoring(
                    statement=statement,
                    rationale=(
                        "Compiled from an edited native render: "
                        + ", ".join(byte_sorted(tuple(edited)))
                        + f" at baseline generation {manifest.coordinate.generation_root}."
                    ),
                    claim_id=baseline_record.claim.identity.name,
                    predecessor_artifact_digest=baseline_record.artifact_digest,
                    handoffs=_handoffs_for(
                        baseline_state,
                        subject=statement.subject,
                        predicate=statement.predicate,
                    ),
                ),
            )
        )


def _resolve_claim_type(
    accepted: Mapping[str, ClaimType],
    predicate: str,
) -> tuple[ClaimType, bool] | None:
    """Return the ClaimType to pin, and whether the change set must carry it.

    A descriptor predicate the instance has not accepted yet is seeded from its
    reviewed expansion rather than invented, which is why lowering a distinction
    never needs a separate vocabulary generation first.
    """

    found = accepted.get(predicate)
    if found is not None:
        return found, False
    if predicate in {ALIAS_PREDICATE, DISTINCT_FROM_PREDICATE}:
        return descriptor_claim_type(predicate), True  # type: ignore[arg-type]
    return None


def _draft_statement(
    *,
    subject: SemanticAddress,
    claim_type: ClaimType,
    value: ClaimObject,
) -> ClaimStatement:
    return ClaimStatement(
        subject=subject,
        claim_type=claim_type.identity,
        claim_type_digest=claim_type_digest(claim_type).tagged,
        predicate=claim_type.predicate,
        object=value,
        role=claim_type.permitted_roles[0],
    )


def _compile_draft(
    assembly: _Assembly,
    *,
    draft: NativeCompileDraftV1,
    baseline_state: NativeAcceptedStateV1,
    accepted_types: Mapping[str, ClaimType],
) -> NativeCompileDraftV1:
    disposition = draft.disposition
    if disposition is None or disposition.kind == "withdraw":
        return draft

    resolved = _resolve_claim_type(accepted_types, str(disposition.predicate))
    if resolved is None:
        assembly.refuse(
            code="draft_claim_type_unknown",
            path=draft.path,
            draft_id=draft.draft_id,
            message=(
                f"this draft states the predicate {disposition.predicate}, which this "
                "instance has not accepted"
            ),
            required_action=(
                "Propose the ClaimType on its own surface first; a compile pins accepted "
                "vocabulary and never invents a predicate."
            ),
        )
        return draft
    claim_type, seed_type = resolved

    subject = disposition.subject_address
    if subject is None:  # pragma: no cover - the marker validator requires one
        return draft

    shell: SubjectShell | None = None
    if disposition.kind == "new_distinct":
        shell = SubjectShell(
            identity=ArtifactIdentity(
                kind="Subject",
                name=f"{disposition.subject_kind}/{disposition.subject_id}",
            ),
            subject_kind=str(disposition.subject_kind),
            subject_id=str(disposition.subject_id),
            authority=claim_type.authority,
        )

    lowered: list[SemanticAddress] = []
    if disposition.kind == "new_distinct":
        distinct = _resolve_claim_type(accepted_types, DISTINCT_FROM_PREDICATE)
        if distinct is None:  # pragma: no cover - the descriptor seed always resolves
            return draft
        distinct_type, seed_distinct = distinct
        for candidate in draft.blocking_candidates:
            lowered.append(candidate.address)
            assembly.members.append(
                NativeCompileMemberV1(
                    kind="generated_distinct_from",
                    address=candidate.address,
                    draft_id=draft.draft_id,
                    predicate=DISTINCT_FROM_PREDICATE,
                    subject_path=subject.artifact_path,
                    authoring=_authoring(
                        statement=_draft_statement(
                            subject=subject,
                            claim_type=distinct_type,
                            value=SubjectClaimObject(address=candidate.address),
                        ),
                        rationale=(
                            f"{subject.artifact_path} is deliberately distinct from "
                            f"{candidate.identity}, stated in the change set that names it."
                        ),
                        subject_shell=shell,
                        claim_type_artifact=distinct_type if seed_distinct else None,
                        handoffs=_handoffs_for(
                            baseline_state,
                            subject=subject,
                            predicate=DISTINCT_FROM_PREDICATE,
                        ),
                    ),
                )
            )

    if disposition.kind == "extend":
        alias = _resolve_claim_type(accepted_types, ALIAS_PREDICATE)
        if alias is not None:
            alias_type, seed_alias = alias
            assembly.members.append(
                NativeCompileMemberV1(
                    kind="generated_alias",
                    address=subject,
                    draft_id=draft.draft_id,
                    predicate=ALIAS_PREDICATE,
                    subject_path=subject.artifact_path,
                    authoring=_authoring(
                        statement=_draft_statement(
                            subject=subject,
                            claim_type=alias_type,
                            value=LiteralClaimObject(value=str(disposition.alias)),
                        ),
                        rationale=(
                            f"{disposition.alias} is an accepted alternate expression for "
                            f"{subject.artifact_path}; it improves recall and acquires no "
                            "authority."
                        ),
                        claim_type_artifact=alias_type if seed_alias else None,
                        handoffs=_handoffs_for(
                            baseline_state,
                            subject=subject,
                            predicate=ALIAS_PREDICATE,
                        ),
                    ),
                )
            )

    assembly.members.append(
        NativeCompileMemberV1(
            kind="unbound_native_draft",
            address=subject,
            draft_id=draft.draft_id,
            predicate=claim_type.predicate,
            subject_path=subject.artifact_path,
            authoring=_authoring(
                statement=_draft_statement(
                    subject=subject,
                    claim_type=claim_type,
                    value=LiteralClaimObject(value=disposition.value),
                ),
                rationale=draft.text,
                subject_shell=shell,
                claim_type_artifact=claim_type if seed_type else None,
                handoffs=_handoffs_for(
                    baseline_state,
                    subject=subject,
                    predicate=claim_type.predicate,
                ),
            ),
        )
    )
    return draft.model_copy(update={"generated_distinct_from": tuple(lowered)})


def _draft_candidates(
    *,
    draft_id: str,
    text: str,
    disposition: NativeDraftDispositionV1 | None,
    interfaces: tuple[SemanticReuseInterfaceV1, ...],
    head_state: NativeAcceptedStateV1,
) -> tuple[NativeDraftCandidateV1, ...]:
    """Run the §11.3 reuse lookup for one draft, deterministically.

    Reuse and extension name no new interface, so no lookup is run for them:
    there is nothing proposed to collide. A `new_distinct` names one, and a draft
    with no disposition at all is treated as naming one so that the refusal can
    say which accepted items it would have collided with.
    """

    if disposition is not None and disposition.kind in {"reuse", "extend", "withdraw"}:
        return ()
    if disposition is not None and disposition.subject_id is not None:
        tokens: tuple[str, ...] = (disposition.subject_id,)
        identity = ArtifactIdentity(
            kind="Subject",
            name=f"{disposition.subject_kind}/{disposition.subject_id}",
        )
        address = SemanticAddress.whole_artifact(
            f"subjects/{disposition.subject_kind}/{disposition.subject_id}.yaml"
        )
    else:
        tokens = _prose_tokens(text)
        identity = ArtifactIdentity(
            kind="Subject",
            name=f"native.draft/{draft_id.removeprefix('sha256:')}",
        )
        address = SemanticAddress.whole_artifact(
            f"subjects/native.draft/{draft_id.removeprefix('sha256:')}.yaml"
        )
    if not tokens:
        return ()
    evidence = evaluate_vocabulary_reuse(
        VocabularyReuseRequestV1(
            proposal=ProposedSemanticInterfaceV1(
                address=address,
                identity=identity,
                kind="subject",
                label=identity.qualified,
                canonical_tokens=tokens,
                structural_signature_digest=subject_reuse_signature(identity),
            ),
            hints=DiscoveryHintsV1(),
            disposition=ReuseDispositionV1(kind="new_distinct"),
        ),
        accepted_interfaces=interfaces,
        coordinate=head_state.at,
        implementation_digest=head_state.at.compiler_digest,
    )
    return tuple(NativeDraftCandidateV1.from_reuse(item) for item in evidence.candidates)


def compile_native_tree(
    dir_state: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    baseline_state: NativeAcceptedStateV1,
    accepted_state_at_head: NativeAcceptedStateV1,
    ctx: RenderContextV1,
    dispositions: Sequence[NativeDraftDispositionV1] = (),
) -> NativeCompileResultV1:
    """Compile one edited working tree into one proposal's worth of input.

    ``baseline_state`` is accepted state at the generation the render was a
    checkout of, and ``accepted_state_at_head`` is accepted state now. Both are
    required and neither is optional in disguise: the first is what the edits
    were made against, the second is what they will land on, and the difference
    between them is exactly the three-way this contract owes.
    """

    if ctx.at != manifest.coordinate or ctx.at != baseline_state.at:
        raise NativeCompileError(
            "a compile context, its render manifest, and its baseline state must name "
            "one accepted generation"
        )
    if baseline_state.instance_id != accepted_state_at_head.instance_id:
        raise NativeCompileError("a compile reads one instance at two generations, not two")

    assembly = _Assembly()
    baseline_render = build_native_render(baseline_state, ctx)
    for entry in manifest.files:
        produced = baseline_render.files.get(entry.path)
        if produced is None or file_digest(produced).tagged == entry.content_digest:
            continue
        assembly.refuse(
            code="baseline_file_not_reproducible",
            path=entry.path,
            message=(
                f"{entry.path} does not reproduce from accepted state at the baseline "
                "generation its manifest names, so the bytes this tree was checked out "
                "from cannot be recovered"
            ),
            required_action=(
                "Re-render the tree. Compile subtracts a baseline it reproduced, never one "
                "it had to trust."
            ),
        )

    parsed = parse_native_tree(dir_state, manifest=manifest)
    for diagnostic in parsed.refusals:
        assembly.refuse(
            code=diagnostic.code,
            path=diagnostic.path,
            region_id=diagnostic.region_id,
            message=diagnostic.message,
            required_action=(
                diagnostic.instruction
                or "Restore the region the baseline describes; a refused region carries no "
                "proposal."
            ),
        )
    assembly.notices.extend(
        item
        for item in (
            *parsed.diagnostics,
            *(entry for file in parsed.files for entry in file.diagnostics),
        )
        if item.severity == "notice"
    )

    _compile_regions(
        assembly,
        dir_state=dir_state,
        manifest=manifest,
        dirty=tuple(item for item in parsed.regions if item.state == "dirty"),
        baseline_state=baseline_state,
        head_state=accepted_state_at_head,
    )

    supplied = {item.draft_id: item for item in dispositions}
    interfaces = _accepted_interfaces(accepted_state_at_head)
    accepted_types = _claim_types_by_predicate(accepted_state_at_head)
    drafts: list[NativeCompileDraftV1] = []
    for path in sorted(dir_state, key=lambda item: item.encode("utf-8")):
        if not path.endswith(".md"):
            continue
        added, marker, diagnostics = _added_prose(
            dir_state[path],
            baseline_render.files.get(path, b""),
            path=path,
        )
        for diagnostic in diagnostics:
            assembly.refuse(
                code=diagnostic.code,
                path=diagnostic.path,
                message=diagnostic.message,
                required_action=(
                    "State one well-formed draft disposition outside every region, or none."
                ),
            )
        if not added:
            continue
        guarded = _guarded_source(manifest, path, _DRAFT_GUARD_CODES)
        if guarded is not None:
            code, binding = guarded
            assembly.refuse(
                code=code,
                path=path,
                message=(
                    f"{path} is observed under a {binding}; text added there is local "
                    "material and never a proposal against it"
                ),
                required_action=(
                    "Move the draft into a natively editable file; a source that is not "
                    "native_editable is categorically not a mutation target."
                ),
            )
            continue
        text = "\n".join(item for _number, item in added)
        draft_id = _draft_digest(path, [item for _number, item in added])
        disposition = supplied.get(draft_id)
        if disposition is None and marker is not None:
            disposition = NativeDraftDispositionV1.from_marker(draft_id, marker)
        candidates = _draft_candidates(
            draft_id=draft_id,
            text=text,
            disposition=disposition,
            interfaces=interfaces,
            head_state=accepted_state_at_head,
        )
        draft = NativeCompileDraftV1(
            draft_id=draft_id,
            path=path,
            text=text,
            line_numbers=tuple(number for number, _item in added),
            disposition=disposition,
            candidates=candidates,
        )
        if disposition is None:
            blocking = draft.blocking_candidates
            assembly.refuse(
                code="draft_disposition_required",
                path=path,
                draft_id=draft_id,
                message=(
                    "this unlocated draft states no disposition"
                    + (
                        "; it collides with " + ", ".join(item.identity for item in blocking)
                        if blocking
                        else " and the compiler never invents semantic identity"
                    )
                ),
                required_action=(
                    "State reuse, extend, new_distinct, or withdraw for draft "
                    f"{draft_id}. A new_distinct disposition lowers into an explicit "
                    "semantic.distinct_from Claim for each named item, in this same change set."
                ),
                candidates=byte_sorted(tuple(item.identity for item in blocking)),
            )
            drafts.append(draft)
            continue
        drafts.append(
            _compile_draft(
                assembly,
                draft=draft,
                baseline_state=baseline_state,
                accepted_types=accepted_types,
            )
        )

    assembly.drafts.extend(drafts)
    return NativeCompileResultV1(
        instance_id=baseline_state.instance_id,
        baseline=baseline_state.at,
        head=accepted_state_at_head.at,
        lens_id=manifest.lens.lens_id,
        lens_version=manifest.lens.lens_version,
        evaluation_time=ctx.evaluation_time_text,
        three_way=tuple(assembly.three_way),
        drafts=tuple(assembly.drafts),
        members=tuple(assembly.members),
        refusals=tuple(assembly.refusals),
        notices=tuple(assembly.notices),
    )


def native_review_currency(
    *,
    proposal_id: str,
    candidate_digest: str,
    parent_semantic_root: str,
    attestation_signer_ids: Sequence[str] = (),
    bound_candidate_digest: str | None = None,
    superseded_signer_ids: Sequence[str] = (),
) -> NativeReviewCurrencyV1:
    """Answer whether review evidence binds the candidate that would settle.

    ``bound_candidate_digest`` is the candidate an earlier review was collected
    against -- a compile result's own digest, or the digest a reviewer signed. It
    is a caller input rather than a lookup because approvals are *stored under
    the digest they signed*: evidence for a superseded candidate is not weaker
    evidence at the current one, it is absent from it, so nothing on the current
    candidate could report the earlier act. Naming it is how the earlier act
    re-enters the conversation, and the answer is then exact.

    ``superseded_signer_ids`` names who signed that earlier candidate, so a
    report can say *whose* approval has to be collected again rather than only
    that some approval must be. The caller reads it from the earlier proposal
    through the ordinary review operation; no read here, and no operation added
    for it.

    The one thing neither input can do is *discover* the earlier proposal.
    Admissions for one lineage share a ``target_ref``, and the served reads
    resolve a proposal by identity rather than enumerating them, so "which
    proposals preceded this one" is a question the current surface cannot ask.
    That gap is deliberate and recorded: it wants one read operation over
    admissions, and adding a served operation is not this batch's to do.
    """

    signers = byte_sorted(tuple(attestation_signer_ids))
    superseded = byte_sorted(tuple(superseded_signer_ids))
    if bound_candidate_digest is not None and bound_candidate_digest != candidate_digest:
        return NativeReviewCurrencyV1(
            proposal_id=proposal_id,
            candidate_digest=candidate_digest,
            parent_semantic_root=parent_semantic_root,
            bound_candidate_digest=bound_candidate_digest,
            status="superseded_by_rebase",
            binding_signer_ids=signers,
            superseded_signer_ids=superseded,
            required_action=(
                f"Fresh approval must bind candidate {candidate_digest} at parent semantic "
                f"root {parent_semantic_root}; approval of {bound_candidate_digest} does "
                "not verify against the rebased candidate."
                + (
                    " Approvals that no longer verify: " + ", ".join(superseded) + "."
                    if superseded
                    else ""
                )
            ),
        )
    if not signers:
        return NativeReviewCurrencyV1(
            proposal_id=proposal_id,
            candidate_digest=candidate_digest,
            parent_semantic_root=parent_semantic_root,
            bound_candidate_digest=bound_candidate_digest,
            status="not_reviewed",
            required_action=(
                f"Approval must bind candidate {candidate_digest} at parent semantic root "
                f"{parent_semantic_root}."
            ),
        )
    return NativeReviewCurrencyV1(
        proposal_id=proposal_id,
        candidate_digest=candidate_digest,
        parent_semantic_root=parent_semantic_root,
        bound_candidate_digest=bound_candidate_digest,
        status="current",
        binding_signer_ids=signers,
        required_action="None: the collected approvals bind the current candidate.",
    )


__all__ = [
    "ALIAS_PREDICATE",
    "DISTINCT_FROM_PREDICATE",
    "DRAFT_DIGEST_DOMAIN",
    "NativeCompileDraftV1",
    "NativeCompileError",
    "NativeCompileMemberV1",
    "NativeCompileRefusalV1",
    "NativeCompileResultV1",
    "NativeDraftCandidateV1",
    "NativeDraftDispositionV1",
    "NativeMemberKind",
    "NativeReviewCurrencyV1",
    "NativeThreeWayOutcome",
    "NativeThreeWayV1",
    "compile_native_tree",
    "native_review_currency",
]
