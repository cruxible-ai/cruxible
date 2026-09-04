"""The typed world facade: what `pb.world()` names, and what it refuses."""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import Playbill
from cruxible_client import contracts as api
from cruxible_client.authoring.sdk_types import (
    AbsentSubject,
    Cardinality,
    ClaimObjectKind,
    ClaimRole,
    ClaimTypeRef,
    LiteralSchemaError,
    LiteralValue,
    LiteralValueTypeError,
    PendingClaimTypeRef,
    PendingSubjectRef,
    ReferentSensitivity,
)
from cruxible_client.authoring.world import (
    KindNamespace,
    World,
    WorldClaimType,
    WorldStructureError,
)
from cruxible_client.authoring.world_stub import STUB_HEADER_TAG
from cruxible_client.contracts.projection import AcceptedCoordinate

_DIGEST = "sha256:" + "1" * 64
_COORDINATE = api.PlaybillAcceptedCoordinate(
    git_oid="a" * 40,
    semantic_root=_DIGEST,
    generation_root="sha256:" + "2" * 64,
    compiler_digest="sha256:" + "3" * 64,
)
_MOVED_COORDINATE = api.PlaybillAcceptedCoordinate(
    git_oid="b" * 40,
    semantic_root=_DIGEST,
    generation_root="sha256:" + "2" * 64,
    compiler_digest="sha256:" + "3" * 64,
)

SEVERITY = "sec.vuln.severity"
AFFECTS = "sec.vuln.affects_package"
LANDED_AT = "dev.batch.landed_at"


def _claim_type(predicate: str, **overrides: object) -> api.PlaybillClaimTypeView:
    envelope: dict[str, Any] = {
        "artifact_format": "playbill-claim-type-v1",
        "identity": {"kind": "ClaimType", "name": predicate},
        "predicate": predicate,
        "allowed_subject_kinds": ("sec.vulnerability",),
        "object_kind": "literal",
        "literal_schema": {"type": "string", "enum": ["high", "low"]},
        "allowed_object_subject_kinds": (),
        "cardinality": "one",
        "permitted_roles": ("observation",),
        "referent_sensitivity": "identity",
        "lifecycle": {"state": "live"},
    }
    envelope.update(overrides)
    return api.PlaybillClaimTypeView(
        coordinate=_COORDINATE,
        path=f"claim-types/{predicate}.json",
        predicate=predicate,
        identity=f"ClaimType:{predicate}",
        artifact_digest=_DIGEST,
        envelope=envelope,
    )


def _subject_view(
    subject_kind: str,
    subject_id: str,
    *,
    state: str = "live",
) -> api.PlaybillSubjectView:
    """The Subject projection exactly as the served list verb renders it."""

    return api.PlaybillSubjectView(
        coordinate=_COORDINATE,
        envelope={
            "identity": f"Subject:{subject_kind}/{subject_id}",
            "kind": "subject",
            "format_tag": "playbill-subject-v1",
            "path": f"subjects/{subject_kind}/{subject_id}.json",
            "artifact_digest": _DIGEST,
            "predecessor_digest": None,
            "revision": 1,
        },
        facts=[
            {
                "schema_id": "playbill.subject.identity",
                "schema_version": 1,
                "fact_key": "stable_referent",
                "value": {
                    "subject_kind": subject_kind,
                    "subject_id": subject_id,
                    "identity": {
                        "kind": "Subject",
                        "name": f"{subject_kind}/{subject_id}",
                    },
                },
            },
            {
                "schema_id": "playbill.subject.lifecycle",
                "schema_version": 1,
                "fact_key": "accepted_shell",
                "value": {"lifecycle": {"state": state}},
            },
        ],
    )


