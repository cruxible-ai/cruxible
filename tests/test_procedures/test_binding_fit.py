"""Binding by capability and fit (note §2.2-§2.6).

A slot names one capability version. A Line binds each slot to the one live
operation that serves the capability's name with an equal effect and fits it:
what the slot sends fits what the operation accepts, and what the operation
returns fits what the slot expects. Where both sides name ``capture<K>``, the
producer's layout of ``K`` covers the reader's. Run admission rechecks the
recorded binding and never chooses; ``provider move`` and a kit's default
provider follow the same rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.capabilities import Capability, capability_pin
from cruxible_client.contracts.capability_fit import (
    EffectUnequal,
    Fits,
    TruncationUnfit,
    Unfit,
    render_unfit,
)
from cruxible_client.contracts.capture_layouts import DeclaredCaptureKind
from cruxible_client.contracts.captures import CaptureDecoding
from cruxible_client.contracts.procedures.line_specs import (
    BindingSide,
    SlotNeed,
    line_spec_path,
    parse_line_spec,
    slot_fit,
)
from cruxible_client.contracts.provider_operations import closure_lookup
from cruxible_client.contracts.shapes import Contract, contract_digest, validate_contract
from cruxible_core.errors import RequestRefusedError
from tests.support.declared_operations import carried_declares, declared_operation
from tests.support.operation_world import (
    LINE,
    author_line,
    author_procedure,
    build_files,
    operation,
    operation_build,
    slotted_spec,
    world,
)

# -- shapes -------------------------------------------------------------------------------

ROW = validate_contract(
    {"name": "acme.hit", "version": 1, "fields": {"title": "string{max_len: 64}", "rank": "int"}}
)
JSON = CaptureDecoding.model_validate(
    {"format": "json", "limits": {"decoded_bytes": 1024, "depth": 8, "records": 100}}
)
KIND = "acme.hits"


def _pin(contract: Contract) -> dict[str, object]:
    return {
        "name": contract.name,
        "version": contract.version,
        "digest": contract_digest(contract).tagged,
    }


def _kind(*sets: str) -> DeclaredCaptureKind:
    """``acme.hits`` with the named record sets (none: decoding only, no layout)."""

    if not sets:
        return DeclaredCaptureKind(name=KIND, decoding=JSON)
    return DeclaredCaptureKind.model_validate(
        {
            "name": KIND,
            "decoding": JSON.model_dump(mode="json"),
            "layout": {
                "records": {
                    name: {
                        "at": f"/{name}/*",
                        "row": _pin(ROW),
                        "fields": {"title": {"at": "title"}, "rank": {"at": "rank"}},
                    }
                    for name in sets
                }
            },
        }
    )


def _contract(name: str, field: str, kind: str = "*", **extra: Any) -> Contract:
    return validate_contract(
        {
            "name": name,
            "version": 1,
            "fields": {field: {"type": {"kind": "capture", "capture_kind": kind}}, **extra},
        }
    )


def _need(
    in_: Contract, out: Contract, *, effect: str = "pure", may_truncate: bool = False
) -> SlotNeed:
    found = Capability.model_validate(
        {
            "name": "acme.derive",
            "version": 1,
            "description": "Derives hits.",
            "effect": effect,
            "in": _pin(in_),
            "out": _pin(out),
        }
    )
    return SlotNeed(pin=capability_pin(found), capability=found, may_truncate=may_truncate)


def _fit(
    capability_in: Contract,
    capability_out: Contract,
    operation_in: Contract,
    operation_out: Contract,
    *,
    instance_kind: DeclaredCaptureKind | None = None,
    build_kind: DeclaredCaptureKind | None = None,
    effect: str = "pure",
    may_truncate: bool = False,
    need_truncates: bool = False,
) -> Any:
    declared = declared_operation(
        {
            "name": "derive",
            "serves": ["acme.derive"],
            "in": f"{operation_in.name}@1",
            "out": f"{operation_out.name}@1",
            "effect": effect,
            "may_truncate": may_truncate,
            "entrypoint": "acme.derive:Derive",
        }
    )
    contracts = {item.name: item for item in (operation_in, operation_out, ROW)}
    declares = carried_declares(
        (declared,),
        contracts.values(),
        capture_kinds=() if build_kind is None else (build_kind,),
    )
    if build_kind is not None and build_kind.layout is not None:
        # The copy of the kind's layout carries its row Contract too.
        declares = declares.model_copy(
            update={
                "contracts": tuple(
                    sorted(
                        {
                            (item.name, item.version): item for item in (*declares.contracts, ROW)
                        }.values(),
                        key=lambda item: (item.name, item.version),
                    )
                )
            }
        )
    declared = declared.model_copy(
        update={
            "in_": declared.in_.model_copy(update={"digest": contract_digest(operation_in).tagged}),
            "out": declared.out.model_copy(
                update={"digest": contract_digest(operation_out).tagged}
            ),
        }
    )
    side = BindingSide(
        contracts=closure_lookup((capability_in, capability_out, ROW)),
        capture_kinds=lambda name: instance_kind if name == KIND else None,
    )
    return slot_fit(
        _need(capability_in, capability_out, effect=effect, may_truncate=need_truncates),
        declared,
        declares,
        side,
    )


ANY_IN = _contract("acme.any.request", "doc")
KIND_IN = _contract("acme.kind.request", "doc", KIND)
ANY_OUT = _contract("acme.any.result", "doc")
KIND_OUT = _contract("acme.kind.result", "doc", KIND)


# -- §2.2: identical, structural, and the three ways not to fit ---------------------------


def test_equal_contracts_fit_identically() -> None:
    assert _fit(ANY_IN, ANY_OUT, ANY_IN, ANY_OUT) == Fits(fit="identical")


def test_an_operation_accepting_more_fits_structurally() -> None:
    wider = _contract("acme.wide.request", "doc", limit={"optional": True, "type": {"kind": "int"}})
    assert _fit(ANY_IN, ANY_OUT, wider, ANY_OUT) == Fits(fit="structural")


def test_an_operation_accepting_less_is_unfit_at_the_first_path() -> None:
    narrower = _contract("acme.narrow.request", "docs")
    fit = _fit(ANY_IN, ANY_OUT, narrower, ANY_OUT)
    assert isinstance(fit, Unfit)
    assert (fit.side, fit.path) == ("request", "doc")
    assert render_unfit(fit, slot="derive") == (
        "request.doc: the slot may send capture<*>; the operation does not accept it"
    )


def test_an_extra_result_field_is_refused() -> None:
    extra = _contract("acme.extra.result", "doc", note={"type": {"kind": "string"}})
    fit = _fit(ANY_IN, ANY_OUT, ANY_IN, extra)
    assert isinstance(fit, Unfit) and (fit.side, fit.path, fit.problem) == (
        "result",
        "note",
        "undeclared",
    )


def test_effects_must_be_equal_and_truncation_must_be_accepted() -> None:
    assert isinstance(_fit(ANY_IN, ANY_OUT, ANY_IN, ANY_OUT, effect="external_read"), Fits)
    unequal = slot_fit(
        _need(ANY_IN, ANY_OUT, effect="external_read"),
        declared_operation(
            {
                "name": "derive",
                "serves": ["acme.derive"],
                "in": {**_pin(ANY_IN)},
                "out": {**_pin(ANY_OUT)},
                "effect": "pure",
                "entrypoint": "acme.derive:Derive",
            }
        ),
        None,
        BindingSide(contracts=closure_lookup((ANY_IN, ANY_OUT)), capture_kinds=lambda _: None),
    )
    assert unequal == EffectUnequal(capability="external_read", operation="pure")
    assert _fit(ANY_IN, ANY_OUT, ANY_IN, ANY_OUT, may_truncate=True) == TruncationUnfit()
    assert isinstance(
        _fit(ANY_IN, ANY_OUT, ANY_IN, ANY_OUT, may_truncate=True, need_truncates=True), Fits
    )


# -- §2.3: every row of the Capture table -------------------------------------------------


def test_row_1_sends_a_kind_operation_accepts_any() -> None:
    assert isinstance(_fit(KIND_IN, ANY_OUT, ANY_IN, ANY_OUT, instance_kind=_kind("hits")), Fits)


def test_row_2_sends_a_kind_operation_accepts_the_kind_when_the_instance_layout_covers() -> None:
    covered = _fit(
        KIND_IN,
        ANY_OUT,
        KIND_IN,
        ANY_OUT,
        instance_kind=_kind("hits", "misses"),
        build_kind=_kind("hits"),
    )
    assert isinstance(covered, Fits)
    gap = _fit(
        KIND_IN, ANY_OUT, KIND_IN, ANY_OUT, instance_kind=_kind("hits"), build_kind=_kind("misses")
    )
    assert isinstance(gap, Unfit) and (gap.side, gap.problem) == ("request", "layout")
    assert gap.path == "doc.layout.records.misses"


def test_row_3_sends_any_operation_accepts_a_kind() -> None:
    fit = _fit(ANY_IN, ANY_OUT, KIND_IN, ANY_OUT, build_kind=_kind("hits"))
    assert isinstance(fit, Unfit) and (fit.side, fit.path, fit.problem) == (
        "request",
        "doc",
        "type",
    )


def test_row_4_expects_any_operation_returns_any_or_a_kind() -> None:
    assert isinstance(_fit(ANY_IN, ANY_OUT, ANY_IN, ANY_OUT), Fits)
    assert isinstance(_fit(ANY_IN, ANY_OUT, ANY_IN, KIND_OUT, build_kind=_kind("hits")), Fits)


def test_row_5_expects_a_kind_operation_returns_it_when_its_copy_covers() -> None:
    assert isinstance(
        _fit(
            ANY_IN,
            KIND_OUT,
            ANY_IN,
            KIND_OUT,
            instance_kind=_kind("hits"),
            build_kind=_kind("hits", "misses"),
        ),
        Fits,
    )
    # The copy promises no layout at all: the present-but-empty case is refused here.
    thin = _fit(ANY_IN, KIND_OUT, ANY_IN, KIND_OUT, instance_kind=_kind("hits"), build_kind=_kind())
    assert isinstance(thin, Unfit) and (thin.side, thin.problem) == ("result", "layout")
    assert "record sets hits" in render_unfit(thin, slot="derive")
    missing = _fit(
        ANY_IN, KIND_OUT, ANY_IN, KIND_OUT, instance_kind=_kind("hits"), build_kind=_kind("misses")
    )
    assert isinstance(missing, Unfit) and missing.path == "doc.layout.records.hits"


def test_row_6_expects_a_kind_operation_returns_any() -> None:
    fit = _fit(ANY_IN, KIND_OUT, ANY_IN, ANY_OUT, instance_kind=_kind("hits"))
    assert isinstance(fit, Unfit)
    assert render_unfit(fit, slot="derive") == (
        f"result.doc: the operation returns capture<*>; the capability expects capture<{KIND}>"
    )


def test_equal_kind_declarations_cover_and_keep_the_fit_identical() -> None:
    assert _fit(
        ANY_IN, KIND_OUT, ANY_IN, KIND_OUT, instance_kind=_kind("hits"), build_kind=_kind("hits")
    ) == Fits(fit="identical")


# -- Line acceptance: the gate --------------------------------------------------------------

WIDE = Contract.model_validate(
    {
        "name": "test.wide",
        "version": 1,
        "fields": {"value": {"type": "int"}, "note": {"type": "string", "optional": True}},
    }
)
NARROW = Contract.model_validate(
    {"name": "test.narrow", "version": 1, "fields": {"count": {"type": "int"}}}
)


def _build(provider_id: str, *, signature: tuple[str, str], serves: tuple[str, ...]) -> Any:
    return operation_build(
        provider_id=provider_id,
        operations=(operation(signature=signature, serves=serves),),
        contracts=(WIDE, NARROW),
    )


EXACT = _build(
    "exact-provider", signature=("test.value@1", "test.value@1"), serves=("test.compute",)
)
WIDER = _build("wide-provider", signature=("test.wide@1", "test.value@1"), serves=("test.compute",))
NARROWER = _build(
    "narrow-provider", signature=("test.narrow@1", "test.value@1"), serves=("test.compute",)
)
OTHER = _build("other-provider", signature=("test.value@1", "test.value@1"), serves=("test.other",))


def _world(tmp_path: Path, *builds: Any) -> Any:
    instance = world(tmp_path, *builds)
    author_procedure(instance, slotted_spec())
    return instance


def _bound(instance: Any) -> Any:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(LINE)
    return parse_line_spec(tree[path], path=path).bindings["compute"]


def _refused(instance: Any, bindings: Mapping[str, str] | None = None) -> RequestRefusedError:
    before = instance.accepted_coordinate()
    with pytest.raises(RequestRefusedError) as refused:
        author_line(instance, bindings=bindings)
    assert instance.accepted_coordinate() == before
    return refused.value


def test_an_exactly_fitting_operation_binds(tmp_path: Path) -> None:
    instance = _world(tmp_path, EXACT, NARROWER, OTHER)
    author_line(instance)
    assert _bound(instance).build.target == EXACT.identity


def test_an_operation_accepting_more_binds_by_name(tmp_path: Path) -> None:
    instance = _world(tmp_path, WIDER)
    author_line(instance, bindings={"compute": "wide-provider"})
    assert _bound(instance).build.target == WIDER.identity


def test_an_operation_accepting_less_is_refused_unfit(tmp_path: Path) -> None:
    instance = _world(tmp_path, NARROWER)
    refusal = _refused(instance, {"compute": "narrow-provider"})
    assert refusal.error_code == "cruxible.line.binding_unfit"
    assert str(refusal).endswith(
        "Slot `compute` needs test.compute@1. `narrow-provider#compute` declares it but does "
        "not fit: request.count: the operation requires int; the slot never sends it."
    )
    # Unnamed, it is listed among those that declare the capability but do not fit.
    unfilled = _refused(instance)
    assert unfilled.error_code == "cruxible.line.slot_unfilled"
    assert "`narrow-provider#compute`: request.count" in str(unfilled)


def test_an_operation_serving_another_capability_is_refused_undeclared(tmp_path: Path) -> None:
    instance = _world(tmp_path, OTHER)
    refusal = _refused(instance, {"compute": "other-provider"})
    assert refusal.error_code == "cruxible.line.binding_undeclared"
    assert str(refusal).endswith(
        "Slot `compute` needs test.compute@1. `other-provider#compute` serves test.other, "
        "not test.compute."
    )


def test_two_fitting_operations_are_ambiguous(tmp_path: Path) -> None:
    instance = _world(tmp_path, EXACT, WIDER)
    refusal = _refused(instance)
    assert refusal.error_code == "cruxible.line.slot_ambiguous"
    assert f"`{EXACT.identity.name}#compute`" in str(refusal)
    assert f"`{WIDER.identity.name}#compute`" in str(refusal)


def test_a_builtin_slot_refuses_any_other_build_before_fit(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.line_specs import LINE_BINDING_BUILTIN_MISMATCH

    instance = _world(tmp_path, EXACT)
    refusal = _refused(instance, {"compute": "cruxible-builtin"})
    assert refusal.error_code == LINE_BINDING_BUILTIN_MISMATCH
    assert "test.compute@1" in str(refusal) and "core's builtin" in str(refusal)


def test_the_line_law_refuses_an_unfit_binding_accepted_directly(tmp_path: Path) -> None:
    """A Line accepted without authoring (a kit's, a hand edit) meets the same gate."""

    from cruxible_client.contracts.procedures.line_specs import render_line_spec
    from cruxible_client.contracts.provider_operations import binding_pin
    from cruxible_client.contracts.providers import provider_digest
    from tests.core_support._identity_support import accept_tree

    instance = _world(tmp_path, EXACT, NARROWER)
    author_line(instance)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(LINE)
    line = parse_line_spec(tree[path], path=path)
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest

    moved = line.model_copy(
        update={
            "bindings": {
                "compute": line.bindings["compute"].model_copy(
                    update={
                        "build": binding_pin(NARROWER.identity, provider_digest(NARROWER).tagged)
                    }
                )
            },
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
        }
    )
    with pytest.raises(AssertionError, match="cruxible.line.binding_unfit"):
        accept_tree(instance, {path: render_line_spec(moved)}, name="unfit-line")


# -- run admission: the recorded binding is rechecked, never refitted -----------------------


def test_admission_rechecks_the_recorded_binding_and_records_its_fit(tmp_path: Path) -> None:
    from cruxible_core.procedures.reference_binding import bind_procedure_references
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure

    instance = _world(tmp_path, WIDER)
    author_line(instance, bindings={"compute": "wide-provider"})
    binding = _bound(instance)
    coordinate = instance.accepted_coordinate()
    bound = _accepted_procedure(
        instance,
        name="test.slotted",
        coordinate=coordinate,
        bind_references=True,
        bindings={"compute": binding},
    )
    (slot,) = bound.procedure.slot_bindings
    assert (slot.capability.spelling, slot.fit) == ("test.compute@1", "structural")

    # The same binding offered for a slot it does not fit is refused, not refitted.
    narrow = operation_build(
        provider_id="narrow-provider",
        operations=(
            operation(signature=("test.narrow@1", "test.value@1"), serves=("test.compute",)),
        ),
        contracts=(NARROW,),
    )
    from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
    from cruxible_client.contracts.providers import provider_digest
    from tests.core_support._identity_support import accept_tree

    accept_tree(instance, build_files(narrow), name="narrow")
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        accepted = _accepted_procedure(instance, name="test.slotted", coordinate=coordinate)

        def lookup(identity: Any) -> Any:
            row = projection.typed.envelope(identity.qualified)
            source = projection.typed.source(identity.qualified)
            return None if row is None or source is None else (source, row.artifact_digest)

        with pytest.raises(RequestRefusedError) as refused:
            bind_procedure_references(
                accepted,
                lookup=lookup,
                bindings={
                    "compute": LineBinding(
                        build=binding_pin(narrow.identity, provider_digest(narrow).tagged),
                        operation="compute",
                    )
                },
            )
    assert refused.value.error_code == "cruxible.line.binding_unfit"


def test_the_admission_recheck_is_memoized_per_process(tmp_path: Path) -> None:
    from cruxible_core.providers.operation_index import BINDING_WORK
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure

    instance = _world(tmp_path, WIDER)
    author_line(instance, bindings={"compute": "wide-provider"})
    binding = _bound(instance)
    coordinate = instance.accepted_coordinate()

    def admit() -> None:
        _accepted_procedure(
            instance,
            name="test.slotted",
            coordinate=coordinate,
            bind_references=True,
            bindings={"compute": binding},
        )

    admit()
    fits = BINDING_WORK["fits"]
    admit()
    admit()
    assert BINDING_WORK["fits"] == fits


# -- provider move and a kit's default provider --------------------------------------------


def test_a_move_rebinds_to_the_target_operation_that_fits(tmp_path: Path) -> None:
    from collections import ChainMap

    from cruxible_core.providers.build_moves import (
        ProviderBuildMoveRefused,
        _rebound_line,
        build_move,
    )

    instance = _world(tmp_path, EXACT)
    author_line(instance)
    renamed = operation_build(
        provider_id="exact-provider",
        build="b",
        operations=(
            operation("apply", signature=("test.wide@1", "test.value@1"), serves=("test.compute",)),
            operation("other", signature=("test.value@1", "test.value@1"), serves=("test.other",)),
        ),
        contracts=(WIDE,),
    )
    narrow = operation_build(
        provider_id="exact-provider",
        build="c",
        operations=(
            operation(
                "compute", signature=("test.narrow@1", "test.value@1"), serves=("test.compute",)
            ),
        ),
        contracts=(NARROW,),
    )
    from tests.core_support._identity_support import accept_tree

    accept_tree(instance, {**build_files(renamed), **build_files(narrow)}, name="builds")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(LINE)

    # No operation of the same name: the one that serves the capability and fits.
    move = build_move(tree, EXACT.identity.name, renamed.identity.name)
    moved = _rebound_line(tree, {}, path, {EXACT.identity.qualified: move})
    assert moved is not None
    assert parse_line_spec(moved, path=path).bindings["compute"].operation == "apply"

    # The operation of the same name does not fit: refused with its reason.
    move = build_move(tree, EXACT.identity.name, narrow.identity.name)
    with pytest.raises(ProviderBuildMoveRefused, match="request.count") as refused:
        _rebound_line(ChainMap(tree), {}, path, {EXACT.identity.qualified: move})
    assert refused.value.reason == "contract_changed"


def test_a_default_provider_is_skipped_only_when_another_operation_fits(tmp_path: Path) -> None:
    from cruxible_client.contracts.kits import KitProvider
    from cruxible_client.contracts.procedures.artifacts import procedure_path
    from cruxible_core.service.kits import _default_implementers

    def default() -> KitProvider:
        # Only these fields decide the skip; the wheel and lock it would verify do not.
        return KitProvider.model_construct(
            provider_id="kit-default", delivery="index", operations=("compute",)
        )

    for builds, expected in (
        ((NARROWER, OTHER), ()),
        ((NARROWER, WIDER), (WIDER.identity.name,)),
    ):
        root = tmp_path / str(len(expected))
        root.mkdir()
        instance = _world(root, *builds)
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        contents = {procedure_path("test.slotted"): tree[procedure_path("test.slotted")]}
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            found = _default_implementers(projection.typed.connection, tree, default(), contents)
        assert found == expected
