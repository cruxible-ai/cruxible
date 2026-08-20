"""PC-F3-S2: the compile contract, over accepted state rather than a fixture.

Every test here seeds a real instance through the governed propose/activate
path, renders it with the real lens, edits the rendered bytes the way a person
would, and compiles. Nothing stubs accepted state, because the laws under test
are all statements about the relationship between a working tree and a ledger,
and a fixture standing in for the ledger would make them statements about the
fixture.

The two laws that shape the whole file: **editing never proposes and compiling
never accepts**, and **no compile path ever infers a retirement.**

The §11.9.6 round-trip laws themselves are not here. They live as one frozen
block in ``test_native_round_trip_laws.py`` -- including the two that run through
this contract, "compile of a render is a no-op" and "editing a derived field is a
typed refusal" -- so that the five laws have one canonical home rather than a
copy per surface. What stays here is everything §11.9.3/§11.9.4 asks of compile
that is not one of those five.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.playbill.claims import LiteralClaimObject, claim_statement_digest
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import GeneratedKeyMaterial
from cruxible_core.playbill.native import (
    NATIVE_RENDER_MANIFEST_PATH,
    NativeAcceptedStateV1,
    NativeCompileResultV1,
    NativeDraftDispositionV1,
    build_native_render,
    compile_native_tree,
    native_boundary_from_manifest,
    native_review_currency,
    parse_native_tree,
    render_context_from_manifest,
    verify_native_locator,
)
from cruxible_core.playbill.native.grammar import (
    NativeDraftMarkerV1,
    parse_region_open,
    render_draft_marker,
    render_region_open,
)
from cruxible_core.playbill.native.manifest import NativeRenderManifestV1
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    DirectClaimBatchProposalV1,
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
    service_propose_playbill_claims,
)
from tests.test_playbill._knowledge_loop_support import (
    accept_proposal,
    activate,
    work_item_query,
)
from tests.test_playbill._native_support import (
    WI_42,
    WI_43,
    native_context,
    native_state,
    seed_native_instance,
)

READY = b'\n"ready"\n'
DONE = b'\n"done"\n'
QUALIFIER = b"\n(none)\n"
PREDICATE = "project.work_item.status"


def _seeded(
    tmp_path: Path,
) -> tuple[PlaybillInstance, GeneratedKeyMaterial, dict[str, bytes], NativeRenderManifestV1]:
    """Seed, render, and hand back the tree as an operator would have it on disk."""

    instance, owner = seed_native_instance(tmp_path)
    state = native_state(instance)
    render = build_native_render(state, native_context(state))
    return instance, owner, dict(render.files), render.manifest


def _compiled(
    instance: PlaybillInstance,
    files: dict[str, bytes],
    manifest: NativeRenderManifestV1,
    *,
    dispositions: tuple[NativeDraftDispositionV1, ...] = (),
) -> NativeCompileResultV1:
    """Compile exactly as the CLI does: baseline at G, head now, one context."""

    baseline_at = PlaybillAcceptedCoordinate.model_validate(
        manifest.coordinate.model_dump(mode="json")
    )
    return compile_native_tree(
        files,
        manifest=manifest,
        baseline_state=native_state(
            instance,
            at=baseline_at,
            boundary=native_boundary_from_manifest(manifest),
        ),
        accepted_state_at_head=native_state(instance),
        ctx=render_context_from_manifest(manifest),
        dispositions=dispositions,
    )


def _edit(files: dict[str, bytes], path: str, old: bytes, new: bytes) -> dict[str, bytes]:
    edited = dict(files)
    assert old in edited[path], f"{old!r} is not in the rendered {path}"
    edited[path] = edited[path].replace(old, new, 1)
    return edited


def _append(files: dict[str, bytes], path: str, text: str) -> dict[str, bytes]:
    edited = dict(files)
    edited[path] = edited[path] + text.encode("utf-8")
    return edited


def _accepted_value(instance: PlaybillInstance, claim_path: str) -> object:
    record = next(item for item in native_state(instance).claims if item.path == claim_path)
    assert isinstance(record.claim.statement.object, LiteralClaimObject)
    return record.claim.statement.object.value


def _claim_path(state: NativeAcceptedStateV1, subject_id: str) -> str:
    return next(
        item.path
        for item in state.claims
        if item.claim.statement.subject.artifact_path.endswith(f"{subject_id}.yaml")
    )


def _move_head(
    instance: PlaybillInstance,
    owner: GeneratedKeyMaterial,
    *,
    claim_path: str,
    value: str,
    sequence: int,
) -> None:
    """Accept a successor for one Claim, so the head moves under an old baseline."""

    record = next(item for item in native_state(instance).claims if item.path == claim_path)
    proposed = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=record.claim.statement.model_copy(
                update={"object": LiteralClaimObject(value=value)}
            ),
            rationale=f"An independent accepted change moves the head to {value}.",
            claim_id=record.claim.identity.name,
            predecessor_artifact_digest=record.artifact_digest,
            existing_statement_handoffs=(
                ExistingStatementHandoffV1(
                    statement_digest=claim_statement_digest(record.claim.statement).tagged,
                    disposition="not_tested",
                ),
            ),
        ),
        actor_id="owner",
        proposal_name=f"head-move-{sequence}",
        timestamp="2026-08-16T20:05:00.000000Z",
    )
    activate(instance, owner, proposed, sequence=sequence)


# -- the three gates -------------------------------------------------------


def test_an_edit_stays_local_and_a_compile_accepts_nothing(tmp_path: Path) -> None:
    """Edit, compile, accept are three gates; the first two change no accepted state."""

    instance, _owner, files, manifest = _seeded(tmp_path)
    claim_path = _claim_path(native_state(instance), "wi-42")
    before = instance.accepted_coordinate()

    edited = _edit(files, WI_42, READY, DONE)
    # Gate one is behind us and nothing has been proposed: an edited file is a
    # local draft, and reading accepted state still answers what it always did.
    assert _accepted_value(instance, claim_path) == "ready"

    result = _compiled(instance, edited, manifest)

    assert [item.kind for item in result.members] == ["locator_successor"]
    assert result.compilable is True
    # Gate two produced proposal *input*. It admitted nothing, so the accepted
    # coordinate has not moved and the accepted value has not changed.
    assert instance.accepted_coordinate() == before
    assert _accepted_value(instance, claim_path) == "ready"


def test_compiled_authorings_are_exactly_the_propose_surface_input(tmp_path: Path) -> None:
    """The wire mappings compile emits validate as the one authoring model.

    Compile may not import the service layer, so this test is where the two
    shapes are pinned together: if the authoring contract moves, this fails here
    rather than at a daemon.
    """

    instance, _owner, files, manifest = _seeded(tmp_path)
    state = native_state(instance)
    claim_path = _claim_path(state, "wi-42")
    record = next(item for item in state.claims if item.path == claim_path)

    result = _compiled(instance, _edit(files, WI_42, READY, DONE), manifest)

    authoring = DirectClaimAuthoringV1.model_validate(result.authorings[0])
    assert authoring.claim_id == record.claim.identity.name
    assert authoring.predecessor_artifact_digest == record.artifact_digest
    assert authoring.retire is False
    assert isinstance(authoring.statement.object, LiteralClaimObject)
    assert authoring.statement.object.value == "done"
    # The predecessor's own statement is dispositioned, because the propose
    # surface reads that requirement off the accepted base and so does compile.
    assert [item.statement_digest for item in authoring.existing_statement_handoffs] == [
        claim_statement_digest(record.claim.statement).tagged
    ]


# -- the semantic three-way ------------------------------------------------


def test_an_unchanged_head_compiles_as_a_clean_successor(tmp_path: Path) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)

    result = _compiled(instance, _edit(files, WI_42, READY, DONE), manifest)

    three_way = result.three_way[0]
    assert three_way.outcome == "unchanged_at_head"
    assert three_way.baseline_artifact_digest == three_way.head_artifact_digest
    assert three_way.rebase_expected is False
    assert result.rebase_expected is False


def test_a_moved_head_classifies_as_changed_and_expects_a_rebase(tmp_path: Path) -> None:
    """The classification is compile's; the rebase itself belongs to the receive path."""

    instance, owner, files, manifest = _seeded(tmp_path)
    claim_path = _claim_path(native_state(instance), "wi-42")
    _move_head(instance, owner, claim_path=claim_path, value="blocked", sequence=4)

    result = _compiled(instance, _edit(files, WI_42, READY, DONE), manifest)

    three_way = result.three_way[0]
    assert three_way.outcome == "changed_at_head"
    assert three_way.baseline_artifact_digest != three_way.head_artifact_digest
    assert result.rebase_expected is True
    # It still compiles: the proposal binds the baseline and the receive path
    # performs the deterministic three-way. Compile predicts no verdict for it.
    assert result.compilable is True
    assert result.baseline != result.head