class _WorldClient:
    """A spy over exactly the read verbs the world facade is allowed to use."""

    def __init__(self) -> None:
        self.coordinate = _COORDINATE
        self.subject_list_calls = 0
        self.claim_type_list_calls = 0
        self.searches: list[dict[str, Any]] = []
        self.claim_reads: list[str] = []
        self.claim_predicates: dict[str, str] = {}
        self.retired_severity = False

    def search_playbill(self, _instance_id: str, **values: Any) -> api.PlaybillSearchResult:
        self.searches.append(dict(values))
        rows = (
            [
                {
                    "kind": "claim",
                    "identity": "CLM-" + "9" * 32,
                    "address": {
                        "artifact_path": "claims/CLM-" + "9" * 32 + ".json",
                        "selector": {"kind": "claim_statement"},
                    },
                    "status": "accepted",
                    "subject": values["subject"],
                    "predicate": self.claim_predicates.get("CLM-" + "9" * 32, SEVERITY),
                    "title": self.claim_predicates.get("CLM-" + "9" * 32, SEVERITY),
                }
            ]
            if values.get("subject") is not None
            else []
        )
        return api.PlaybillSearchResult(
            mode=values["mode"],
            coordinate=self.coordinate,
            evaluation_time=str(values["evaluation_time"]),
            rows=rows,
            orientation={"state": "empty"} if values["mode"] == "orient" else None,
            selection_basis_digest="sha256:" + "4" * 64,
            truncated=False,
            result_digest="sha256:" + "5" * 64,
        )

    def list_playbill_claim_types(
        self, _instance_id: str, *, at: Any = None
    ) -> api.PlaybillClaimTypeList:
        self.claim_type_list_calls += 1
        return api.PlaybillClaimTypeList(
            coordinate=self.coordinate,
            claim_types=[
                _claim_type(
                    SEVERITY,
                    lifecycle={"state": "retired" if self.retired_severity else "live"},
                ),
                _claim_type(
                    AFFECTS,
                    object_kind="subject",
                    literal_schema=None,
                    allowed_object_subject_kinds=("sec.package",),
                ),
                _claim_type(
                    LANDED_AT,
                    allowed_subject_kinds=("dev.batch",),
                    literal_schema={"type": "string", "pattern": "^[0-9a-f]{40}$"},
                ),
            ],
        )

    def list_playbill_subjects(
        self, _instance_id: str, *, at: Any = None
    ) -> api.PlaybillSubjectList:
        self.subject_list_calls += 1
        return api.PlaybillSubjectList(
            coordinate=self.coordinate,
            subjects=[
                _subject_view("sec.package", "cryptography"),
                _subject_view("sec.vulnerability", "cve-2026-69247"),
                _subject_view("dev.batch", "p2c"),
                _subject_view("dev.batch", "retired_batch", state="retired"),
            ],
        )

    def get_playbill_claim(self, _instance_id: str, identity: str, **_values: Any) -> Any:
        self.claim_reads.append(identity)
        predicate = self.claim_predicates.get(identity, SEVERITY)
        return api.PlaybillClaimViewV2(
            tag="playbill-claim-read-v2",
            coordinate_kind="canonical",
            coordinate=self.coordinate,
            envelope={"identity": identity, "revision": 1},
            admission_evaluation_time="2026-09-07T12:00:00Z",
            statement=api.ClaimStatementCardV1(
                subject={
                    "artifact_path": "subjects/sec.vulnerability/cve-2026-69247.json",
                    "selector": {"scheme": "artifact-v1", "value": ""},
                },
                predicate=predicate,
                object={"kind": "literal", "value": "high"},
                role="observation",
                qualifier=None,
                lifecycle="live",
            ),
            facts=[
                {
                    "schema_id": "playbill.claim.statement",
                    "value": {
                        "subject": {
                            "artifact_path": "subjects/sec.vulnerability/cve-2026-69247.json"
                        },
                        "predicate": predicate,
                        "qualifier": None,
                        "role": "observation",
                        "object": {"kind": "literal", "value": "high"},
                    },
                },
                {
                    "schema_id": "playbill.claim.lifecycle",
                    "value": {"lifecycle": {"state": "live"}},
                },
                {
                    "schema_id": "playbill.claim.current_verdict",
                    "value": {"verdict": "supported"},
                },
            ],
            admission_accounts=[],
        )

    def explain_playbill_subject(self, _instance_id: str, **values: Any) -> dict[str, Any]:
        return {"explained": values["subject"]}

    def close(self) -> None:
        return None


