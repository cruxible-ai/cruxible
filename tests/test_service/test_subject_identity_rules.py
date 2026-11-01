"""SubjectKind identity rules: per-key-part normalizers, collisions, explicit re-keys (R12, D4).

- A key part's canonical value is its ClaimType's canonical form, then the
  normalizer its SubjectKind pins for it; two raw keys that canonicalize to one
  key name the same Subject on every route (authoring, write verbs,
  ``kind/<text>`` references) and the key Claim states the canonical value.
- A key Claim stating a non-canonical value is refused at the candidate boundary
  (``key_not_canonical``), whatever route wrote it.
- Changing a key part's normalizer is a SubjectKind successor that re-keys
  explicitly: every live value the new rule moves is re-authored in the same
  change (``rekey_unhandled`` otherwise), and two Subjects the new rule gives
  one unique key are refused (``key_conflict``), never merged silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    ClaimTypeInput,
    SubjectInput,
    SubjectKindInput,
    lower_authoring_input,
)
from cruxible_client.contracts.claims import parse_claim, render_claim
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.identity_normalizers import (
    LowercaseNormalizer,
    Pep503Normalizer,
    PurlTypeNormalizer,
)
from cruxible_client.contracts.subject_kinds import (
    SubjectKeyRule,
    SubjectKind,
    SubjectKindKeys,
    parse_subject_kind,
    render_subject_kind,
    subject_kind_digest,
    subject_kind_path,
)
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.indexes.subject_identity import (
    candidate_identity_issues,
    tree_key_rules,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._support import initialize_local
from tests.test_service.test_subject_identity import (
    _key,
    accept,
    change_set,
    compile_,
    key_claims,
    tree,
    write,
)

PACKAGE = "pkg.package"
NAME = f"{PACKAGE}.name"
RELEASE = "pkg.release"
PURL = f"{RELEASE}.purl"
TAG = "pkg.tag"
LABEL = f"{TAG}.label"
_TRIM = ("nfc", "trim")


def _codes(result: Any) -> set[str]:
    return {item.code for item in result.frontier.diagnostics}


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    instance, _owner = initialize_local(tmp_path)
    accept(
        instance,
        change_set(
            ClaimTypeInput(
                kind="claim_type", claim_type=_key(PACKAGE, "name", canonical_form=_TRIM)
            ),
            SubjectKindInput(
                kind="subject_kind",
                name=PACKAGE,
                keys=(NAME,),
                normalizers={"name": Pep503Normalizer(version=1)},
                uniqueness="unique",
            ),
            ClaimTypeInput(
                kind="claim_type", claim_type=_key(RELEASE, "purl", canonical_form=_TRIM)
            ),
            SubjectKindInput(
                kind="subject_kind",
                name=RELEASE,
                keys=(PURL,),
                normalizers={PURL: PurlTypeNormalizer(version=1, identity="package")},
                uniqueness="unique",
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_key(TAG, "label", canonical_form=_TRIM)),
            SubjectKindInput(kind="subject_kind", name=TAG, keys=(LABEL,), uniqueness="unique"),
        ),
    )
    return instance


def _create(instance: PlaybillInstance, kind: str, key: str) -> Any:
    return accept(
        instance, lower_authoring_input(SubjectInput(kind="subject", subject_kind=kind, key=key))
    )


def _created_by(instance: PlaybillInstance, kind: str, key: str) -> list[str]:
    """The Subjects a creation by ``key`` would add (none when it reuses one)."""

    from cruxible_client.contracts.authoring.models import (
        ChangeSetAuthoringPayload,
        SubjectCreationAuthoringPayload,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.core_support._identity_support import OWNER

    _intent, computed = AuthoringIntentCoordinator.for_instance(instance).preview(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(
            members=(SubjectCreationAuthoringPayload(subject_kind=kind, key_value=key),)
        ),
        canonical_timestamp="2026-10-09T12:00:00.000000Z",
    )
    if computed.lowered is None:
        return []
    return [path for path, _c in computed.lowered.changed_members if path.startswith("subjects/")]


def _subjects_of(instance: PlaybillInstance, kind: str) -> list[str]:
    return sorted(path for path in tree(instance) if path.startswith(f"subjects/{kind}/"))


def test_the_artifact_pins_each_key_parts_normalizer(instance: PlaybillInstance) -> None:
    current = tree(instance)
    package = parse_subject_kind(
        current[subject_kind_path(PACKAGE)], path=subject_kind_path(PACKAGE)
    )
    tag = parse_subject_kind(current[subject_kind_path(TAG)], path=subject_kind_path(TAG))
    assert package.artifact_format == "cruxible-subject-kind-v2"
    assert package.normalizer(NAME) == Pep503Normalizer(version=1)
    # A key part the author named no normalizer for takes none@1, written out.
    assert b'"normalizer":"none"' in current[subject_kind_path(TAG)].replace(b" ", b"")
    assert tag.normalizer(LABEL).normalizer == "none"
    card = service_playbill_get(
        instance,
        request=GetRequest(
            ref=f"SubjectKind:{PACKAGE}", evaluation_time=datetime(2026, 10, 9, tzinfo=UTC)
        ),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    )
    assert card.card is not None
    assert card.card.model_dump(mode="json")["normalizers"] == {NAME: "pep503@1"}


def test_two_raw_keys_that_normalize_to_one_key_name_one_subject(
    instance: PlaybillInstance,
) -> None:
    _create(instance, PACKAGE, "Django_REST")
    assert _subjects_of(instance, PACKAGE) == [f"subjects/{PACKAGE}/django-rest.json"]
    # The key Claim states the canonical value; the raw spelling stays in its evidence.
    assert key_claims(instance, PACKAGE, "django-rest") == {NAME: "django-rest"}
    # Another spelling of the same key reuses the Subject: nothing new is created.
    assert _created_by(instance, PACKAGE, "django.Rest") == []
    assert _created_by(instance, PACKAGE, "flask") == [f"subjects/{PACKAGE}/flask.json"]
    # A write names it by any spelling of its key, through kind/<text>.
    for spelled in ("Django-Rest", "django__rest"):
        outcome = write(
            instance,
            {
                "op": "add",
                "subject": f"{PACKAGE}/{spelled}",
                "field": "semantic.alias",
                "value": spelled,
            },
        )
        assert outcome.status == "accepted", outcome
        assert outcome.subjects_added == ()
    assert _subjects_of(instance, PACKAGE) == [f"subjects/{PACKAGE}/django-rest.json"]


def test_a_purl_key_excludes_qualifiers_from_package_identity(
    instance: PlaybillInstance,
) -> None:
    _create(instance, RELEASE, "pkg:deb/Debian/curl@8.5.0-2?arch=amd64")
    (created,) = _subjects_of(instance, RELEASE)
    subject_id = created.removeprefix(f"subjects/{RELEASE}/").removesuffix(".json")
    assert key_claims(instance, RELEASE, subject_id) == {PURL: "pkg:deb/debian/curl@8.5.0-2"}
    assert _created_by(instance, RELEASE, "pkg:deb/debian/curl@8.5.0-2?arch=arm64") == []
    assert _created_by(instance, RELEASE, "pkg:deb/debian/curl@8.6.0") != []
    assert _subjects_of(instance, RELEASE) == [created]


def test_case_sensitive_npm_names_are_distinct_subjects(instance: PlaybillInstance) -> None:
    """npm scope and name are case-sensitive (purl npm definition): no merge by case."""

    _create(instance, RELEASE, "pkg:npm/JSONStream@1.3.5")
    assert _created_by(instance, RELEASE, "pkg:npm/jsonstream@1.3.5") != []
    _create(instance, RELEASE, "pkg:npm/jsonstream@1.3.5")
    assert len(_subjects_of(instance, RELEASE)) == 2
    values = {
        next(
            iter(
                key_claims(instance, RELEASE, path.rsplit("/", 1)[1].removesuffix(".json")).values()
            )
        )
        for path in _subjects_of(instance, RELEASE)
    }
    assert values == {"pkg:npm/JSONStream@1.3.5", "pkg:npm/jsonstream@1.3.5"}
    # A case-insensitive type still merges: github owner and repository fold.
    _create(instance, RELEASE, "pkg:github/Cruxible-AI/Core@v1")
    assert _created_by(instance, RELEASE, "pkg:github/cruxible-ai/core@v1") == []


def test_a_key_its_normalizer_cannot_read_is_refused_naming_why(
    instance: PlaybillInstance,
) -> None:
    refused = compile_(
        instance,
        lower_authoring_input(SubjectInput(kind="subject", subject_kind=PACKAGE, key="_private")),
    )
    assert refused.verdict == "refused"
    assert "cruxible.subject.key_invalid" in _codes(refused)
    assert any("PEP 508" in item.message for item in refused.frontier.diagnostics)
    ambiguous = write(
        instance,
        {"op": "create", "label": "r", "subject_kind": RELEASE, "key": "pkg:npm/x@1?a=1&a=2"},
    )
    assert (
        ambiguous.refusal is not None and ambiguous.refusal.code == "cruxible.subject.key_invalid"
    )


def _key_claim(
    instance: PlaybillInstance, kind: str, subject_id: str, predicate: str
) -> tuple[str, Any]:
    for path, content in tree(instance).items():
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(content, path=path)
        if (
            claim.statement.predicate == predicate
            and claim.statement.subject.artifact_path == subject_path(kind, subject_id)
        ):
            return path, claim
    raise AssertionError(f"no {predicate} Claim about {kind}/{subject_id}")


def _restated(claim: Any, value: object) -> bytes:
    from cruxible_client.contracts.claims import LiteralClaimObject

    statement = claim.statement.model_copy(update={"object": LiteralClaimObject(value=value)})
    return render_claim(claim.model_copy(update={"statement": statement}))


def test_a_key_claim_stating_a_non_canonical_value_is_refused_at_the_boundary(
    instance: PlaybillInstance,
) -> None:
    _create(instance, PACKAGE, "requests")
    path, claim = _key_claim(instance, PACKAGE, "requests", NAME)
    parent = tree(instance)
    issues = candidate_identity_issues(
        current_tree=parent,
        candidate_tree={**parent, path: _restated(claim, "Requests")},
        scope=(path,),
    )
    assert [item.code for item in issues] == ["cruxible.subject.key_not_canonical"]
    assert "'requests'" in issues[0].message
    unreadable = candidate_identity_issues(
        current_tree=parent,
        candidate_tree={**parent, path: _restated(claim, "not a name")},
        scope=(path,),
    )
    assert [item.code for item in unreadable] == ["cruxible.subject.key_invalid"]
    # The same value under the kind's own rule is fine (a no-op restatement).
    assert (
        candidate_identity_issues(
            current_tree=parent,
            candidate_tree={**parent, path: _restated(claim, "requests")},
            scope=(path,),
        )
        == ()
    )


def _successor(instance: PlaybillInstance, kind: str, rule: SubjectKeyRule) -> tuple[str, bytes]:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle

    path = subject_kind_path(kind)
    current = parse_subject_kind(tree(instance)[path], path=path)
    assert isinstance(current.identity_rule, SubjectKindKeys)
    successor = SubjectKind(
        identity=current.identity,
        description=current.description,
        identity_rule=current.identity_rule.model_copy(update={"parts": (rule,)}),
        lifecycle=ArtifactLifecycle(predecessor_digest=subject_kind_digest(current).tagged),
    )
    return path, render_subject_kind(successor)


def test_a_changed_normalizer_is_an_explicit_rekey_never_a_silent_one(
    instance: PlaybillInstance,
) -> None:
    _create(instance, TAG, "Alpha")
    _create(instance, TAG, "beta")
    kind_path, rekeyed = _successor(
        instance, TAG, SubjectKeyRule(claim_type=LABEL, normalizer=LowercaseNormalizer(version=1))
    )
    parent = tree(instance)
    # The new rule re-spells "Alpha": the change must re-author it, or it refuses.
    issues = candidate_identity_issues(
        current_tree=parent, candidate_tree={**parent, kind_path: rekeyed}, scope=(kind_path,)
    )
    assert [item.code for item in issues] == ["cruxible.subject_kind.rekey_unhandled"]
    assert issues[0].existing == (f"{TAG}/alpha",)
    assert "none@1 to lowercase@1" in issues[0].message
    # Re-authoring the moved key value in the same change is the explicit re-key.
    claim_path, claim = _key_claim(instance, TAG, "alpha", LABEL)
    assert (
        candidate_identity_issues(
            current_tree=parent,
            candidate_tree={**parent, kind_path: rekeyed, claim_path: _restated(claim, "alpha")},
            scope=(kind_path, claim_path),
        )
        == ()
    )
    # The rules core reads at a coordinate follow the accepted SubjectKind.
    rules = tree_key_rules({**parent, kind_path: rekeyed})
    assert rules.canonical(TAG, "label", "GAMMA") == "gamma"
    assert tree_key_rules(parent).canonical(TAG, "label", "GAMMA") == "GAMMA"
    assert rules.rule_token(TAG) != tree_key_rules(parent).rule_token(TAG)


def test_a_rekey_that_would_merge_two_unique_subjects_is_refused(
    instance: PlaybillInstance,
) -> None:
    _create(instance, TAG, "Gamma")
    _create(instance, TAG, "gamma")
    subjects = _subjects_of(instance, TAG)
    assert len(subjects) == 2
    kind_path, rekeyed = _successor(
        instance, TAG, SubjectKeyRule(claim_type=LABEL, normalizer=LowercaseNormalizer(version=1))
    )
    parent = tree(instance)
    upper_id = next(
        path.removeprefix(f"subjects/{TAG}/").removesuffix(".json")
        for path in subjects
        if key_claims(instance, TAG, path.removeprefix(f"subjects/{TAG}/").removesuffix(".json"))
        == {LABEL: "Gamma"}
    )
    claim_path, claim = _key_claim(instance, TAG, upper_id, LABEL)
    issues = candidate_identity_issues(
        current_tree=parent,
        candidate_tree={**parent, kind_path: rekeyed, claim_path: _restated(claim, "gamma")},
        scope=(kind_path, claim_path),
    )
    # Two raw keys that canonicalize to one key are one Subject: two live holders
    # of it under a unique kind are ambiguous, so the re-key refuses naming both.
    assert "cruxible.subject.key_conflict" in {item.code for item in issues}
    assert "cruxible.subject_kind.rekey_unhandled" not in {item.code for item in issues}


def test_the_law_refuses_a_text_normalizer_on_a_non_string_key(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    refused = compile_(
        instance,
        change_set(
            ClaimTypeInput(
                kind="claim_type",
                claim_type=_key(
                    PACKAGE, "number", literal_schema={"type": "integer"}, canonical_form=None
                ),
            ),
            SubjectKindInput(
                kind="subject_kind",
                name=PACKAGE,
                keys=(f"{PACKAGE}.number",),
                normalizers={"number": LowercaseNormalizer(version=1)},
            ),
        ),
    )
    assert refused.verdict == "refused"
    assert "cruxible.subject_kind.key_claim_type_invalid" in _codes(refused)


def test_a_normalizer_names_one_of_the_keys(tmp_path: Path) -> None:
    from cruxible_client.contracts.authoring.inputs import AuthoringInputError

    with pytest.raises(AuthoringInputError) as caught:
        lower_authoring_input(
            SubjectKindInput(
                kind="subject_kind",
                name=PACKAGE,
                keys=(NAME,),
                normalizers={"version": Pep503Normalizer(version=1)},
            )
        )
    assert "names no key" in str(caught.value)
    del tmp_path


def test_key_rules_canonicalize_whole_and_part_keys(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.shapes import KeyValueRefused

    rules = tree_key_rules(tree(instance))
    assert rules.canonical(PACKAGE, None, "Zope.Interface") == "zope-interface"
    assert rules.canonical(PACKAGE, "name", " Zope_Interface ") == "zope-interface"
    assert rules.canonical(PACKAGE, NAME, "zope-interface") == "zope-interface"
    assert rules.canonical(PACKAGE, None, {"name": "Zope_Interface"}) == {"name": "zope-interface"}
    for kind, part, value in (
        (PACKAGE, "version", "x"),
        (PACKAGE, None, "_bad"),
        ("pkg.unknown", None, "x"),
        (PACKAGE, None, {"other": "x"}),
    ):
        with pytest.raises(KeyValueRefused):
            rules.canonical(kind, part, value)


def test_a_run_reads_the_identity_rules_at_its_admitted_coordinate(
    instance: PlaybillInstance,
) -> None:
    """The Procedure runtime's key rules read the accepted SubjectKinds and key ClaimTypes."""

    from cruxible_core.service.procedures.procedure_runs import accepted_key_rules

    coordinate = instance.accepted_coordinate()
    rules = accepted_key_rules(instance, coordinate)
    assert rules.canonical(PACKAGE, "name", "Zope.Interface") == "zope-interface"
    assert rules.canonical(RELEASE, None, "pkg:npm/@Scope/Left-Pad@1?x=1") == (
        "pkg:npm/%40Scope/Left-Pad@1"
    )
    assert rules.canonical(RELEASE, None, "pkg:github/Org/Repo@v1") == "pkg:github/org/repo@v1"
    assert rules.rule_token(PACKAGE) == tree_key_rules(tree(instance)).rule_token(PACKAGE)