def test_a_claim_gone_at_head_refuses_rather_than_guessing_a_predecessor(
    tmp_path: Path,
) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)
    edited = _edit(files, WI_42, READY, DONE)
    head = native_state(instance)
    claim_path = _claim_path(head, "wi-42")
    thinned = head.model_copy(
        update={"claims": tuple(item for item in head.claims if item.path != claim_path)}
    )

    result = compile_native_tree(
        edited,
        manifest=manifest,
        baseline_state=native_state(
            instance,
            at=PlaybillAcceptedCoordinate.model_validate(
                manifest.coordinate.model_dump(mode="json")
            ),
            boundary=native_boundary_from_manifest(manifest),
        ),
        accepted_state_at_head=thinned,
        ctx=render_context_from_manifest(manifest),
    )

    assert [item.code for item in result.refusals] == ["claim_absent_at_head"]
    assert result.three_way[0].outcome == "absent_at_head"
    assert result.members == ()


# -- atomicity -------------------------------------------------------------


def test_one_compile_spans_two_regions_and_two_files_as_one_change_set(
    tmp_path: Path,
) -> None:
    """§11.9.4: one compile spans regions and files that form one semantic change.

    Two regions of one Claim collapse into a single successor by **typed field
    attribution** -- the value region governs the object and the qualifier region
    governs the qualifier -- while a second file contributes its own member. All
    of it is one set of authorings for one proposal.
    """

    instance, _owner, files, manifest = _seeded(tmp_path)
    edited = _edit(files, WI_42, READY, DONE)
    edited = _edit(edited, WI_42, QUALIFIER, b"\nreviewed at the release gate\n")
    edited = _edit(edited, WI_43, b'\n"blocked"\n', b'\n"ready"\n')

    result = _compiled(instance, edited, manifest)

    assert len(result.members) == 2
    assert {item.claim_path for item in result.three_way} == {
        item.claim_path for item in result.members
    }
    wi_42 = next(item for item in result.three_way if item.claim_path.endswith(".yaml"))
    assert len(wi_42.region_ids) in {1, 2}
    spanning = next(item for item in result.three_way if len(item.region_ids) == 2)
    member = next(item for item in result.members if item.claim_path == spanning.claim_path)
    authoring = DirectClaimAuthoringV1.model_validate(member.authoring)
    assert isinstance(authoring.statement.object, LiteralClaimObject)
    assert authoring.statement.object.value == "done"
    assert authoring.statement.qualifier == "reviewed at the release gate"