def _workspace(path: Path) -> None:
    (path / ".playbill").mkdir()
    (path / ".playbill" / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: corpus.runbook
    locator: corpus/runbook.md
    document_id: runbook
    document_kind: runbook
    title: Runbook
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:runbook]
""",
        encoding="utf-8",
    )
    (path / "corpus").mkdir()
    (path / "corpus" / "runbook.md").write_text("Patch within 48 hours.\n", encoding="utf-8")


@pytest.fixture
def connection(tmp_path: Path) -> tuple[Playbill, _WorldClient]:
    _workspace(tmp_path)
    client = _WorldClient()
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    return playbill, client


def test_world_names_nested_dotted_kinds_and_answers_subjects_both_ways(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """`w.sec.package` nests, and a Subject answers by attribute or by index."""

    playbill, _client = connection
    world = playbill.world()

    assert world.kinds == ("dev.batch", "sec.package", "sec.vulnerability")
    assert world.predicates == (LANDED_AT, AFFECTS, SEVERITY)
    assert world.sec.package.subject_kind == "sec.package"
    assert world.sec.subject_kind is None

    by_attribute = world.sec.package.cryptography
    by_index = world.sec.package["cryptography"]
    assert by_attribute == by_index
    assert by_attribute.address == "sec.package/cryptography"
    assert by_attribute.coordinate == world.coordinate
    assert world.sec.vulnerability["cve-2026-69247"].address == ("sec.vulnerability/cve-2026-69247")


def test_an_absent_subject_names_its_kind_id_and_coordinate(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """The typed refusal has to be actionable without a second read."""

    playbill, _client = connection
    world = playbill.world()

    with pytest.raises(AbsentSubject) as absent:
        world.sec.vulnerability["cve-2026-00000"]

    assert absent.value.subject_kind == "sec.vulnerability"
    assert absent.value.subject_id == "cve-2026-00000"
    assert absent.value.coordinate == world.coordinate
    assert "sec.vulnerability.define('cve-2026-00000')" in str(absent.value)

    with pytest.raises(AbsentSubject):
        world.sec.vulnerability.absent_one


def test_a_predicate_is_a_claim_type_ref_carrying_its_own_structure(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """Structure is a read the caller otherwise makes by hand every time."""

    playbill, _client = connection
    world = playbill.world()
    severity = world.sec.vuln.severity

    assert isinstance(severity, ClaimTypeRef)
    assert isinstance(severity, WorldClaimType)
    assert severity.address == SEVERITY
    assert severity.predicate == SEVERITY
    assert severity.object_kind is ClaimObjectKind.LITERAL
    assert severity.cardinality is Cardinality.ONE
    assert severity.permitted_roles == (ClaimRole.OBSERVATION,)
    assert severity.referent_sensitivity is ReferentSensitivity.IDENTITY
    assert severity.allowed_subject_kinds == ("sec.vulnerability",)
    assert world.sec.vuln.affects_package.allowed_object_subject_kinds == ("sec.package",)


def test_enum_members_are_typed_values_and_an_unknown_member_names_the_enum(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    playbill, _client = connection
    world = playbill.world()

    high = world.sec.vuln.severity.high
    assert isinstance(high, LiteralValue)
    assert high.predicate == SEVERITY
    assert high.value == "high"
    assert high.coordinate == world.coordinate
    assert world.sec.vuln.severity.members == ("high", "low")

    with pytest.raises(AttributeError, match="admits: high, low"):
        world.sec.vuln.severity.critical


def test_a_non_enum_schema_validates_its_constructor_before_the_wire(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """A 39-character digest is a caller mistake, not a proposal to submit."""

    playbill, _client = connection
    world = playbill.world()

    landed = world.dev.batch.landed_at("f" * 40)
    assert landed.predicate == LANDED_AT
    assert landed.value == "f" * 40

    with pytest.raises(LiteralSchemaError, match="declared pattern"):
        world.dev.batch.landed_at("f" * 39)
    with pytest.raises(LiteralSchemaError, match="value is not string"):
        world.dev.batch.landed_at(7)
    with pytest.raises(WorldStructureError, match="subject object"):
        world.sec.vuln.affects_package("sec.package/cryptography")


def test_a_kind_loads_its_subjects_only_when_one_is_first_asked_for(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """A thousand-Subject world must cost the vocabulary and nothing else."""

    playbill, client = connection
    world = playbill.world()

    assert client.claim_type_list_calls == 1
    assert client.subject_list_calls == 0

    assert world.sec.package.cryptography.address == "sec.package/cryptography"
    assert client.subject_list_calls == 1

    # A second kind, and a second read of the first, are answered from what the
    # first ask already resolved.
    assert world.dev.batch["p2c"].address == "dev.batch/p2c"
    assert world.sec.package["cryptography"].address == "sec.package/cryptography"
    assert client.subject_list_calls == 1

    # A retired Subject leaves the world it was retired out of.
    assert world.dev.batch.subject_ids == ("p2c",)
    with pytest.raises(AbsentSubject):
        world.dev.batch["retired_batch"]


def test_the_facade_stops_answering_once_the_orientation_moves(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """A name that resolved at one coordinate may name something else at the next."""

    playbill, client = connection
    world = playbill.world()
    assert world.sec.package.cryptography.address == "sec.package/cryptography"

    client.coordinate = _MOVED_COORDINATE
    playbill.refresh()

    for read in (
        lambda: world.sec.package.cryptography,
        lambda: world.sec.vuln.severity,
        lambda: world.sec.package.define("click"),
        lambda: world.kinds,
        lambda: world.predicates,
        lambda: world.kind("sec.package"),
        lambda: world.claim_type(SEVERITY),
        lambda: world.stub(),
    ):
        with pytest.raises(ValueError, match="differs from the active orientation"):
            read()

    # `repr` deliberately stays outside the law: a debugger holding a stale
    # world must still be able to see what it was.
    assert repr(world).startswith(f"<World at {'a' * 40} ")


def test_a_retired_claim_type_leaves_the_world_it_was_read_from(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    playbill, client = connection
    client.retired_severity = True

    world = playbill.world()

    assert SEVERITY not in world.predicates
    with pytest.raises(AttributeError, match="nests no 'severity'"):
        world.sec.vuln.severity


def test_a_subject_reads_its_live_claims_through_the_existing_verbs(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    playbill, client = connection
    world = playbill.world()
    vulnerability = world.sec.vulnerability["cve-2026-69247"]

    under_predicate = vulnerability.affects_package
    assert under_predicate == ()

    claims = vulnerability.claims
    assert len(claims) == 1
    assert claims[0].predicate == SEVERITY
    assert claims[0].value == "high"
    assert claims[0].lifecycle_state == "live"
    assert vulnerability.severity == claims

    subject_search = [row for row in client.searches if row.get("subject") is not None]
    assert subject_search[-1]["kinds"] == ("claim",)
    assert subject_search[-1]["subject"] == {
        "tag": "playbill-semantic-address-v1",
        "artifact_path": "subjects/sec.vulnerability/cve-2026-69247.json",
        "selector": {"scheme": "artifact-v1", "value": ""},
    }
    # One list read plus one Claim read, then the Subject answers from cache.
    assert client.claim_reads == ["CLM-" + "9" * 32]

    assert vulnerability.explain() == {
        "explained": {
            "tag": "playbill-semantic-address-v1",
            "artifact_path": "subjects/sec.vulnerability/cve-2026-69247.json",
            "selector": {"scheme": "artifact-v1", "value": ""},
        }
    }
    with pytest.raises(AttributeError, match="it admits: affects_package, severity"):
        vulnerability.nonsense


def test_a_same_set_definition_returns_refs_usable_in_the_same_set(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """Card 85 closes by construction on the typed path: the ref is the return."""

    playbill, _client = connection
    world = playbill.world()
    draft = playbill.changes(rationale="Name the package this advisory affects.")

    package = draft.subject(world.sec.package.define("click"))
    assert isinstance(package, PendingSubjectRef)
    assert package.address == "sec.package/click"
    assert package.coordinate == playbill.coordinate

    draft.claim(
        subject=world.sec.vulnerability["cve-2026-69247"],
        predicate=world.sec.vuln.affects_package,
        value=package,
        role="observation",
        rationale="The advisory names this package.",
        supported_by=None,
        copied_from=None,
        self_source="affects: click\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    )
    compiled = draft._compiled()

    assert len(compiled.payload.members) == 2
    claim_index = next(
        index
        for index, member in enumerate(compiled.payload.members)
        if member.model_dump(mode="json")["tag"].startswith("playbill-claim-authoring-payload-")
    )
    # The Subject the set defines asserts no reference expectation: it did not
    # exist at the coordinate this ref names, and asserting it there would
    # refuse against the base tree. The Subject and predicate the set only
    # reads still assert theirs.
    assert [item.payload_path for item in compiled.reference_expectations] == [
        f"members[{claim_index}].statement.predicate",
        f"members[{claim_index}].statement.subject",
    ]
    claim = compiled.payload.members[claim_index].model_dump(mode="json")
    assert claim["statement"]["object"] == {
        "kind": "subject",
        "address": {
            "tag": "playbill-semantic-address-v1",
            "artifact_path": "subjects/sec.package/click.json",
            "selector": {"scheme": "artifact-v1", "value": ""},
        },
    }


def test_a_same_set_claim_type_ref_lowers_without_reading_the_unaccepted_type(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    playbill, client = connection
    definition = playbill.claim_type(
        predicate="sec.package.license",
        subject_kinds=("sec.package",),
        object_kind="literal",
        value_schema={"type": "string"},
        object_subject_kinds=(),
        cardinality="one",
        permitted_roles=("observation",),
        referent_sensitivity="identity",
        sources=(),
        admission_policy=_admission_policy(),
        resolution_policy=_resolution_policy(),
        pins=(),
        evidence_freshness=None,
    )
    draft = playbill.changes(rationale="Open the license slot and state one value.")
    predicate = draft.claim_type(definition)

    assert isinstance(predicate, PendingClaimTypeRef)
    assert predicate.address == "sec.package.license"
    assert predicate.object_kind == "literal"

    draft.claim(
        subject="sec.package/cryptography",
        predicate=predicate,
        value="sec.package/bsd",
        role="observation",
        rationale="The package metadata states its license.",
        supported_by=None,
        copied_from=None,
        self_source="license: bsd\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    )
    compiled = draft._compiled()

    claim = next(
        member.model_dump(mode="json")
        for member in compiled.payload.members
        if member.model_dump(mode="json")["tag"].startswith("playbill-claim-authoring-payload-")
    )
    # A literal ClaimType keeps an address-shaped string a literal, and the
    # pending ref answered that without reading a ClaimType that is not
    # accepted yet.
    assert claim["statement"]["object"] == {"kind": "literal", "value": "sec.package/bsd"}
    # Nothing here asserts a reference: the ClaimType is defined by this set and
    # the Subject was spelled as a string.
    assert compiled.reference_expectations == ()
    assert client.claim_type_list_calls == 0


def test_a_value_minted_under_one_claim_type_refuses_under_another(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    playbill, _client = connection
    world = playbill.world()

    with pytest.raises(LiteralValueTypeError) as refused:
        playbill.claim(
            subject=world.sec.vulnerability["cve-2026-69247"],
            predicate=world.dev.batch.landed_at,
            value=world.sec.vuln.severity.high,
            role="observation",
            rationale="Say a severity where a commit belongs.",
            supported_by=None,
            copied_from=None,
            self_source="severity: high\n",
            qualifier=None,
            effective_period=None,
            revises=None,
            dispositions={},
            publish_to=None,
            subject_definition=None,
            claim_type_definition=None,
        )

    assert refused.value.minted_under == SEVERITY
    assert refused.value.passed_to == LANDED_AT
    assert SEVERITY in str(refused.value)
    assert LANDED_AT in str(refused.value)


def _admission_policy() -> Any:
    from cruxible_client.contracts.policies import ClaimAdmissionPolicyV1

    return ClaimAdmissionPolicyV1()


def _resolution_policy() -> Any:
    from cruxible_client.contracts.policies import ClaimResolutionPolicyV1

    return ClaimResolutionPolicyV1(
        cardinality="one",
        eligible_verdicts=("supported",),
        selector="only_contender",
    )


# ---------------------------------------------------------------------------
# Stub generation
# ---------------------------------------------------------------------------


def test_the_stub_is_byte_identical_for_the_same_coordinate_and_moves_with_it(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    playbill, client = connection

    first = playbill.world().stub()
    second = playbill.world().stub()
    assert first == second

    assert first.startswith(f"# {STUB_HEADER_TAG}:")
    assert f"#   git_oid          {'a' * 40}" in first
    # The emitted classes inherit no runtime class carrying `__getattr__`, which
    # is what makes an undeclared name a type error rather than `Any`.
    assert "class _W_sec__vuln__severity(ClaimTypeRef):" in first
    assert "    high: LiteralValue" in first
    assert "    low: LiteralValue" in first
    assert "    cryptography: _S_sec__package" in first
    assert "class _W_sec__vulnerability:" in first
    assert "class _S_sec__vulnerability(SubjectRef):" in first
    assert "    severity: tuple[ClaimView, ...]" in first
    assert "WorldClaimType):" not in first
    assert "KindNamespace):" not in first
    # `cve-2026-69247` is not a Python identifier, so it is reachable only by
    # index and the stub does not pretend otherwise.
    assert "cve-2026-69247" not in first
    assert "# object_kind=literal cardinality=one referent_sensitivity=identity" in first

    client.coordinate = _MOVED_COORDINATE
    playbill.refresh()
    moved = playbill.world().stub()
    assert moved != first
    assert f"#   git_oid          {'b' * 40}" in moved


def test_a_type_checker_reads_the_generated_stub_as_exact_types(
    connection: tuple[Playbill, _WorldClient],
    tmp_path: Path,
) -> None:
    """A stub that types everything as Any would buy the caller nothing."""

    playbill, _client = connection
    project = tmp_path / "stub-project"
    project.mkdir()
    (project / "world.pyi").write_text(playbill.world().stub(), encoding="utf-8")
    (project / "uses_world.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "from cruxible_client.authoring.sdk_types import LiteralValue\n"
        "from world import World\n"
        "\n"
        "\n"
        "def declared(world: World) -> LiteralValue:\n"
        "    return world.sec.vuln.severity.high\n"
        "\n"
        "\n"
        "def mistyped(world: World) -> int:\n"
        "    return world.sec.vuln.severity.high\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--follow-imports=silent",
            "--no-error-summary",
            "uses_world.py",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )

    report = completed.stdout + completed.stderr
    assert "uses_world.py:8" not in report, report
    assert "uses_world.py:12" in report, report
    assert "LiteralValue" in report, report


def test_the_world_is_built_from_orient_and_the_claim_type_list_only(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    playbill, client = connection
    before = len(client.searches)

    world = playbill.world()

    assert isinstance(world, World)
    assert [row["mode"] for row in client.searches[before:]] == ["orient"]
    assert client.claim_type_list_calls == 1
    assert client.subject_list_calls == 0
    assert world.coordinate == AcceptedCoordinate.model_validate(
        _COORDINATE.model_dump(mode="json")
    )
    assert world.unstructured_predicates == ()


# ---------------------------------------------------------------------------
# Paging: a read that under-reports without saying so is the one failure mode
# hard state must not have.
# ---------------------------------------------------------------------------


class _PagedClaimsClient(_WorldClient):
    """A daemon whose subject-filtered list answers in pages, exactly as it does."""

    page_size = 20

    def __init__(
        self,
        *,
        total: int = 55,
        offer_cursor: bool = True,
        stuck_cursor: bool = False,
    ) -> None:
        super().__init__()
        self.total = total
        self.offer_cursor = offer_cursor
        self.stuck_cursor = stuck_cursor
        self.subject_searches = 0
        for index in range(total):
            self.claim_predicates[_paged_identity(index)] = SEVERITY if index % 2 == 0 else AFFECTS

    def search_playbill(self, instance_id: str, **values: Any) -> api.PlaybillSearchResult:
        if values.get("subject") is None:
            return super().search_playbill(instance_id, **values)
        self.searches.append(dict(values))
        self.subject_searches += 1
        cursor = values.get("cursor")
        start = 0 if cursor is None else int(cursor["offset"])
        stop = min(start + self.page_size, self.total)
        rows = [
            {
                "kind": "claim",
                "identity": _paged_identity(index),
                "address": {
                    "artifact_path": f"claims/{_paged_identity(index)}.json",
                    "selector": {"kind": "claim_statement"},
                },
                "status": "accepted",
                "subject": values["subject"],
                "predicate": self.claim_predicates[_paged_identity(index)],
                "title": self.claim_predicates[_paged_identity(index)],
            }
            for index in range(start, stop)
        ]
        truncated = stop < self.total
        return api.PlaybillSearchResult(
            mode=values["mode"],
            coordinate=self.coordinate,
            evaluation_time=str(values["evaluation_time"]),
            rows=rows,
            orientation=None,
            selection_basis_digest="sha256:" + "4" * 64,
            truncated=truncated,
            next_cursor=(
                ({"offset": start} if self.stuck_cursor else {"offset": stop})
                if truncated and self.offer_cursor
                else None
            ),
            result_digest="sha256:" + "5" * 64,
        )


def _paged_identity(index: int) -> str:
    return "CLM-" + f"{index:032d}"


def _paged_connection(tmp_path: Path, client: _PagedClaimsClient) -> Playbill:
    _workspace(tmp_path)
    return Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )


def test_a_subject_reads_every_page_of_its_claims_not_only_the_first(
    tmp_path: Path,
) -> None:
    """Fifty-five live Claims must not answer as fifty, silently."""

    client = _PagedClaimsClient(total=55)
    playbill = _paged_connection(tmp_path, client)
    vulnerability = playbill.world().sec.vulnerability["cve-2026-69247"]

    claims = vulnerability.claims

    assert len(claims) == 55
    assert client.subject_searches == 3
    assert [row.get("cursor") for row in client.searches if row.get("subject") is not None] == [
        None,
        {"offset": 20},
        {"offset": 40},
    ]
    assert len(vulnerability.severity) == 28
    assert len(vulnerability.affects_package) == 27
    # The pages were walked once and every Claim was read once; the predicate
    # views are served from what the Subject already read.
    assert len(client.claim_reads) == 55


def test_a_predicate_view_reads_only_the_claims_that_can_survive_its_filter(
    tmp_path: Path,
) -> None:
    """The served row already names the predicate, so the other reads are waste."""

    client = _PagedClaimsClient(total=55)
    playbill = _paged_connection(tmp_path, client)
    vulnerability = playbill.world().sec.vulnerability["cve-2026-69247"]

    under_severity = vulnerability.severity

    assert len(under_severity) == 28
    assert client.subject_searches == 3
    assert len(client.claim_reads) == 28


def test_a_truncated_page_with_no_cursor_refuses_rather_than_under_report(
    tmp_path: Path,
) -> None:
    """A short answer with no signal is worse than a refusal that names the cap."""

    client = _PagedClaimsClient(total=55, offer_cursor=False)
    playbill = _paged_connection(tmp_path, client)
    vulnerability = playbill.world().sec.vulnerability["cve-2026-69247"]

    with pytest.raises(WorldStructureError) as refused:
        vulnerability.claims

    assert "truncated after 20 rows" in str(refused.value)
    assert "sec.vulnerability/cve-2026-69247" in str(refused.value)


def test_a_cursor_that_does_not_advance_refuses_rather_than_looping(
    tmp_path: Path,
) -> None:
    """The other way a truncated list cannot be continued, and the worse one.

    A daemon handing back the cursor it was given is skew, not corruption: the
    page is right, the walk simply never ends. Without this the client appends
    the same rows forever, which is the one thing a client whose thesis is
    refusing wrong answers must not do.
    """

    client = _PagedClaimsClient(total=55, stuck_cursor=True)
    playbill = _paged_connection(tmp_path, client)
    vulnerability = playbill.world().sec.vulnerability["cve-2026-69247"]

    with pytest.raises(WorldStructureError) as refused:
        vulnerability.claims

    assert "the cursor it was given" in str(refused.value)
    assert "sec.vulnerability/cve-2026-69247" in str(refused.value)
    # Two calls: the first page, then the one that repeated its cursor.
    assert client.subject_searches == 2


def test_a_failure_inside_a_subject_member_is_not_reported_as_a_naming_mistake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claims` is one of this Subject's own names, so reaching __getattr__ for it
    means the member ran and raised -- a real fault, not an unknown predicate.

    Python routes an AttributeError escaping a property back into __getattr__,
    which used to answer "no accepted predicate 'claims' is admitted for Subject
    kind ...". A mis-built contract object deep inside a Claim read and a
    misspelt predicate then looked identical, and only one of them is the
    caller's to fix.
    """

    client = _PagedClaimsClient(total=55)
    playbill = _paged_connection(tmp_path, client)
    world = playbill.world()
    vulnerability = world.sec.vulnerability["cve-2026-69247"]

    def _broken(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise AttributeError("a contract object deep inside the Claim read has no 'envelope'")

    monkeypatch.setattr(World, "_claims_about", _broken)

    with pytest.raises(AttributeError) as refused:
        vulnerability.claims

    message = str(refused.value)
    assert "no accepted predicate" not in message
    assert "failed inside the member itself" in message
    assert "'claims'" in message


# ---------------------------------------------------------------------------
# Freshness, and the connection a read-only caller is allowed to open
# ---------------------------------------------------------------------------


def test_a_world_is_built_at_the_instances_current_coordinate(
    connection: tuple[Playbill, _WorldClient],
) -> None:
    """A world built at a coordinate the instance has left is a stale answer."""

    playbill, client = connection
    assert playbill.world().coordinate.git_oid == "a" * 40

    # Another connection accepts a generation. This one is told nothing.
    client.coordinate = _MOVED_COORDINATE

    world = playbill.world()

    assert world.coordinate.git_oid == "b" * 40
    assert playbill.coordinate.git_oid == "b" * 40


def test_a_read_only_connection_needs_no_workspace_source_catalog(
    tmp_path: Path,
) -> None:
    """Reads touch no working tree, so they must not demand a writer's setup."""

    client = _WorldClient()
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )

    assert not (tmp_path / ".playbill").exists()
    assert playbill.world().kinds == ("dev.batch", "sec.package", "sec.vulnerability")