def test_an_encoded_dot_subpath_reads_as_one_key_on_every_route(
    instance: PlaybillInstance,
) -> None:
    """Creation, the candidate boundary, key reads and extraction agree on one canonical key."""

    from cruxible_client.contracts.shapes import parse_type
    from cruxible_core.indexes.subject_identity import tree_key_reader

    _create(instance, RELEASE, "pkg:generic/tool@1#%2E/lib/%2E%2E")
    (created,) = _subjects_of(instance, RELEASE)
    subject_id = created.removeprefix(f"subjects/{RELEASE}/").removesuffix(".json")
    assert key_claims(instance, RELEASE, subject_id) == {PURL: "pkg:generic/tool@1#lib"}
    for spelled in (
        "pkg:generic/tool@1#lib",
        "pkg:generic/tool@1#./lib",
        "PKG:generic/tool@1#%6Cib",
    ):
        assert _created_by(instance, RELEASE, spelled) == []
        assert tree_key_reader(tree(instance))(RELEASE, spelled) == (
            (PURL, "pkg:generic/tool@1#lib"),
        )
    rules = tree_key_rules(tree(instance))
    from cruxible_client.contracts.shapes import FieldPresent, cast_value

    cast = cast_value(
        FieldPresent(value="pkg:generic/tool@1#%2E/lib", at="/k"),
        parse_type(f"key<{RELEASE}>"),
        keys=rules,
    )
    assert isinstance(cast, FieldPresent) and cast.value == "pkg:generic/tool@1#lib"
    refused = _created_by_refusal(instance, RELEASE, "pkg:generic/tool@1#a%2Fb")
    assert refused == "cruxible.subject.key_invalid"


