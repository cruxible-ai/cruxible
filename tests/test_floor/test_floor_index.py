"""The floor index: ledger-derived stamps, and incremental renders equal to cold ones."""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.floor.floor_index import (
    FloorRender,
    advance_floor_index,
    floor_render_from,
)
from tests.core_support._write_support import (
    KIND,
    _claim_type,
    caller,
    seed_write_surface,
)

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"


def _write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> Any:
    request = WriteRequest.model_validate(
        {"because": "The writer checked it.", "changes": list(changes), **options}
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _set(subject: str, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


def accept_edit(instance: PlaybillInstance, name: str, edits: dict[str, bytes]) -> None:
    """Accept one direct tree edit through the ordinary proposal and activation."""

    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**instance.tree_at(base.git_oid), **edits},
        timestamp="2026-09-30T12:00:00.000000Z",
    )
    assert proposed.candidate is not None, [
        (item.code, item.message) for item in proposed.evaluation.diagnostics
    ]
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


def migrate_claim_type(instance: PlaybillInstance, predicate: str, **update: Any) -> None:
    """Accept a successor of one ClaimType, carrying every Claim of it to the successor."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claim_types import claim_type_digest, parse_claim_type
    from cruxible_core.claims.claim_type_migrations import (
        ClaimTypeDependentDisposition,
        ClaimTypeMigrationRequest,
        service_migrate_claim_type,
    )

    path = claim_type_path(predicate)
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    state = update.pop("state", "live")
    successor = current.model_copy(
        update={
            **update,
            "lifecycle": ArtifactLifecycle(
                state=state, predecessor_digest=claim_type_digest(current).tagged
            ),
        }
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        claims = sorted(
            str(row[0]).removeprefix("Claim:")
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE predicate=?", (predicate,)
            )
        )
    if not claims:
        accept_edit(
            instance,
            f"type-{predicate.replace('.', '-')}-{len(instance.accepted_history())}",
            {path: render_claim_type(successor)},
        )
        return
    migration = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequest(
            mode="submit",
            successor=successor,
            dependents=tuple(
                ClaimTypeDependentDisposition(
                    identity=ArtifactIdentity(kind="Claim", name=claim), disposition="successor"
                )
                for claim in claims
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    proposal = migration.proposal.proposal
    assert proposal.candidate is not None, proposal.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


LEAD = _claim_type("lead", object_kind="subject", roles=("normative",), object_kinds=(KIND,))


def add_lead_field(instance: PlaybillInstance) -> None:
    """A single-valued Subject field, so set moves one edge in place."""

    accept_edit(instance, "lead-field", {claim_type_path(LEAD.predicate): render_claim_type(LEAD)})


def _cold(instance: PlaybillInstance, generation: int | None = None) -> FloorRender:
    instance.floor_current_memo.clear()
    if generation is None:
        return advance_floor_index(instance)
    with instance.accepted_history_reader() as history:
        oid = history.generation(generation).git_oid
    return advance_floor_index(instance, instance.coordinate_for_oid(oid))


def _bytes(render: FloorRender) -> dict[str, tuple[bytes, int]]:
    return dict(render.files)


def test_incremental_renders_equal_cold_ones_at_every_generation(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    steps = [
        (_set(WI1, "status", "ready"),),
        (_set(WI2, "title", "Two"), _set(WI1, "ruling", "Text.\nMore text.\n")),
        (_set(WI1, "status", "blocked"),),
        ({"op": "add", "subject": WI1, "field": "governs", "value": WI2},),
        (_set(WI3, "ruling", "".join(f"Clause {i}.\n" for i in range(60))),),
        (_set(WI3, "ruling", "Short now.\n"),),
    ]
    warm: list[FloorRender] = []
    advance_floor_index(instance)
    for changes in steps:
        _write(instance, *changes)
        warm.append(advance_floor_index(instance))
    for render in warm:
        assert _bytes(render) == _bytes(_cold(instance, render.generation)), render.generation
    # A coalesced advance over every step at once is the same floor too.
    instance.floor_current_memo.clear()
    first = _cold(instance, warm[0].generation)
    assert first.generation < warm[-1].generation
    assert _bytes(advance_floor_index(instance)) == _bytes(warm[-1])


def test_a_stamp_moves_exactly_when_a_files_inputs_do(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"), _set(WI2, "status", "done"))
    before = advance_floor_index(instance)
    _write(instance, _set(WI2, "status", "blocked"))
    after = advance_floor_index(instance)
    for path, (content, changed_at) in after.files.items():
        old = before.files.get(path)
        if changed_at <= before.generation:
            assert old == (content, changed_at), path
        else:
            assert old is None or old[0] != content, path
    assert after.files[f"current/{WI2}.yaml"][1] == after.generation
    assert after.files[f"current/{WI1}.yaml"][1] < after.generation


def test_any_generation_renders_from_the_index_in_either_direction(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    _write(instance, _set(WI2, "title", "Two"))
    _write(instance, _set(WI1, "status", "done"), _set(WI3, "title", "Three"))
    head = advance_floor_index(instance)
    for generation in range(head.generation + 1):
        backwards = floor_render_from(instance, head, generation)
        assert _bytes(backwards) == _bytes(_cold(instance, generation)), generation
        forwards = floor_render_from(instance, backwards, head.generation)
        assert _bytes(forwards) == _bytes(head)


def test_a_moved_edge_restamps_its_old_and_new_targets(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    add_lead_field(instance)
    _write(instance, _set(WI1, "lead", WI2))
    first = advance_floor_index(instance)
    assert f"lead <- {WI1}" in first.files[f"current/{WI2}.yaml"][0].decode()
    _write(instance, _set(WI1, "lead", WI3))
    moved = advance_floor_index(instance)
    old_target = moved.files[f"current/{WI2}.yaml"]
    new_target = moved.files[f"current/{WI3}.yaml"]
    assert "incoming:" not in old_target[0].decode()
    assert f"lead <- {WI1}" in new_target[0].decode()
    assert old_target[1] == new_target[1] == moved.generation
    assert _bytes(moved) == _bytes(_cold(instance))
    # Back at the first generation, the edge points where it did.
    back = floor_render_from(instance, moved, first.generation)
    assert _bytes(back) == _bytes(first)


def test_a_new_claim_type_restamps_no_subject_that_does_not_show_it(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "title", "One"), _set(WI2, "status", "done"))
    before = advance_floor_index(instance)
    add_lead_field(instance)
    after = advance_floor_index(instance)
    assert after.generation == before.generation + 1
    # The ontology files show the new ClaimType; no current/ file does.
    assert {
        path: item
        for path, item in after.files.items()
        if not path.startswith(("changes/", "ontology/"))
    } == {
        path: item
        for path, item in before.files.items()
        if not path.startswith(("changes/", "ontology/"))
    }
    assert _bytes(after) == _bytes(_cold(instance))


def _shown_chain(path: str, sequence: int, cardinality: str = "one") -> Any:
    """A one-revision ClaimType history whose shown view (cardinality, live) changed at it."""

    from cruxible_core.service.floor.floor_definitions import DefinitionRevision

    return DefinitionRevision(
        sequence, None, None, None, reach=frozenset(), views=((cardinality, True),)
    )


def test_a_field_stamp_follows_its_claim_type_and_its_short_name_shadow() -> None:
    from types import SimpleNamespace

    from cruxible_core.derived.persistent import PMap
    from cruxible_core.service.floor.floor_index import _Stamps

    paths = {
        "claim-types/project.work_item/a.json": 3,
        "claim-types/a/b.json": 7,
        "claim-types/project.work_item/c.json": 2,
    }
    inputs: Any = SimpleNamespace(
        definitions=PMap((path, _shown_chain(path, at)) for path, at in paths.items())
    )
    stamps = _Stamps(inputs, frozenset())
    # project.work_item.a.b shows as "a.b" unless the predicate a.b is live: both move it.
    assert stamps.name("project.work_item.a.b", "project.work_item") == 7
    assert stamps.name("project.work_item.c", "project.work_item") == 2
    # A field of another kind keeps its full name and only its own type moves it.
    assert stamps.name("project.work_item.c", "other.kind") == 2


def test_a_claim_type_revision_restamps_subjects_only_when_it_changes_what_they_show(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A shadow ClaimType revision that shows nothing new moves no field; retiring it renames one.

    ``project.work_item.ext.note`` shows as its full name while the predicate
    ``ext.note`` is live. A revision of that shadow rewrites none of the
    field's Claims (they pin their own ClaimType), so only what the revision
    shows -- cardinality and liveness -- may restamp the Subject.
    """

    from cruxible_core.service.floor import floor_index

    instance, _owner = seed_write_surface(tmp_path)
    note = _claim_type("ext.note", literal_schema={"type": "string"})
    accept_edit(instance, "ext-note", {claim_type_path(note.predicate): render_claim_type(note)})
    _write(instance, _set(WI1, "ext.note", "Hello"), _set(WI2, "title", "Two"))
    shadow = note.model_copy(
        update={
            "predicate": "ext.note",
            "identity": note.identity.model_copy(update={"name": "ext.note"}),
        }
    )
    accept_edit(instance, "shadow", {claim_type_path("ext.note"): render_claim_type(shadow)})
    before = advance_floor_index(instance)
    assert f"{KIND}.ext.note: Hello" in before.files[f"current/{WI1}.yaml"][0].decode()
    rendered: list[str] = []
    render_subject = floor_index.render_subject

    def counting(**kwargs: Any) -> Any:
        rendered.append(kwargs["path"])
        return render_subject(**kwargs)

    monkeypatch.setattr(floor_index, "render_subject", counting)
    # A revision that leaves cardinality and liveness as they were (a v1
    # ClaimType carries no description, so this one changes only its
    # lineage) shows a Subject file nothing new: none is rendered, restamped
    # or delivered.
    migrate_claim_type(instance, "ext.note", description="A shadowing predicate.")
    described = advance_floor_index(instance)
    assert rendered == []
    assert not {path for path in described.steps[-1].written if path.startswith("current/")}
    for name in (f"current/{WI1}.yaml", f"current/{WI2}.yaml", f"current/{KIND}/INDEX"):
        assert described.files[name] is before.files[name], name
    # Retiring the shadow renames the field: that Subject alone restamps.
    migrate_claim_type(instance, "ext.note", state="retired")
    retired = advance_floor_index(instance)
    assert rendered == [f"subjects/{WI1}.json"]
    wi1 = retired.files[f"current/{WI1}.yaml"]
    assert "\next.note: Hello" in wi1[0].decode() and wi1[1] == retired.generation
    monkeypatch.setattr(floor_index, "render_subject", render_subject)
    assert _bytes(retired) == _bytes(_cold(instance))
    # Patching back across both revisions gives the floors they had.
    back = floor_render_from(instance, retired, before.generation)
    assert {key: value for key, value in back.files.items() if key.startswith("current/")} == {
        key: value for key, value in before.files.items() if key.startswith("current/")
    }