def test_selecting_a_file_still_refuses_at_the_same_typed_point(
    tmp_path: Path,
) -> None:
    """Deferring the refusal must not remove it, and it must land before the wire."""

    from cruxible_client.authoring.sdk_types import SourceSelectionError

    client = _WorldClient()
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    before = len(client.searches)

    with pytest.raises(SourceSelectionError) as refused:
        playbill.file("notes.txt")

    assert "exactly one .playbill/sources.yaml or sources.yaml" in str(refused.value)
    assert len(client.searches) == before


# ---------------------------------------------------------------------------
# Collisions: the fixed surface wins, and index access reaches the rest
# ---------------------------------------------------------------------------


class _CollidingClient(_WorldClient):
    """A world whose accepted names collide with the facade's own."""

    def list_playbill_claim_types(
        self, _instance_id: str, *, at: Any = None
    ) -> api.PlaybillClaimTypeList:
        self.claim_type_list_calls += 1
        return api.PlaybillClaimTypeList(
            coordinate=self.coordinate,
            claim_types=[
                _claim_type(
                    SEVERITY,
                    literal_schema={
                        "type": "string",
                        "enum": ["cardinality", "high", "low"],
                    },
                ),
                # The predicate leaf collides with `WorldSubject.claims`.
                _claim_type("sec.vuln.claims"),
                # The dotted name is an accepted predicate AND an accepted kind.
                _claim_type(
                    "sec.package",
                    object_kind="subject",
                    literal_schema=None,
                    allowed_object_subject_kinds=("sec.package",),
                ),
            ],
        )