# -- deletion is never inferred -------------------------------------------


def test_deleting_a_rendered_file_proposes_no_retirement(tmp_path: Path) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)
    without = {path: content for path, content in files.items() if path != WI_43}

    result = _compiled(instance, without, manifest)

    assert result.members == ()
    assert result.retirements == ()
    assert result.refusals == ()
    assert any(item.code == "rendered_file_absent" for item in result.notices)


def test_deleting_the_whole_rendered_directory_proposes_no_retirement(
    tmp_path: Path,
) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)
    only_manifest = {NATIVE_RENDER_MANIFEST_PATH: files[NATIVE_RENDER_MANIFEST_PATH]}

    result = _compiled(instance, only_manifest, manifest)

    assert result.members == ()
    assert result.retirements == ()
    assert result.drafts == ()
    codes = {item.code for item in result.notices}
    assert codes == {"rendered_file_absent"}


def test_deleting_an_editable_field_is_an_edit_and_never_a_retirement(
    tmp_path: Path,
) -> None:
    """Emptying a field proposes a successor with an empty value, not a removal."""

    instance, _owner, files, manifest = _seeded(tmp_path)

    result = _compiled(instance, _edit(files, WI_42, QUALIFIER, b"\n\n"), manifest)

    assert result.retirements == ()
    authoring = DirectClaimAuthoringV1.model_validate(result.authorings[0])
    assert authoring.retire is False
    assert authoring.statement.qualifier is None