def test_an_advance_renders_only_the_subjects_its_change_reaches(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from cruxible_core.service.floor import floor_index

    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"), _set(WI2, "status", "done"))
    _write(instance, _set(WI3, "title", "Three"))
    advance_floor_index(instance)
    rendered: list[str] = []
    render_subject = floor_index.render_subject

    def counting(**kwargs: Any) -> Any:
        rendered.append(kwargs["path"])
        return render_subject(**kwargs)

    monkeypatch.setattr(floor_index, "render_subject", counting)
    _write(instance, _set(WI2, "title", "Two"))
    warm = advance_floor_index(instance)
    assert rendered == [f"subjects/{WI2}.json"]
    # The one restamped Subject, its kind INDEX, the change and the sources
    # line are the step's only files; the rest of the floor was not re-read.
    (step,) = warm.steps[-1:]
    assert step.base == warm.generation - 1 and step.generation == warm.generation
    assert f"current/{WI2}.yaml" in step.written
    assert f"current/{WI1}.yaml" not in step.written
    assert f"current/{WI3}.yaml" not in step.written
    monkeypatch.setattr(floor_index, "render_subject", render_subject)
    assert _bytes(warm) == _bytes(_cold(instance))


def test_each_file_is_hashed_once_when_rendered(tmp_path: Any, monkeypatch: Any) -> None:
    from cruxible_core.service.floor import floor_index

    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"), _set(WI2, "status", "done"))
    head = advance_floor_index(instance)
    head.manifest()
    hashed: list[int] = []
    digest = floor_index.content_digest

    def counting(content: bytes) -> str:
        hashed.append(len(content))
        return digest(content)

    monkeypatch.setattr(floor_index, "content_digest", counting)
    _write(instance, _set(WI1, "status", "blocked"))
    moved = advance_floor_index(instance)
    assert len(hashed) == len(moved.steps[-1].written)
    assert len(hashed) < len(moved.files)
    for path, (content, _changed) in moved.files.items():
        assert moved.digests[path] == (digest(content), len(content)), path


def test_a_warm_advance_shares_the_floor_instead_of_copying_it(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from cruxible_core.derived import persistent
    from cruxible_core.service.floor import floor_index

    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, *(_set(f"{KIND}/bulk-{index}", "title", f"T{index}") for index in range(40)))
    first = advance_floor_index(instance)
    built: list[int] = []
    init = persistent.PMap.__init__

    def recording(self: Any, items: Any = None) -> None:
        if items is not None and not hasattr(items, "__len__"):
            items = list(items)
        built.append(0 if items is None else len(items))
        init(self, items)

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a warm advance rebuilt a whole input")

    monkeypatch.setattr(persistent.PMap, "__init__", recording)
    for name in ("build_floor_inputs", "_all_live_sequences"):
        monkeypatch.setattr(floor_index, name, refused)
    monkeypatch.setattr(floor_index, "build_sources_ledger", refused)
    _write(instance, _set(f"{KIND}/bulk-3", "status", "ready"))
    warm = advance_floor_index(instance)
    monkeypatch.undo()
    # Nothing population-sized was built: every map is the previous one with
    # the change applied, sharing all other entries.
    assert max(built, default=0) <= 2
    assert warm.files[f"current/{KIND}/bulk-7.yaml"] is first.files[f"current/{KIND}/bulk-7.yaml"]
    assert _bytes(warm) == _bytes(_cold(instance))


def _counted_chain(size: int, visits: list[int]) -> Any:
    """A Claim history of ``size`` revisions whose nodes count every walk step through them."""

    from cruxible_core.service.floor.floor_index import _Revisions

    class Counted(_Revisions):
        __slots__ = ()

        def __getattribute__(self, name: str) -> Any:
            if name in ("older", "target", "sequence"):
                visits[0] += 1
            return super().__getattribute__(name)

    chain: Any = None
    for sequence in range(1, size + 1):
        chain = Counted(sequence, "subjects/k/old.json", chain)
    visits[0] = 0
    return chain


def test_one_revision_extends_or_cuts_a_claim_history_without_walking_it(
    monkeypatch: Any,
) -> None:
    from types import SimpleNamespace

    from cruxible_core.service.floor import floor_index

    monkeypatch.setattr(floor_index, "_object", lambda claim: claim)
    path = "claims/k/edge.json"
    walked: dict[int, list[int]] = {}
    for size in (64, 512):
        visits = [0]
        known = _counted_chain(size, visits)
        history: Any = SimpleNamespace(
            member_sequences_between=lambda paths, low, high: {path: (high,)}
        )
        for target, gained in (("subjects/k/old.json", False), ("subjects/k/new.json", True)):
            found = floor_index._object_histories(
                None,  # type: ignore[arg-type]
                history,
                {path: target},  # type: ignore[dict-item]
                [path],
                at_most=size + 1,
                known={path: known},
                since=size,
            )[path]
            # The new revision is one node over the known history, shared whole.
            assert found.older is known and len(found) == size + 1
            assert found.targets[target] == (size + 1 if not gained else 1)
            added, removed = floor_index._target_moves(known, found)
            assert added == ({target} if gained else set()) and removed == set()
        # Cut back one generation: the older chain, shared, not rebuilt.
        cut = floor_index._object_histories(
            None,  # type: ignore[arg-type]
            history,
            {path: "subjects/k/old.json"},  # type: ignore[dict-item]
            [path],
            at_most=size - 1,
            known={path: known},
            since=size,
        )[path]
        assert cut is known.older
        added, removed = floor_index._target_moves(known, cut)
        assert added == removed == set()
        walked[size] = visits
    # The same one-revision change walks the same few nodes at any history length.
    assert walked[64] == walked[512]
    assert walked[512][0] <= 8


def test_a_claim_type_change_reaches_only_the_claims_showing_it() -> None:
    from types import SimpleNamespace

    from cruxible_core.derived.persistent import PMap, PSet
    from cruxible_core.service.floor.floor_index import _affected_subjects, _shadow

    def claim(subject: str, predicate: str) -> Any:
        return SimpleNamespace(
            statement=SimpleNamespace(
                subject=SimpleNamespace(artifact_path=subject), predicate=predicate, object=None
            )
        )

    shown = claim("subjects/project.work_item/wi-1.json", "project.work_item.a.b")
    other_kind = claim("subjects/other.kind/x.json", "project.work_item.a.b")
    direct = claim("subjects/other.kind/y.json", "a.b")
    assert _shadow(shown) == "a.b"
    assert _shadow(other_kind) is None and _shadow(direct) is None
    claims = {"claims/1.json": shown, "claims/2.json": other_kind, "claims/3.json": direct}
    inputs: Any = SimpleNamespace(
        claims=claims,
        claim_types={},
        live_by_subject={},
        objects=PMap(),
        by_predicate={
            "project.work_item.a.b": PSet(["claims/1.json", "claims/2.json"]),
            "a.b": PSet(["claims/3.json"]),
        },
        by_shadow={"a.b": PSet(["claims/1.json"])},
        definitions=PMap(),
    )
    # The ClaimType a.b moves its own Claims and the field it shadows on
    # project.work_item, never the same predicate on a Subject of another kind,
    # when its shown view moved between the two generations.
    old = SimpleNamespace(**{**vars(inputs), "definitions": PMap()})
    new = SimpleNamespace(
        **{
            **vars(inputs),
            "definitions": PMap({"claim-types/a/b.json": _shown_chain("claim-types/a/b.json", 4)}),
        }
    )
    # A revision that leaves the view where it was reaches no Subject.
    assert _affected_subjects(new, new, ["claim-types/a/b.json"]) == set()
    assert _affected_subjects(old, new, ["claim-types/a/b.json"]) == {
        "subjects/project.work_item/wi-1.json",
        "subjects/other.kind/y.json",
    }


def test_a_subject_change_reads_the_same_whatever_its_retired_claims() -> None:
    from types import SimpleNamespace

    from cruxible_client.contracts.claims import SubjectClaimObject
    from cruxible_core.derived.persistent import PCounts, PMap, PSet
    from cruxible_core.service.floor.floor_index import _affected_subjects, _Stamps

    path = "subjects/k/one.json"
    measured: dict[int, int] = {}
    for retired in (64, 512):
        work = [0]

        class Counting(dict):  # type: ignore[type-arg]
            def get(self, key: Any, default: Any = None) -> Any:
                work[0] += 1
                return super().get(key, default)

            def __getitem__(self, key: Any) -> Any:
                work[0] += 1
                return super().__getitem__(key)

        def claim(state: str, target: str | None = None) -> Any:
            # A Subject-valued Claim names its target; a literal one names none.
            obj = (
                None
                if target is None
                else SubjectClaimObject.model_construct(
                    address=SimpleNamespace(artifact_path=target)
                )
            )
            return SimpleNamespace(
                lifecycle=SimpleNamespace(state=state),
                statement=SimpleNamespace(
                    subject=SimpleNamespace(artifact_path=path), predicate="k.status", object=obj
                ),
            )

        # Half the retired Claims are edges to former targets, half literals.
        claims = Counting(
            {
                f"claims/retired-{index}.json": claim(
                    "retired", f"subjects/k/former-{index}.json" if index % 2 else None
                )
                for index in range(retired)
            }
        )
        claims["claims/live.json"] = live = claim("live")
        inputs: Any = SimpleNamespace(
            subjects={path: SimpleNamespace(subject_kind="k")},
            claims=claims,
            claim_types={},
            objects=PMap(),
            live_by_subject={path: PSet(["claims/live.json"])},
            by_predicate={},
            by_shadow={},
            latest=Counting({path: 1024, **{key: 1 for key in claims}}),
            # Every lifecycle counts toward the stamps, as maintained maxima.
            claims_latest=PMap({path: PCounts([1] * (retired + 1))}),
            pointed_latest=PMap({path: PCounts([3] * retired)}),
            definitions=PMap(),
        )
        # The shell change reaches no former target of a retired edge.
        assert _affected_subjects(inputs, inputs, [path]) == {path}
        stamps = _Stamps(inputs, PSet())
        assert stamps.subject(path, [live]) == 1024
        assert stamps.incoming(path, []) == 3
        measured[retired] = work[0]
    # Retired Claims render nothing: the same reads at 64 and at 512 of them.
    assert measured[64] == measured[512]


def test_warm_indexes_hold_live_claims_only_and_match_a_cold_build(tmp_path: Any) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    add_lead_field(instance)
    _write(instance, _set(WI1, "lead", WI2), _set(WI1, "status", "ready"))
    advance_floor_index(instance)
    _write(instance, _set(WI1, "lead", WI3))
    _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "status"}})
    _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "lead"}})
    warm = advance_floor_index(instance)
    cold = _cold(instance)
    for name in ("live_by_subject", "live_incoming", "by_predicate", "by_shadow"):
        assert dict(getattr(warm.inputs, name)) == dict(getattr(cold.inputs, name)), name
        for members in getattr(warm.inputs, name).values():
            for claim_file in members:
                assert warm.inputs.claims[claim_file].lifecycle.state == "live", name
    for name in ("claims_latest", "pointed_latest"):
        assert {key: list(value) for key, value in getattr(warm.inputs, name).items()} == {
            key: list(value) for key, value in getattr(cold.inputs, name).items()
        }, name
    # The retired edge shows nowhere, yet both its targets still stamp with it.
    wi1 = f"subjects/{WI1}.json"
    retired = [
        path
        for path, claim in warm.inputs.claims.items()
        if claim.statement.subject.artifact_path == wi1 and claim.lifecycle.state != "live"
    ]
    assert retired and not set(retired) & set(warm.inputs.live_by_subject.get(wi1, ()))
    for target in (f"subjects/{WI2}.json", f"subjects/{WI3}.json"):
        assert target not in warm.inputs.live_incoming
        assert target in warm.inputs.pointed_latest
    assert _bytes(warm) == _bytes(cold)


def test_a_warm_advance_patches_the_manifest_instead_of_rebuilding_it(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """The manifest moves with the files the advance wrote: those paths alone are validated."""

    from cruxible_client.contracts import floor as floor_contracts
    from cruxible_client.contracts.floor_inventory import FloorInventory

    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, *(_set(f"{KIND}/bulk-{index}", "title", f"T{index}") for index in range(40)))
    first = advance_floor_index(instance)
    first.manifest_digest
    validated: list[str] = []
    safe = floor_contracts.safe_floor_path

    def counting(value: str) -> str:
        validated.append(value)
        return safe(value)

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a warm advance rebuilt the whole manifest")

    monkeypatch.setattr(floor_contracts, "safe_floor_path", counting)
    monkeypatch.setattr(FloorInventory, "build", refused)
    _write(instance, _set(f"{KIND}/bulk-3", "status", "ready"))
    warm = advance_floor_index(instance)
    digest = warm.manifest_digest
    monkeypatch.undo()
    step = warm.steps[-1]
    assert sorted(validated) == sorted(step.written)
    assert len(validated) < len(warm.files)
    assert warm.manifest() == _cold(instance).manifest()
    assert digest == _cold(instance).manifest_digest