def test_a_predicate_leaf_that_a_member_shadows_is_reachable_by_index(
    tmp_path: Path,
) -> None:
    """`subject.claims` keeps its documented meaning; the predicate has an escape."""

    client = _CollidingClient()
    client.claim_predicates["CLM-" + "9" * 32] = "sec.vuln.claims"
    _workspace(tmp_path)
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    world = playbill.world()
    vulnerability = world.sec.vulnerability["cve-2026-69247"]

    # The member answers what it has always answered: every live Claim.
    assert len(vulnerability.claims) == 1
    # The shadowed predicate is reachable by index, by leaf or in full.
    assert vulnerability["sec.vuln.claims"] == vulnerability.claims
    assert vulnerability["claims"] == vulnerability.claims
    assert vulnerability[world.claim_type("sec.vuln.claims")] == vulnerability.claims
    # Nothing advertises it as an attribute, in `dir()` or in the stub.
    assert "sec.vuln.claims" not in set(world._predicate_leaves("sec.vulnerability").values())
    assert "reach it with subject['sec.vuln.claims']" in world.stub()


def test_an_enum_member_a_structure_field_shadows_is_minted_by_the_call_form(
    tmp_path: Path,
) -> None:
    """A member called `cardinality` must not steal the ClaimType's own structure."""

    client = _CollidingClient()
    _workspace(tmp_path)
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    severity = playbill.world().sec.vuln.severity

    assert severity.cardinality is Cardinality.ONE
    assert severity("cardinality").value == "cardinality"
    assert "cardinality" not in set(dir(severity)) - set(object.__dir__(severity))
    assert "mint it with severity('cardinality')" in playbill.world().stub()