# -- refusals that pass through unreinterpreted ----------------------------


def test_deleting_a_locator_is_never_inferred_as_retirement(tmp_path: Path) -> None:
    """A region whose markers were removed is unlocated text, not a removal.

    The body stops being a bound region and becomes prose, which is a draft that
    refuses for want of a disposition. What it is never is a retirement of the
    Claim the locator used to bind, which is the §11.9.3 law under test.
    """

    instance, _owner, files, manifest = _seeded(tmp_path)
    content = files[WI_42].decode("utf-8")
    opened = next(line for line in content.splitlines() if line.startswith("<!--playbill:region "))
    stripped = content.replace(opened + "\n", "", 1).replace("<!--playbill:/region-->\n", "", 1)
    edited = dict(files)
    edited[WI_42] = stripped.encode("utf-8")

    result = _compiled(instance, edited, manifest)

    assert result.retirements == ()
    assert result.members == ()
    assert all(item.code != "derived_region_tampered" for item in result.refusals)
    assert _accepted_value(instance, _claim_path(native_state(instance), "wi-42")) == "ready"


def test_a_duplicated_locator_refuses_as_ambiguity(tmp_path: Path) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)
    copied = dict(files)
    copied["subjects/project.work_item/wi-42-copy.md"] = files[WI_42]

    result = _compiled(instance, copied, manifest)

    assert any(item.code == "locator_duplicated" for item in result.refusals)
    assert result.members == ()


def _forge_lens(files: dict[str, bytes], path: str, **forgery: object) -> dict[str, bytes]:
    """Restate every locator in one file under a forged lens, changing nothing else.

    Region identity, baseline digest, address, artifact digest, and generation
    root all survive verbatim: the only lie in the tree is which lens claims to
    have minted the region.
    """

    forged: list[str] = []
    for line in files[path].decode("utf-8").splitlines(keepends=True):
        locator = parse_region_open(line)
        if locator is None:
            forged.append(line)
            continue
        ending = line[len(line.rstrip("\n")) :]
        forged.append(render_region_open(locator.model_copy(update=forgery)) + ending)
    edited = dict(files)
    edited[path] = "".join(forged).encode("utf-8")
    return edited