def _created_by_refusal(instance: PlaybillInstance, kind: str, key: str) -> str | None:
    outcome = write(instance, {"op": "create", "label": "r", "subject_kind": kind, "key": key})
    return None if outcome.refusal is None else outcome.refusal.code


# -- query operands on a key predicate take its normalizer (R12) ---------------------


def _compact(instance: PlaybillInstance, **fields: Any) -> list[str]:
    from cruxible_client.contracts.compact_query import QueryRequest
    from cruxible_core.service.discovery.compact_query import service_playbill_query

    result = service_playbill_query(instance, request=QueryRequest.model_validate(fields))
    return sorted(row["subject_id"] for row in result.rows)


def test_a_compact_filter_on_a_key_names_the_same_key_by_any_spelling(
    instance: PlaybillInstance,
) -> None:
    _create(instance, PACKAGE, "django-rest")
    _create(instance, PACKAGE, "flask")
    for spelled in ("Django_REST", "django.rest", " django-rest "):
        assert _compact(instance, kind=PACKAGE, where=[{"field": "name", "eq": spelled}]) == [
            "django-rest"
        ]
    assert _compact(
        instance, kind=PACKAGE, where=[{"field": "name", "in": ["FLASK", "Django__Rest"]}]
    ) == ["django-rest", "flask"]
    assert _compact(instance, kind=PACKAGE, where=[{"field": "name", "ne": "Django_REST"}]) == [
        "flask"
    ]
    from cruxible_core.service.read_refusals import ReadRefusalError

    with pytest.raises(ReadRefusalError) as caught:
        _compact(instance, kind=PACKAGE, where=[{"field": "name", "eq": "_private"}])
    assert caught.value.error_code == "cruxible.query.value_type_mismatch"