def test_a_kind_a_predicate_shadows_stays_reachable_as_a_kind(
    tmp_path: Path,
) -> None:
    """One dotted name, two accepted things: the predicate wins, the kind escapes."""

    client = _CollidingClient()
    _workspace(tmp_path)
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    world = playbill.world()

    package = world.sec.package
    assert isinstance(package, WorldClaimType)
    assert isinstance(package.as_kind, KindNamespace)
    assert package.as_kind.subject_kind == "sec.package"
    assert package.as_kind.define("click").address == "sec.package/click"
    assert package["cryptography"].address == "sec.package/cryptography"
    assert world.kind("sec.package").subject_ids == ("cryptography",)

    with pytest.raises(AttributeError, match="as_kind"):
        package.define


# ---------------------------------------------------------------------------
# Names attribute access cannot spell
# ---------------------------------------------------------------------------


class _KeywordSegmentClient(_WorldClient):
    """A world whose accepted grammar admits segments Python reserves."""

    def list_playbill_claim_types(
        self, _instance_id: str, *, at: Any = None
    ) -> api.PlaybillClaimTypeList:
        self.claim_type_list_calls += 1
        return api.PlaybillClaimTypeList(
            coordinate=self.coordinate,
            claim_types=[
                _claim_type("sec.vuln.import", allowed_subject_kinds=("dev.class",)),
                _claim_type(SEVERITY),
            ],
        )