@pytest.mark.parametrize(
    "forgery",
    [{"lens_id": "playbill-native-markdown-forged"}, {"lens_version": 99}],
    ids=["lens_id", "lens_version"],
)
def test_a_forged_locator_lens_refuses_at_the_parse_gate(
    tmp_path: Path, forgery: dict[str, object]
) -> None:
    """Compile may not admit a lens identity the standalone verifier refuses."""

    instance, _owner, files, manifest = _seeded(tmp_path)
    forged = _forge_lens(_edit(files, WI_42, READY, DONE), WI_42, **forgery)

    forged_locator = next(
        locator
        for line in forged[WI_42].decode("utf-8").splitlines()
        if (locator := parse_region_open(line)) is not None
    )
    parsed = parse_native_tree(forged, manifest=manifest)
    regions = tuple(item for item in parsed.regions if item.path == WI_42)
    result = _compiled(instance, forged, manifest)
    verdict = verify_native_locator(
        forged_locator,
        state=native_state(instance),
        manifest=manifest,
        path=WI_42,
    )

    # The forged locators kept the identity the genuine render minted, so every
    # region is found in the baseline and only the lens claim is false.
    assert regions
    assert all(manifest.baseline_for(item.region_id) is not None for item in regions)
    assert {item.state for item in regions} == {"tampered"}
    assert all("locator_lens_mismatch" in item.reason_codes for item in regions)
    assert any(item.code == "locator_lens_mismatch" for item in parsed.refusals)
    assert verdict.verdict == "refused"
    assert "lens_mismatch" in verdict.reason_codes
    assert result.compilable is False
    assert result.members == ()
    assert result.authorings == ()


def test_a_foreign_region_is_categorically_not_a_mutation_target(tmp_path: Path) -> None:
    """The guard reads the accepted source binding, never caller-supplied metadata."""

    from cruxible_core.playbill.canonical import file_digest
    from cruxible_core.playbill.coverage.contracts import LogicalSourceIdentityV1
    from cruxible_core.playbill.native.grammar import NativeFileMarkerV1, render_file_marker
    from cruxible_core.playbill.native.manifest import NativeRenderFileV1, native_render_digest
    from cruxible_core.playbill.query.grammar import byte_sorted

    instance, _owner, files, manifest = _seeded(tmp_path)
    path = "external/vendor-notes.md"
    body = (
        render_file_marker(
            NativeFileMarkerV1(
                lens_id=manifest.lens.lens_id,
                lens_version=manifest.lens.lens_version,
                path=path,
                disposition="foreign_observed",
                generation_root=manifest.coordinate.generation_root,
                evaluation_time=manifest.evaluation_time.isoformat(),
                scope_digest=manifest.scope_digest,
            )
        )
        + "\n# Vendor notes\n"
    ).encode("utf-8")
    entry = NativeRenderFileV1(
        path=path,
        content_digest=file_digest(body).tagged,
        byte_length=len(body),
        disposition="foreign_observed",
        source=LogicalSourceIdentityV1(plane="external", identity="vendor.release.notes"),
    )
    inventory = tuple(sorted((*manifest.files, entry), key=lambda item: item.path.encode("utf-8")))
    assert byte_sorted(tuple(item.path for item in inventory))
    observed = manifest.model_copy(
        update={"files": inventory, "render_digest": native_render_digest(inventory)}
    )
    tree = dict(files)
    tree[path] = body + b"\nThe vendor now ships weekly.\n"

    result = _compiled(instance, tree, observed)

    refusal = next(item for item in result.refusals if item.path == path)
    assert refusal.code == "foreign_draft_not_mutable"
    assert "vendor.release.notes" in refusal.message
    assert result.members == ()
    assert result.drafts == ()


# -- unlocated drafts ------------------------------------------------------


def _draft_block(marker: NativeDraftMarkerV1 | None, prose: str) -> str:
    lines = ["", "## Draft", ""]
    if marker is not None:
        lines.append(render_draft_marker(marker))
    lines.extend([prose, ""])
    return "\n".join(lines)


def test_an_unlocated_draft_without_a_disposition_refuses_naming_the_candidate(
    tmp_path: Path,
) -> None:
    """§11.9.3: the compiler never invents semantic identity, and says whose it would."""

    instance, _owner, files, manifest = _seeded(tmp_path)
    edited = _append(
        files,
        WI_42,
        _draft_block(None, "The wi-42 rollout needs its own tracked item."),
    )

    result = _compiled(instance, edited, manifest)

    refusal = next(item for item in result.refusals if item.code == "draft_disposition_required")
    assert "Subject:project.work_item/wi-42" in refusal.candidates
    assert "wi-42" in refusal.message
    assert "new_distinct" in refusal.required_action
    assert result.members == ()
    assert len(result.drafts) == 1
    assert result.drafts[0].disposition is None