def _named_query(instance: PlaybillInstance, name: str, where: str, **spec: Any) -> None:
    from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
    from cruxible_client.contracts.query.authored import QuerySpecInput

    payload = lower_authoring_input(
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": name,
                    "from": {"p": PACKAGE},
                    "fields": ["p", {"name": "p.pkg.package.name"}],
                    "where": where,
                    **spec,
                }
            ),
        )
    )
    checked = compile_(instance, payload)
    assert checked.verdict == "passed", [
        (item.code, item.message) for item in checked.frontier.diagnostics
    ]
    accept(instance, payload)


def _run(instance: PlaybillInstance, query: str, **parameters: object) -> list[object]:
    from cruxible_core.service.discovery.query import service_run_playbill_query

    run = service_run_playbill_query(
        instance,
        name=query,
        evaluation_time=datetime(2026, 10, 9, 13, tzinfo=UTC),
        parameters=parameters or None,
    )
    assert run.result.verdict == "completed", run.result
    return sorted(row.fields[1].value for row in run.result.rows)  # type: ignore[union-attr]


def test_a_named_query_literal_and_parameter_name_a_key_by_any_spelling(
    instance: PlaybillInstance,
) -> None:
    _create(instance, PACKAGE, "django-rest")
    _create(instance, PACKAGE, "flask")
    _named_query(instance, "pkg.django", 'p.pkg.package.name == "Django_REST"')
    _named_query(instance, "pkg.named", "p.pkg.package.name == $name", params={"name": "string"})
    _named_query(
        instance,
        "pkg.either",
        'p.pkg.package.name in ("FLASK", "django.rest")',
    )
    assert _run(instance, "pkg.django") == ["django-rest"]
    for spelled in ("Django_REST", "DJANGO-rest", "django__rest"):
        assert _run(instance, "pkg.named", name=spelled) == ["django-rest"]
    assert _run(instance, "pkg.named", name="flask") == ["flask"]
    assert _run(instance, "pkg.either") == ["django-rest", "flask"]


