"""Subject-valued key parts (pair Subjects), slice S1: the identity core and its law.

A SubjectKind key part may name a Subject of one other kind; the kind is then a
pair kind, keyed by its ends' paths. Literal keys keep every byte they had. The
SubjectKind law accepts a pair kind only when it is unique, its parts are
one-valued, take normalizer ``none``, name one declared kind each and the kinds
form a shallow DAG. The candidate boundary freezes a pair's endpoints and its
kind's identity for life, and keeps a live pair's ends live -- on every route,
because every route is judged there (raw proposals here).
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.identity_normalizers import LowercaseNormalizer
from cruxible_client.contracts.subject_kinds import (
    MAX_KEY_DEPTH,
    KeyClaimTypeShape,
    SubjectEnd,
    SubjectKeyRule,
    SubjectKind,
    SubjectKindKeys,
    evaluate_subject_kind_law,
    key_digest_hex,
    render_key_slug,
    render_subject_kind,
    subject_kind_digest,
    subject_kind_path,
)
from cruxible_core.indexes.subject_identity import (
    PendingEnd,
    key_encoding,
    key_order,
    single_key,
)
from tests.core_support._pair_support import (
    COMPONENT,
    E_COMPONENT,
    E_COMPONENT2,
    E_VULN,
    EXPOSURE,
    KINDS,
    O_OWNER,
    O_SERIAL,
    OWNED,
    PURL,
    SCOPE,
    TYPES,
    VULN,
    claim,
    definitions,
    end,
    exposure,
    judge,
    keyed,
    literal,
    path_of,
    put,
    retired_claim,
    retired_shell,
    shell,
    successor_kind,
    to,
)
from tests.support.claim_types import claim_type

C1 = path_of(COMPONENT, "c1")
C2 = path_of(COMPONENT, "c2")
V1 = path_of(VULN, "v1")
E11 = path_of(EXPOSURE, "c1--v1")

# -- encoding stability ------------------------------------------------------------

#: Literal keys, as the build before Subject-valued parts encoded, rendered and
#: digested them (checked against that build's code when this slice landed).
LITERAL_KEYS = (
    (
        (("dev.decision.slug", "Ship It"),),
        '[["dev.decision.slug","Ship It"]]',
        "ship-it",
        "af3a816c2318e9536961c061692e4b7cf92da394878576af7257d759697bb3c1",
    ),
    (
        (("inv.package.code", 17),),
        '[["inv.package.code",17]]',
        "x17",
        "fe3b7b3db0778380f48e04b7f215ea1ac524bf0a846bcf78f8e3da2727707f1a",
    ),
    (
        (("inv.package.flag", True),),
        '[["inv.package.flag",true]]',
        "true",
        "1c28f2eb0958c3d15db1f0f0e7f2b8998ca2b8f67ab426a1fbb3d561fe76fad9",
    ),
    (
        (("dev.pair.left", "L"), ("dev.pair.right", "R")),
        '[["dev.pair.left","L"],["dev.pair.right","R"]]',
        "l--r",
        "cbfd3703098febc53ba38595609a8dc923fc7fdcffafd18030aa32db285b0a87",
    ),
)


@pytest.mark.parametrize(("key", "encoding", "slug", "digest"), LITERAL_KEYS)
def test_literal_keys_keep_every_byte(key: Any, encoding: str, slug: str, digest: str) -> None:
    values = [value for _predicate, value in key]
    assert key_encoding(key) == encoding
    assert render_key_slug(values) == slug
    assert key_digest_hex(values) == digest
    assert key_order(key) == ("[" + ",".join(encoding_value(v) for v in values) + "]").encode()


def encoding_value(value: object) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def test_a_literal_key_claim_keeps_its_identity() -> None:
    from cruxible_core.authoring.lowering import subject_key_claim_id

    assert (
        subject_key_claim_id(
            operation="AIT-" + "1" * 32, member="m@1", predicate="dev.decision.slug"
        )
        == "CLM-3ed2ad2672e317b88cd663655cf2c884"
    )


def test_a_subject_part_encodes_as_its_end_path_and_renders_as_its_id() -> None:
    component = SubjectEnd(path="subjects/pt.component/c1.json")
    vuln = SubjectEnd(path="subjects/pt.vuln/cve-2024-1.json")
    key = ((E_COMPONENT, component), (E_VULN, vuln))
    assert key_encoding(key) == (
        '[["pt.exposure.component",{"subject":"subjects/pt.component/c1.json"}],'
        '["pt.exposure.vulnerability",{"subject":"subjects/pt.vuln/cve-2024-1.json"}]]'
    )
    assert render_key_slug([component, vuln]) == "c1--cve-2024-1"
    assert (
        key_digest_hex([component, vuln])
        == "4a36dbee70936410e1bc78aa3ff2441d66c35124f9b9eeff46de59d31e5b8a43"
    )
    # A JSON object never equals a literal: the end's path as text is another key.
    assert key_encoding(((E_COMPONENT, component.path),)) != key_encoding(
        ((E_COMPONENT, component),)
    )
    # A mixed key renders each part its own way.
    assert render_key_slug([component, "B"]) == "c1--b"
    # A pending end (same-change creation) is its member's token, never a path.
    assert key_encoding(((E_COMPONENT, PendingEnd("m@1")),)) == (
        '[["pt.exposure.component",{"pending":"m@1"}]]'
    )
    with pytest.raises(ValueError):
        SubjectEnd(path="claims/00/CLM-0.json")


# -- the SubjectKind law ------------------------------------------------------------


def _shape(predicate: str) -> KeyClaimTypeShape:
    from cruxible_core.indexes.subject_identity import key_claim_type_shape

    shape = key_claim_type_shape(definitions(), predicate)
    assert shape is not None
    return shape


def _law(kind: SubjectKind, shapes: dict[str, KeyClaimTypeShape], kinds: dict[str, SubjectKind]):
    return evaluate_subject_kind_law(
        kind,
        path=subject_kind_path(kind.subject_kind),
        predecessor=None,
        key_claim_types={predicate: shapes.get(predicate) for predicate in kind.keys},
        subject_kind_of=kinds.get,
        key_shape_of=shapes.get,
    )


def _codes(result: Any) -> list[str]:
    return [item.code for item in result.diagnostics]


SHAPES = {predicate: _shape(predicate) for predicate in TYPES}


def test_the_law_accepts_a_pair_kind_of_declared_ends() -> None:
    result = _law(KINDS[EXPOSURE], SHAPES, KINDS)
    assert result.verdict == "accepted", _codes(result)
    assert SHAPES[E_COMPONENT].object_subject_kinds == (COMPONENT,)
    mixed = _law(KINDS[OWNED], SHAPES, KINDS)
    assert mixed.verdict == "accepted", _codes(mixed)


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"object_subject_kinds": (COMPONENT, VULN)}, "not a union"),
        ({"object_subject_kinds": (EXPOSURE,)}, "itself"),
        ({"cardinality": "many"}, "one-valued"),
        ({"allowed_subject_kinds": (EXPOSURE, COMPONENT)}, "about pt.exposure only"),
    ],
    ids=["union", "self", "many-valued", "shared"],
)
def test_a_subject_part_names_one_other_kind_one_valued(change: dict, fragment: str) -> None:
    shapes = {**SHAPES, E_COMPONENT: SHAPES[E_COMPONENT].model_copy(update=change)}
    result = _law(KINDS[EXPOSURE], shapes, KINDS)
    assert _codes(result) == ["cruxible.subject_kind.key_claim_type_invalid"]
    assert fragment in result.diagnostics[0].message


def test_a_subject_part_takes_normalizer_none() -> None:
    lowered = SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=EXPOSURE),
        identity_rule=SubjectKindKeys(
            parts=(
                SubjectKeyRule(claim_type=E_COMPONENT, normalizer=LowercaseNormalizer(version=1)),
                *KINDS[EXPOSURE].identity_rule.parts[1:],  # type: ignore[union-attr]
            ),
            uniqueness="unique",
        ),
    )
    result = _law(lowered, SHAPES, KINDS)
    assert _codes(result) == ["cruxible.subject_kind.key_claim_type_invalid"]
    assert "normalizer none" in result.diagnostics[0].message


def test_a_pair_kind_is_unique() -> None:
    hint = keyed(EXPOSURE, E_COMPONENT, E_VULN, uniqueness="hint")
    result = _law(hint, SHAPES, KINDS)
    assert _codes(result) == ["cruxible.subject_kind.key_claim_type_invalid"]
    assert "unique" in result.diagnostics[0].message


def test_a_pair_kind_names_declared_ends() -> None:
    result = _law(KINDS[EXPOSURE], SHAPES, {k: v for k, v in KINDS.items() if k != VULN})
    assert _codes(result) == ["cruxible.subject_kind.key_claim_type_invalid"]
    assert "pt.vuln" in result.diagnostics[0].message
    # Without readers of the candidate's other kinds, no end is known declared.
    blind = evaluate_subject_kind_law(
        KINDS[EXPOSURE],
        path=subject_kind_path(EXPOSURE),
        predecessor=None,
        key_claim_types={predicate: SHAPES[predicate] for predicate in KINDS[EXPOSURE].keys},
    )
    assert blind.verdict == "refused"


def _chain(depth: int) -> tuple[dict[str, SubjectKind], dict[str, KeyClaimTypeShape]]:
    """Kinds k0 (plain) <- k1 <- ... <- k{depth}, each keyed by the one below."""

    kinds = {"pt.k0": keyed("pt.k0", "pt.k0.slug")}
    shapes = {"pt.k0.slug": _shape(PURL).model_copy(update={"predicate": "pt.k0.slug"})}
    shapes["pt.k0.slug"] = shapes["pt.k0.slug"].model_copy(
        update={"allowed_subject_kinds": ("pt.k0",)}
    )
    for level in range(1, depth + 1):
        kind, predicate = f"pt.k{level}", f"pt.k{level}.down"
        kinds[kind] = keyed(kind, predicate)
        shapes[predicate] = SHAPES[E_COMPONENT].model_copy(
            update={
                "predicate": predicate,
                "allowed_subject_kinds": (kind,),
                "object_subject_kinds": (f"pt.k{level - 1}",),
            }
        )
    return kinds, shapes


def test_pair_kinds_nest_at_most_max_depth() -> None:
    kinds, shapes = _chain(MAX_KEY_DEPTH)
    deepest = _law(kinds[f"pt.k{MAX_KEY_DEPTH}"], shapes, kinds)
    assert deepest.verdict == "accepted", _codes(deepest)
    kinds, shapes = _chain(MAX_KEY_DEPTH + 1)
    too_deep = _law(kinds[f"pt.k{MAX_KEY_DEPTH + 1}"], shapes, kinds)
    assert _codes(too_deep) == ["cruxible.subject_kind.key_claim_type_invalid"]
    assert "deep" in too_deep.diagnostics[0].message


def test_a_cycle_of_pair_kinds_refuses() -> None:
    kinds, shapes = _chain(2)
    # k0 succeeded to be keyed by k2: k2 -> k1 -> k0 -> k2.
    kinds["pt.k0"] = keyed("pt.k0", "pt.k0.up")
    shapes["pt.k0.up"] = SHAPES[E_COMPONENT].model_copy(
        update={
            "predicate": "pt.k0.up",
            "allowed_subject_kinds": ("pt.k0",),
            "object_subject_kinds": ("pt.k2",),
        }
    )
    result = _law(kinds["pt.k0"], shapes, kinds)
    assert _codes(result) == ["cruxible.subject_kind.key_cycle"]
    assert "pt.k0 -> pt.k2 -> pt.k1 -> pt.k0" in result.diagnostics[0].message


def test_a_one_key_pair_kind_has_no_single_key_reading() -> None:
    tree = definitions()
    solo = keyed("pt.solo", "pt.solo.end")
    tree[subject_kind_path("pt.solo")] = render_subject_kind(solo)
    end_type = claim_type("pt.solo.end", value_type=(COMPONENT,))
    tree[claim_type_path("pt.solo.end")] = render_claim_type(end_type)
    assert single_key(tree, "pt.solo") is None
    assert single_key(tree, COMPONENT) is not None


# -- the candidate boundary -----------------------------------------------------------


def _world() -> dict[str, bytes]:
    """Ends c1, c2 and v1, and the pair c1--v1 (Claims 1-6), as a parent tree."""

    tree = definitions()
    put(tree, *end(COMPONENT, "c1", 1), *end(COMPONENT, "c2", 2), *end(VULN, "v1", 3))
    put(tree, *exposure("c1", "v1", 5))
    return tree


def test_a_raw_pair_creation_passes_and_its_key_is_unique() -> None:
    parent = definitions()
    put(parent, *end(COMPONENT, "c1", 1), *end(VULN, "v1", 3))
    created = put({}, *exposure("c1", "v1", 5))
    assert judge(parent, created) == []
    # The same ends under another id: the key is taken.
    parent.update(created)
    twin = path_of(EXPOSURE, "c1--v1-0000aaaa")
    again = put(
        {},
        shell(EXPOSURE, "c1--v1-0000aaaa"),
        claim(20, E_COMPONENT, to(C1), subject=twin),
        claim(21, E_VULN, to(V1), subject=twin),
    )
    assert "cruxible.subject.key_conflict" in judge(parent, again)


def test_a_pair_created_to_a_retired_end_refuses() -> None:
    parent = definitions()
    component, key = end(COMPONENT, "c1", 1)
    put(parent, component, retired_claim(key), *end(VULN, "v1", 3))
    put(parent, retired_shell(component))
    assert judge(parent, put({}, *exposure("c1", "v1", 5))) == [
        "cruxible.subject.key_endpoint_retired"
    ]


def test_an_endpoint_is_never_revised_or_added() -> None:
    parent = _world()
    _pair, component, _vuln = exposure("c1", "v1", 5)
    # Revise: the old endpoint retired, a new one to c2 added, the pair kept.
    moved = put({}, retired_claim(component), claim(30, E_COMPONENT, to(C2), subject=E11))
    assert set(judge(parent, moved)) == {"cruxible.subject.key_endpoint_fixed"}
    # The same Claim identity restated with another end.
    restated = put({}, claim(5, E_COMPONENT, to(C2), subject=E11))
    assert judge(parent, restated) == ["cruxible.subject.key_endpoint_fixed"]
    # An endpoint added to an existing pair, no existing one touched.
    added = put({}, claim(31, E_COMPONENT, to(C2), subject=E11))
    assert "cruxible.subject.key_endpoint_fixed" in judge(parent, added)
    # Literal key parts keep their re-key behaviour: c1's purl is revised freely.
    _shell, purl = end(COMPONENT, "c1", 1)
    rekeyed = put({}, retired_claim(purl), claim(32, PURL, literal("c1b"), subject=C1))
    assert judge(parent, rekeyed) == []


def test_retiring_a_pair_never_moves_its_endpoints() -> None:
    parent = _world()
    pair, component, vuln = exposure("c1", "v1", 5)
    # F10: retiring E(c1, v1) while revising its component to c2 refuses.
    revised = put(
        {},
        retired_shell(pair),
        retired_claim(component),
        retired_claim(vuln),
        claim(30, E_COMPONENT, to(C2), subject=E11),
    )
    assert "cruxible.subject.key_endpoint_fixed" in judge(parent, revised)
    # A lifecycle-only retirement of the pair with its endpoints is fine.
    retired = put({}, retired_shell(pair), retired_claim(component), retired_claim(vuln))
    assert judge(parent, retired) == []
    # An endpoint retired while its pair stays live refuses.
    assert judge(parent, put({}, retired_claim(component))) == [
        "cruxible.subject.key_endpoint_fixed"
    ]


def test_an_end_of_a_live_pair_is_retired_only_with_the_pair() -> None:
    parent = _world()
    component, purl = end(COMPONENT, "c1", 1)
    alone = put({}, retired_shell(component), retired_claim(purl))
    issues = judge(parent, alone)
    assert issues == ["cruxible.subject.key_endpoint_retired"]
    pair, e_component, e_vuln = exposure("c1", "v1", 5)
    together = put(
        {},
        retired_shell(component),
        retired_claim(purl),
        retired_shell(pair),
        retired_claim(e_component),
        retired_claim(e_vuln),
    )
    assert judge(parent, together) == []
    # c2 is the end of no pair: it retires freely.
    other, other_key = end(COMPONENT, "c2", 2)
    assert judge(parent, put({}, retired_shell(other), retired_claim(other_key))) == []


def test_a_minted_end_retired_alone_still_refuses() -> None:
    """No Claim moves when a minted end retires; the end check runs all the same."""

    from tests.core_support._pair_support import HOST, R_HOST, RUN

    parent = definitions()
    host = shell(HOST, "sub-" + "a" * 32)
    host_path = path_of(HOST, host.subject_id)
    run = path_of(RUN, host.subject_id)
    put(parent, host, shell(RUN, host.subject_id), claim(90, R_HOST, to(host_path), subject=run))
    assert judge(parent, put({}, retired_shell(host))) == ["cruxible.subject.key_endpoint_retired"]


def test_the_refusal_names_the_live_pairs_to_retire() -> None:
    from cruxible_core.indexes.subject_identity import candidate_identity_issues

    parent = _world()
    component, purl = end(COMPONENT, "c1", 1)
    changes = put({}, retired_shell(component), retired_claim(purl))
    (issue,) = candidate_identity_issues(
        current_tree=parent, candidate_tree={**parent, **changes}, scope=tuple(sorted(changes))
    )
    assert issue.existing == ("pt.exposure/c1--v1",)
    assert "retire them with their Claims" in issue.message


# -- the pair kind's identity is frozen (R13, F1, F12) --------------------------------


def test_a_successor_cannot_replace_a_pair_part() -> None:
    parent = _world()
    replaced = successor_kind(
        KINDS[EXPOSURE], identity_rule=keyed(EXPOSURE, E_COMPONENT2, E_VULN).identity_rule
    )
    # (a) component2 = c2 laid on the existing E(c1, v1), its c1 endpoint untouched.
    changes = {subject_kind_path(EXPOSURE): render_subject_kind(replaced)}
    put(changes, claim(40, E_COMPONENT2, to(C2), subject=E11))
    assert "cruxible.subject_kind.endpoint_declaration_fixed" in judge(parent, changes)


def test_a_successor_cannot_reorder_or_relax_a_pair_kind() -> None:
    parent = _world()
    reordered = successor_kind(
        KINDS[EXPOSURE], identity_rule=keyed(EXPOSURE, E_VULN, E_COMPONENT).identity_rule
    )
    assert judge(parent, {subject_kind_path(EXPOSURE): render_subject_kind(reordered)}) == [
        "cruxible.subject_kind.endpoint_declaration_fixed"
    ]
    # (c) a retiring successor keeps the rule too; only the lifecycle may change.
    retiring = successor_kind(
        KINDS[EXPOSURE],
        identity_rule=keyed(EXPOSURE, E_COMPONENT).identity_rule,
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=subject_kind_digest(KINDS[EXPOSURE]).tagged,
        ),
    )
    assert judge(parent, {subject_kind_path(EXPOSURE): render_subject_kind(retiring)}) == [
        "cruxible.subject_kind.endpoint_declaration_fixed"
    ]
    plain_retire = successor_kind(
        KINDS[EXPOSURE],
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=subject_kind_digest(KINDS[EXPOSURE]).tagged,
        ),
    )
    assert judge(parent, {subject_kind_path(EXPOSURE): render_subject_kind(plain_retire)}) == []


def _type_successor(predicate: str, **changes: Any) -> bytes:
    previous = TYPES[predicate]
    return render_claim_type(
        previous.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=claim_type_digest(previous).tagged
                ),
                **changes,
            }
        )
    )


def test_an_endpoint_claim_type_keeps_its_end_kind_even_beside_a_kind_successor() -> None:
    from cruxible_client.contracts.shapes import coerce_field_type

    parent = _world()
    other_end = coerce_field_type({"kind": "subject", "subject_kinds": (VULN,)})
    retyped = _type_successor(E_COMPONENT, value=other_end)
    # (d) beside a SubjectKind successor (whose own rule it keeps byte-equal).
    same_rule = successor_kind(KINDS[EXPOSURE])
    changes = {
        claim_type_path(E_COMPONENT): retyped,
        subject_kind_path(EXPOSURE): render_subject_kind(same_rule),
    }
    assert "cruxible.subject_kind.key_claim_type_broken" in judge(parent, changes)
    assert "cruxible.subject_kind.key_claim_type_broken" in judge(
        parent, {claim_type_path(E_COMPONENT): retyped}
    )
    # Retired while its kind exists.
    previous = TYPES[E_COMPONENT]
    retired = render_claim_type(
        previous.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired", predecessor_digest=claim_type_digest(previous).tagged
                )
            }
        )
    )
    assert "cruxible.subject_kind.key_claim_type_broken" in judge(
        parent, {claim_type_path(E_COMPONENT): retired}
    )
    # A literal key ClaimType of a live kind made Subject-valued, kind untouched:
    # pt.component holds Subjects, so it can never become a pair kind.
    to_subject = _type_successor(
        PURL, value=coerce_field_type({"kind": "subject", "subject_kinds": (VULN,)})
    )
    assert judge(parent, {claim_type_path(PURL): to_subject}) == [
        "cruxible.subject_kind.endpoint_declaration_fixed"
    ]
    # With no Subject yet it still needs a SubjectKind successor beside it.
    bare = definitions()
    assert judge(bare, {claim_type_path(PURL): to_subject}) == [
        "cruxible.subject_kind.key_claim_type_broken"
    ]


def test_a_grandfathered_pair_keeps_its_endpoints_frozen() -> None:
    parent = _world()
    parent[subject_kind_path(EXPOSURE)] = render_subject_kind(
        successor_kind(
            KINDS[EXPOSURE],
            lifecycle=ArtifactLifecycle(
                state="retired", predecessor_digest=subject_kind_digest(KINDS[EXPOSURE]).tagged
            ),
        )
    )
    _pair, component, _vuln = exposure("c1", "v1", 5)
    # (e) a key Claim revision on a pair of a retired (grandfathered) kind.
    moved = put({}, retired_claim(component), claim(30, E_COMPONENT, to(C2), subject=E11))
    assert "cruxible.subject.key_endpoint_fixed" in judge(parent, moved)


def test_a_kind_becomes_a_pair_kind_only_while_it_has_no_subject() -> None:
    # (f) a literal-only K(serial) Subject created and retired, then a successor
    # of K adding owner: subject<pt.component>.
    literal_owned = keyed(OWNED, O_SERIAL)
    parent = definitions(COMPONENT, VULN)
    parent[subject_kind_path(OWNED)] = render_subject_kind(literal_owned)
    put(parent, *end(COMPONENT, "c1", 1))
    held = shell(OWNED, "s1")
    serial = claim(50, O_SERIAL, literal("s1"), subject=path_of(OWNED, "s1"))
    put(parent, retired_shell(held), retired_claim(serial))
    paired = successor_kind(literal_owned, identity_rule=KINDS[OWNED].identity_rule)
    change = {subject_kind_path(OWNED): render_subject_kind(paired)}
    assert judge(parent, change) == ["cruxible.subject_kind.endpoint_declaration_fixed"]
    # A fresh kind with no Subject may add it.
    fresh = definitions(COMPONENT, VULN)
    fresh[subject_kind_path(OWNED)] = render_subject_kind(literal_owned)
    assert judge(fresh, change) == []
    # And a new SubjectKind declaring a pair kind over grandfathered Subjects refuses.
    undeclared = definitions(COMPONENT, VULN)
    put(undeclared, held)
    assert judge(undeclared, {subject_kind_path(OWNED): render_subject_kind(KINDS[OWNED])}) == [
        "cruxible.subject_kind.endpoint_declaration_fixed"
    ]


def _literal_owned_with_a_retired_subject(*, retire_kind: bool = False) -> dict[str, bytes]:
    """K = ``pt.owned`` keyed by ``serial`` alone, holding one retired Subject s1."""

    literal_owned = keyed(OWNED, O_SERIAL)
    parent = definitions(COMPONENT, VULN)
    parent[subject_kind_path(OWNED)] = render_subject_kind(
        successor_kind(
            literal_owned,
            lifecycle=ArtifactLifecycle(
                state="retired", predecessor_digest=subject_kind_digest(literal_owned).tagged
            ),
        )
        if retire_kind
        else literal_owned
    )
    put(parent, *end(COMPONENT, "c1", 1))
    serial = claim(50, O_SERIAL, literal("s1"), subject=path_of(OWNED, "s1"))
    put(parent, retired_shell(shell(OWNED, "s1")), retired_claim(serial))
    return parent


def test_a_retiring_successor_cannot_add_the_first_subject_part_either() -> None:
    """Review F1: the lifecycle of the successor does not bypass the Subject probe."""

    literal_owned = keyed(OWNED, O_SERIAL)
    retiring = successor_kind(
        literal_owned,
        identity_rule=KINDS[OWNED].identity_rule,
        lifecycle=ArtifactLifecycle(
            state="retired", predecessor_digest=subject_kind_digest(literal_owned).tagged
        ),
    )
    change = {subject_kind_path(OWNED): render_subject_kind(retiring)}
    assert judge(_literal_owned_with_a_retired_subject(), change) == [
        "cruxible.subject_kind.endpoint_declaration_fixed"
    ]
    # Without any Subject of the kind the same retiring successor passes.
    fresh = definitions(COMPONENT, VULN)
    fresh[subject_kind_path(OWNED)] = render_subject_kind(literal_owned)
    assert judge(fresh, change) == []


def test_a_claim_type_alone_cannot_make_a_kind_with_subjects_a_pair_kind() -> None:
    """Review F1: literal -> subject<K> on a key ClaimType, with no SubjectKind successor."""

    from cruxible_client.contracts.shapes import coerce_field_type

    to_subject = _type_successor(
        O_SERIAL, value=coerce_field_type({"kind": "subject", "subject_kinds": (COMPONENT,)})
    )
    change = {claim_type_path(O_SERIAL): to_subject}
    # The kind retired (grandfathered), its Subject retired: still refused.
    for retire_kind in (True, False):
        codes = judge(_literal_owned_with_a_retired_subject(retire_kind=retire_kind), change)
        assert "cruxible.subject_kind.endpoint_declaration_fixed" in codes, (retire_kind, codes)
    # Beside a SubjectKind successor (same rule) the kind's Subjects still refuse it.
    parent = _literal_owned_with_a_retired_subject()
    beside = {
        **change,
        subject_kind_path(OWNED): render_subject_kind(successor_kind(keyed(OWNED, O_SERIAL))),
    }
    assert "cruxible.subject_kind.endpoint_declaration_fixed" in judge(parent, beside)


def test_a_mixed_key_rekeys_its_literal_part_only() -> None:
    parent = definitions()
    put(parent, *end(COMPONENT, "c1", 1))
    owned = path_of(OWNED, "c1--s1")
    owner = claim(60, O_OWNER, to(C1), subject=owned)
    serial = claim(61, O_SERIAL, literal("s1"), subject=owned)
    created = put({}, shell(OWNED, "c1--s1"), owner, serial)
    assert judge(parent, created) == []
    parent.update(created)
    rekeyed = put({}, retired_claim(serial), claim(62, O_SERIAL, literal("s2"), subject=owned))
    assert judge(parent, rekeyed) == []
    moved = put({}, retired_claim(owner), claim(63, O_OWNER, to(C1), subject=owned))
    assert "cruxible.subject.key_endpoint_fixed" in judge(parent, moved)


# -- id re-derivation: one order, whatever the members' ---------------------------------


def test_colliding_pair_renderings_take_ids_in_the_one_creation_order() -> None:
    """(c1, "b") and (c1, "B") both render ``c1--b``: the lower key order takes it."""

    from cruxible_client.contracts.subject_kinds import subject_id_candidates

    parent = definitions()
    put(parent, *end(COMPONENT, "c1", 1))
    lower, upper = (("b", 70), ("B", 72))
    keys = {serial: ((O_OWNER, SubjectEnd(path=C1)), (O_SERIAL, serial)) for serial in ("b", "B")}
    first = min(keys, key=lambda serial: key_order(keys[serial]))
    second = "B" if first == "b" else "b"
    suffixed = list(subject_id_candidates([SubjectEnd(path=C1), second]))[1]

    def creation(serial: str, number: int, subject_id: str) -> dict[str, bytes]:
        at = path_of(OWNED, subject_id)
        return put(
            {},
            shell(OWNED, subject_id),
            claim(number, O_OWNER, to(C1), subject=at),
            claim(number + 1, O_SERIAL, literal(serial), subject=at),
        )

    numbers = dict((lower, upper))
    right = {
        **creation(first, numbers[first], "c1--b"),
        **creation(second, numbers[second], suffixed),
    }
    assert judge(parent, right) == []
    swapped = {
        **creation(second, numbers[second], "c1--b"),
        **creation(first, numbers[first], suffixed),
    }
    assert "cruxible.subject.id_not_assigned" in judge(parent, swapped)


def test_pair_layers_come_after_their_ends() -> None:
    from cruxible_core.indexes.subject_identity import creation_order, kind_depths

    layers = kind_depths(
        (EXPOSURE, COMPONENT, OWNED),
        subject_kind_of=KINDS.get,
        key_shape_of=SHAPES.get,
    )
    assert layers == {EXPOSURE: 1, COMPONENT: 0, VULN: 0, OWNED: 1}
    kinds, shapes = _chain(3)
    assert kind_depths(("pt.k3",), subject_kind_of=kinds.get, key_shape_of=shapes.get) == {
        "pt.k3": 3,
        "pt.k2": 2,
        "pt.k1": 1,
        "pt.k0": 0,
    }
    key = ((PURL, "c1"),)
    assert creation_order(0, COMPONENT, key, "m") < creation_order(1, "pt.a", key, "m")


def test_a_scope_fact_about_a_pair_is_not_an_endpoint() -> None:
    parent = _world()
    fact = claim(80, SCOPE, literal("required"), subject=E11)
    assert judge(parent, put({}, fact)) == []
    assert judge({**parent, **put({}, fact)}, put({}, retired_claim(fact))) == []


# -- the same law on the ordinary proposal route -----------------------------------------


def test_the_proposal_route_accepts_pairs_and_holds_their_law(tmp_path: Any) -> None:
    """Raw proposals: a pair world is admitted; a hint pair kind and an added endpoint are not."""

    from tests.core_support._pair_real import accept_raw, pair_instance

    instance, facts = pair_instance(tmp_path, pairs=1)
    (pair,) = facts
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    # A pair kind's SubjectKind successor relaxed to hint: the SubjectKind law refuses.
    relaxed = successor_kind(
        KINDS[EXPOSURE],
        identity_rule=keyed(EXPOSURE, E_COMPONENT, E_VULN, uniqueness="hint").identity_rule,
    )
    refused = accept_raw(
        instance, {subject_kind_path(EXPOSURE): render_subject_kind(relaxed)}, name="hint-pair"
    )
    assert refused.candidate is None
    assert {item.code for item in refused.evaluation.diagnostics} == {
        "cruxible.subject_kind.key_claim_type_invalid"
    }
    # A second component endpoint written onto the existing pair.
    from tests.core_support._pair_real import real_claim

    path, content = real_claim(instance, dict(tree), 900, E_COMPONENT, to(C1), subject=pair)
    added = accept_raw(instance, {path: content}, name="endpoint-added")
    assert added.candidate is None
    assert "cruxible.subject.key_endpoint_fixed" in {
        item.code for item in added.evaluation.diagnostics
    }, added.evaluation.diagnostics
    assert pair == path_of(EXPOSURE, "c1--v0")