def test_new_distinct_lowers_into_distinct_from_claims_in_the_same_change_set(
    tmp_path: Path,
) -> None:
    """The lowering is deterministic, exposed before submission, and atomic."""

    instance, _owner, files, manifest = _seeded(tmp_path)
    marker = NativeDraftMarkerV1(
        disposition="new_distinct",
        subject_kind="project.epic",
        subject_id="wi-42",
        predicate=PREDICATE,
        value="ready",
    )
    edited = _append(
        files,
        WI_42,
        _draft_block(marker, "The wi-42 epic is the programme, not the work item."),
    )

    result = _compiled(instance, edited, manifest)

    assert result.refusals == ()
    draft = result.drafts[0]
    assert draft.disposition is not None
    assert draft.disposition.kind == "new_distinct"
    assert [item.artifact_path for item in draft.generated_distinct_from] == [
        "subjects/project.work_item/wi-42.yaml"
    ]
    kinds = [item.kind for item in result.members]
    assert kinds == ["generated_distinct_from", "unbound_native_draft"]
    generated = DirectClaimAuthoringV1.model_validate(result.authorings[0])
    assert generated.statement.predicate == "semantic.distinct_from"
    assert generated.statement.subject.artifact_path == "subjects/project.epic/wi-42.yaml"
    assert generated.subject_shell is not None
    assert generated.claim_type_artifact is not None
    drafted = DirectClaimAuthoringV1.model_validate(result.authorings[1])
    assert drafted.rationale.endswith("not the work item.")
    assert drafted.retire is False


def test_reuse_authors_the_draft_against_the_existing_target(tmp_path: Path) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)
    marker = NativeDraftMarkerV1(
        disposition="reuse",
        target_path="subjects/project.work_item/wi-42.yaml",
        predicate=PREDICATE,
        value="done",
    )
    edited = _append(files, WI_42, _draft_block(marker, "wi-42 shipped this morning."))

    result = _compiled(instance, edited, manifest)

    assert result.refusals == ()
    assert [item.kind for item in result.members] == ["unbound_native_draft"]
    authoring = DirectClaimAuthoringV1.model_validate(result.authorings[0])
    assert authoring.statement.subject.artifact_path == "subjects/project.work_item/wi-42.yaml"
    assert authoring.subject_shell is None
    # Reuse names no new interface, so there is nothing for a reuse lookup to
    # collide with and no candidate list to render.
    assert result.drafts[0].candidates == ()


def test_extend_adds_an_accepted_alias_beside_the_drafted_claim(tmp_path: Path) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)
    marker = NativeDraftMarkerV1(
        disposition="extend",
        target_path="subjects/project.work_item/wi-42.yaml",
        predicate=PREDICATE,
        value="done",
        alias="release-blocker",
    )
    edited = _append(files, WI_42, _draft_block(marker, "Teams call wi-42 the release blocker."))

    result = _compiled(instance, edited, manifest)

    assert result.refusals == ()
    assert [item.kind for item in result.members] == ["generated_alias", "unbound_native_draft"]
    alias = DirectClaimAuthoringV1.model_validate(result.authorings[0])
    assert alias.statement.predicate == "semantic.alias"
    assert isinstance(alias.statement.object, LiteralClaimObject)
    assert alias.statement.object.value == "release-blocker"


def test_withdraw_drops_local_material_and_proposes_nothing(tmp_path: Path) -> None:
    """A withdrawn draft was never accepted; withdrawing it retires nothing."""

    instance, _owner, files, manifest = _seeded(tmp_path)
    marker = NativeDraftMarkerV1(disposition="withdraw")
    edited = _append(files, WI_42, _draft_block(marker, "On reflection, wi-42 needs nothing."))

    result = _compiled(instance, edited, manifest)

    assert result.refusals == ()
    assert result.members == ()
    assert result.retirements == ()
    assert result.drafts[0].disposition is not None
    assert result.drafts[0].disposition.kind == "withdraw"