def test_a_write_to_a_key_field_stores_its_canonical_value(instance: PlaybillInstance) -> None:
    _create(instance, PACKAGE, "requests")
    outcome = write(
        instance,
        {"op": "set", "subject": f"{PACKAGE}/requests", "field": "name", "value": "Requests_Two"},
    )
    assert outcome.status == "accepted", outcome
    assert key_claims(instance, PACKAGE, "requests") == {NAME: "requests-two"}
    refused = write(
        instance,
        {"op": "set", "subject": f"{PACKAGE}/requests", "field": "name", "value": "_bad"},
    )
    assert refused.refusal is not None and refused.refusal.code == "cruxible.subject.key_invalid"


# -- F-001: a served query's operand takes the shared key canonicalization ------------


@pytest.fixture
def ruled(tmp_path: Path) -> PlaybillInstance:
    """Two kinds whose form and normalizer can disagree on an operand."""

    instance, _owner = initialize_local(tmp_path)
    accept(
        instance,
        change_set(
            ClaimTypeInput(
                kind="claim_type", claim_type=_key("pkg.lower", "label", canonical_form=("trim",))
            ),
            SubjectKindInput(
                kind="subject_kind",
                name="pkg.lower",
                keys=("pkg.lower.label",),
                normalizers={"label": LowercaseNormalizer(version=1)},
            ),
            ClaimTypeInput(
                kind="claim_type", claim_type=_key("pkg.purl", "purl", canonical_form=("casefold",))
            ),
            SubjectKindInput(
                kind="subject_kind",
                name="pkg.purl",
                keys=("pkg.purl.purl",),
                normalizers={"purl": PurlTypeNormalizer(version=1, identity="package")},
            ),
        ),
    )
    _create(instance, "pkg.lower", "Ready")
    _create(instance, "pkg.purl", "pkg:generic/x")
    return instance