def test_a_python_keyword_segment_leaves_the_stub_parseable(
    tmp_path: Path,
) -> None:
    """One unspellable segment used to break the whole file, not just its line."""

    client = _KeywordSegmentClient()
    _workspace(tmp_path)
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    world = playbill.world()

    assert "dev.class" in world.kinds
    assert "sec.vuln.import" in world.predicates

    rendered = world.stub()
    ast.parse(rendered)
    assert "    class: " not in rendered
    assert "    import: " not in rendered
    assert "'class' is not a Python attribute" in rendered
    assert "'import' is not a Python attribute" in rendered

    # The escapes the stub names are the escapes that work.
    assert world.kind("dev.class").subject_kind == "dev.class"
    assert world.claim_type("sec.vuln.import").address == "sec.vuln.import"
    with pytest.raises(WorldStructureError, match="not an accepted Subject kind"):
        world.kind("sec.vuln")


def test_a_type_checker_rejects_every_misspelling_the_stub_names(
    connection: tuple[Playbill, _WorldClient],
    tmp_path: Path,
) -> None:
    """Completion without rejection is the property the stub exists to buy."""

    playbill, _client = connection
    project = tmp_path / "typo-project"
    project.mkdir()
    (project / "world.pyi").write_text(playbill.world().stub(), encoding="utf-8")
    (project / "typos.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "from world import World\n"
        "\n"
        "\n"
        "def misspellings(world: World) -> None:\n"
        "    world.sec.vuln.sevrity.high\n"
        "    world.sec.vuln.severity.hgih\n"
        "    world.sekk.package\n"
        "    world.sec.package.cryptografy\n"
        "    world.totally_absent\n",
        encoding="utf-8",
    )
    (project / "correct.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "from world import World\n"
        "\n"
        "\n"
        "def spelled_right(world: World) -> None:\n"
        "    world.sec.vuln.severity.high\n"
        "    world.sec.package.cryptography\n"
        "    world.sec.vulnerability['cve-2026-69247'].severity\n"
        "    world.sec.package.define('click')\n",
        encoding="utf-8",
    )

    report = _mypy(project, "typos.py")
    for line in range(7, 12):
        assert f"typos.py:{line}: error:" in report, report
    assert report.count("attr-defined") == 5, report

    assert _mypy(project, "correct.py") == "", _mypy(project, "correct.py")