def test_a_disposition_supplied_beside_the_tree_answers_the_same_refusal(
    tmp_path: Path,
) -> None:
    """In-file and out-of-file dispositions are two spellings of one act."""

    instance, _owner, files, manifest = _seeded(tmp_path)
    edited = _append(files, WI_42, _draft_block(None, "The wi-42 rollout needs its own item."))
    refused = _compiled(instance, edited, manifest)
    draft_id = refused.drafts[0].draft_id

    result = _compiled(
        instance,
        edited,
        manifest,
        dispositions=(
            NativeDraftDispositionV1(
                draft_id=draft_id,
                kind="reuse",
                target_path="subjects/project.work_item/wi-42.yaml",
                predicate=PREDICATE,
                value="done",
            ),
        ),
    )

    assert result.refusals == ()
    assert [item.kind for item in result.members] == ["unbound_native_draft"]
    # The identity is stable: naming a draft does not change what the draft is.
    assert result.drafts[0].draft_id == draft_id


def test_a_draft_naming_an_unaccepted_predicate_refuses(tmp_path: Path) -> None:
    instance, _owner, files, manifest = _seeded(tmp_path)
    marker = NativeDraftMarkerV1(
        disposition="reuse",
        target_path="subjects/project.work_item/wi-42.yaml",
        predicate="project.work_item.invented",
        value="done",
    )
    edited = _append(files, WI_42, _draft_block(marker, "An invented predicate."))

    result = _compiled(instance, edited, manifest)

    assert [item.code for item in result.refusals] == ["draft_claim_type_unknown"]
    assert result.members == ()


# -- review currency, headless --------------------------------------------


def test_review_currency_is_current_only_when_evidence_binds_the_candidate() -> None:
    digest = "sha256:" + "a" * 64
    root = "sha256:" + "b" * 64

    assert (
        native_review_currency(
            proposal_id="p", candidate_digest=digest, parent_semantic_root=root
        ).status
        == "not_reviewed"
    )
    current = native_review_currency(
        proposal_id="p",
        candidate_digest=digest,
        parent_semantic_root=root,
        attestation_signer_ids=("owner",),
        bound_candidate_digest=digest,
    )
    assert current.status == "current"
    assert current.binding_signer_ids == ("owner",)


def test_a_rebase_marks_prior_review_evidence_superseded_by_rebase() -> None:
    """The mechanism is candidate-digest keying; this is only its name."""

    rebased = "sha256:" + "c" * 64
    signed = "sha256:" + "d" * 64
    root = "sha256:" + "e" * 64

    currency = native_review_currency(
        proposal_id="p",
        candidate_digest=rebased,
        parent_semantic_root=root,
        attestation_signer_ids=(),
        bound_candidate_digest=signed,
    )

    assert currency.status == "superseded_by_rebase"
    assert currency.bound_candidate_digest == signed
    assert rebased in currency.required_action
    assert root in currency.required_action


def test_a_compile_context_must_name_its_own_baseline_generation(tmp_path: Path) -> None:
    instance, owner, files, manifest = _seeded(tmp_path)
    claim_path = _claim_path(native_state(instance), "wi-42")
    _move_head(instance, owner, claim_path=claim_path, value="blocked", sequence=4)
    head = native_state(instance)

    with pytest.raises(Exception, match="one accepted generation"):
        compile_native_tree(
            files,
            manifest=manifest,
            baseline_state=head,
            accepted_state_at_head=head,
            ctx=render_context_from_manifest(manifest),
        )


# -- the three-way, performed by the machinery that owns it ----------------