def _param_query_on(instance: PlaybillInstance, kind: str, predicate: str, operator: str) -> str:
    from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
    from cruxible_client.contracts.query.authored import QuerySpecInput

    name = f"{kind}.by_{'eq' if operator == '==' else 'ne'}"
    accept(
        instance,
        lower_authoring_input(
            QueryDefinitionInput(
                kind="query_definition",
                query_definition=QuerySpecInput.model_validate(
                    {
                        "query": name,
                        "from": {"p": kind},
                        "fields": ["p", {"value": f"p.{predicate}"}],
                        "where": f"p.{predicate} {operator} $value",
                        "params": {"value": "string"},
                    }
                ),
            )
        ),
    )
    return name


@pytest.mark.parametrize(
    ("kind", "predicate", "stored", "bad"),
    [
        ("pkg.lower", "pkg.lower.label", "ready", "   "),
        ("pkg.purl", "pkg.purl.purl", "pkg:generic/x", "pkg:generic/%C3%A9"),
    ],
    ids=["empty-after-form", "form-respells-normalized"],
)
def test_a_served_operand_no_key_can_equal_refuses(
    ruled: PlaybillInstance, kind: str, predicate: str, stored: str, bad: str
) -> None:
    from cruxible_core.service.discovery.query import service_run_playbill_query
    from cruxible_core.service.read_refusals import ReadRefusalError

    for operator in ("==", "!="):
        name = _param_query_on(ruled, kind, predicate, operator)

        def run(value: str) -> Any:
            return service_run_playbill_query(
                ruled,
                name=name,
                evaluation_time=datetime(2026, 10, 9, 13, tzinfo=UTC),
                parameters={"value": value},
            ).result

        refused = run(bad)
        assert refused.verdict == "refused", (operator, refused)
        assert refused.refusal is not None
        assert refused.refusal.code == "cruxible.query.value_type_mismatch"
        assert run(stored).verdict == "completed"
    with pytest.raises(ReadRefusalError) as caught:
        _compact(ruled, kind=kind, where=[{"field": predicate.rsplit(".", 1)[1], "eq": bad}])
    assert caught.value.error_code == "cruxible.query.value_type_mismatch"
    with pytest.raises(ReadRefusalError):
        _compact(
            ruled, kind=kind, where=[{"field": predicate.rsplit(".", 1)[1], "in": [stored, bad]}]
        )