def _mypy(project: Path, target: str) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--follow-imports=silent",
            "--no-error-summary",
            target,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout + completed.stderr


class _UnderscoreSegmentClient(_WorldClient):
    """A world naming both `sec.package` and the single segment `sec__package`."""

    def list_playbill_claim_types(
        self, _instance_id: str, *, at: Any = None
    ) -> api.PlaybillClaimTypeList:
        self.claim_type_list_calls += 1
        return api.PlaybillClaimTypeList(
            coordinate=self.coordinate,
            claim_types=[
                _claim_type(SEVERITY, allowed_subject_kinds=("sec.package", "sec__package")),
            ],
        )


def test_a_segment_spelling_the_separator_does_not_claim_another_names_class(
    tmp_path: Path,
) -> None:
    """`a.b` and `a__b` are both accepted names; one class cannot serve both."""

    client = _UnderscoreSegmentClient()
    _workspace(tmp_path)
    playbill = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    world = playbill.world()

    assert world.kinds == ("sec.package", "sec__package")

    rendered = world.stub()
    module = ast.parse(rendered)
    declared = [node.name for node in module.body if isinstance(node, ast.ClassDef)]
    assert len(declared) == len(set(declared)), declared
    assert "class _W_sec__package:" in rendered
    assert "class _W_sec__package_" in rendered