def _submit(
    instance: PlaybillInstance,
    result: NativeCompileResultV1,
    manifest: NativeRenderManifestV1,
    *,
    name: str,
    timestamp: str,
) -> DirectClaimBatchProposalV1:
    """Submit a compile through the ordinary propose surface, bound to baseline G."""

    return service_propose_playbill_claims(
        instance,
        authorings=tuple(DirectClaimAuthoringV1.model_validate(item) for item in result.authorings),
        actor_id="owner",
        proposal_name=name,
        timestamp=timestamp,
        base=PlaybillAcceptedCoordinate.model_validate(manifest.coordinate.model_dump(mode="json")),
    )


def test_a_conflicting_head_refuses_with_a_typed_member_conflict(tmp_path: Path) -> None:
    """Compile binds G; the receive path runs the §3.4 three-way and refuses exactly.

    No merge in the compiler decided this. The proposal declared the baseline it
    was authored against, the receive path saw the head had moved incompatibly
    for that member, and the deterministic rebase reported the conflict per
    member -- which is the whole of "git merge never decides admissibility".
    """

    instance, owner, files, manifest = _seeded(tmp_path)
    claim_path = _claim_path(native_state(instance), "wi-42")
    _move_head(instance, owner, claim_path=claim_path, value="blocked", sequence=4)

    result = _compiled(instance, _edit(files, WI_42, READY, DONE), manifest)
    assert result.three_way[0].outcome == "changed_at_head"

    batch = _submit(
        instance,
        result,
        manifest,
        name="native-compile-conflict",
        timestamp="2026-08-16T20:06:00.000000Z",
    )

    evaluation = batch.proposal.proposal.evaluation
    assert evaluation.rebased is True
    assert evaluation.verdict == "refused"
    assert [item.code for item in evaluation.diagnostics] == ["playbill.rebase.member_conflict"]
    assert evaluation.candidate_digest is None


def test_a_rebase_moves_the_candidate_digest_review_evidence_must_bind(
    tmp_path: Path,
) -> None:
    """Approval-voiding is structural: approvals are stored under the digest signed.

    The same authorings, submitted at the same baseline, produce one candidate
    before an unrelated acceptance and a different one after. An approval of the
    first is not weaker evidence for the second; it is filed under a digest the
    second does not have, which is what `superseded_by_rebase` reports.
    """

    instance, owner, files, manifest = _seeded(tmp_path)
    result = _compiled(instance, _edit(files, WI_42, READY, DONE), manifest)

    first = _submit(
        instance,
        result,
        manifest,
        name="native-compile-first",
        timestamp="2026-08-16T20:06:00.000000Z",
    )
    assert first.proposal.proposal.evaluation.rebased is False
    before = first.proposal.proposal.evaluation.candidate_digest
    assert before is not None

    # An unrelated accepted change moves the head without touching this Claim.
    inspection = service_propose_playbill_query_definition(
        instance,
        query=work_item_query(name="project.work_items.secondary"),
        actor_id="owner",
        proposal_name="unrelated-entrypoint",
        timestamp="2026-08-16T20:07:00.000000Z",
    )
    accept_proposal(instance, owner, inspection, sequence=4)

    second = _submit(
        instance,
        result,
        manifest,
        name="native-compile-second",
        timestamp="2026-08-16T20:08:00.000000Z",
    )
    evaluation = second.proposal.proposal.evaluation
    assert evaluation.rebased is True
    assert evaluation.verdict == "candidate"
    after = evaluation.candidate_digest
    assert after is not None and after != before

    # The approval store is keyed by candidate digest, so evidence for the first
    # candidate is absent from the second rather than merely outweighed.
    assert instance.proposal_evidence().read_approvals(after) == ()
    currency = native_review_currency(
        proposal_id=second.proposal.proposal.admission.proposal_id,
        candidate_digest=after,
        parent_semantic_root=second.proposal.proposal.candidate.candidate.parent_semantic_root,
        bound_candidate_digest=before,
    )
    assert currency.status == "superseded_by_rebase"
    assert after in currency.required_action
