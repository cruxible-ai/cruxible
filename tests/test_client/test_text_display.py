"""``print()`` on an SDK result reads as the CLI prints it; ``repr()`` stays the model.

Before, ``print(cx.orient())`` was one ~2,000-character line, ``print(cx.coordinate)``
four digests, and ``print(intent)`` hid a refusal's diagnostics (findings F4, F5,
F9, F15). Each now renders through ``cruxible_client.contracts.text_display``,
which the CLI prints too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from cruxible_client.authoring import sdk
from cruxible_client.contracts.authoring_landing import AuthoringLanded, AuthoringProposed
from cruxible_client.contracts.orient import OrientResult
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.text_display import render_orient, write_outcome_lines
from cruxible_client.contracts.write import WriteOutcome

_OID = "0123456789ab" + "c" * 28
_COORDINATE = AcceptedCoordinate.model_validate(
    {
        "git_oid": _OID,
        "semantic_root": "sha256:" + "1" * 64,
        "generation_root": "sha256:" + "2" * 64,
        "compiler_digest": "sha256:" + "3" * 64,
    }
)


def test_a_coordinate_prints_as_its_short_git_oid_and_reprs_whole() -> None:
    assert str(_COORDINATE) == "0123456789ab"
    assert f"at {_COORDINATE}" == "at 0123456789ab"
    assert "semantic_root" in repr(_COORDINATE)


def test_an_orient_result_prints_the_cli_map() -> None:
    from cruxible_core.cli.commands.playbill import _render_orient

    result = OrientResult.model_validate(
        {
            "instance": "inst_demo",
            "generation": 3,
            "coordinate": _COORDINATE.model_dump(mode="json"),
            "accepted_at": "2026-11-14T00:00:00Z",
            "evaluation_time": "2026-11-14T00:00:00Z",
            "kinds": [{"kind": "shop.part", "subjects": 2, "fields": ["status"]}],
            "queries": [],
            "next": ['cx.orient(kind="shop.part")'],
        }
    )

    text = str(result)

    assert text == render_orient(result.model_dump(mode="json"))
    assert _render_orient is render_orient
    assert text.splitlines() == [
        "Cruxible inst_demo generation=3 at 0123456789ab accepted 2026-11-14T00:00:00Z",
        "Kinds (1):",
        "  shop.part  subjects=2  fields=status",
        "Next:",
        '  cx.orient(kind="shop.part")',
    ]
    assert repr(result).startswith("OrientResult(")


def test_an_authoring_landing_prints_status_and_next() -> None:
    landed = AuthoringLanded(accepted_coordinate=_COORDINATE)
    proposed = AuthoringProposed(
        reason="accept_never", message="kept as a proposal (accept=never)", next="activate it"
    )

    assert str(landed) == "landed: accepted at 0123456789ab"
    assert str(proposed) == "kept as a proposal (accept=never)\nnext: activate it"
    assert "accepted_coordinate" in repr(landed)


def test_a_write_outcome_prints_what_the_cli_prints() -> None:
    outcome = WriteOutcome.model_validate(
        {
            "status": "accepted",
            "coordinate": {"git_oid": _OID[:12], "generation": 4},
            "next": 'cx.get("shop.part/bp-1")',
        }
    )

    assert str(outcome).splitlines() == [
        f"accepted (generation 4, {_OID[:12]})",
        'next: cx.get("shop.part/bp-1")',
    ]
    assert str(outcome) == "\n".join(text for text, _err in write_outcome_lines(outcome))


def test_a_refused_intent_prints_its_diagnostics(tmp_path: Path) -> None:
    from cruxible_client import contracts as api

    class _Client:
        def whoami(self, _instance_id: str) -> object:
            return SimpleNamespace(coordinate=_COORDINATE)

        def close(self) -> None:
            return None

    cx = sdk.Cruxible(
        client=_Client(),  # type: ignore[arg-type]
        instance_id="inst_demo",
        workspace=tmp_path,
        access_profile=sdk.AccessProfile(
            profile_id="sdk-default",
            permitted_access_classes=("instance",),
            disclose_restricted_existence=True,
        ),
        clock=lambda: datetime(2026, 11, 14, tzinfo=UTC),
    ).at(_COORDINATE)
    diagnostic = SimpleNamespace(
        code="cruxible.claim_type.string_unbounded",
        stage="law",
        offending_element="$.members[0]",
        message="the string field has no bound",
        repairs=(SimpleNamespace(model_dump=lambda mode: {"description": "use an enum"}),),
        owner=None,
        disposition=None,
    )
    intent = sdk.Intent(
        cx,
        None,
        {"intent_id": "int-1", "intent_revision": 2},
        preflight=api.AuthoringPreflightResult.model_construct(
            verdict="refused", frontier=SimpleNamespace(diagnostics=(diagnostic,))
        ),
    )

    assert repr(intent) == "Intent('int-1', revision=2, refused, 1 diagnostics)"
    assert str(intent).splitlines() == [
        "intent int-1 revision 2: refused",
        "  cruxible.claim_type.string_unbounded at $.members[0]: the string field has no bound",
        "    repair: use an enum",
    ]


def test_vocabulary_results_print_what_the_cli_prints() -> None:
    """Walkthrough W20: print(cx.vocabulary.plan()) was a pydantic repr."""

    from cruxible_client.contracts.authoring_landing import AuthoringLanded
    from cruxible_client.contracts.vocabulary_file import (
        VocabularyApplyResult,
        VocabularyBaseline,
        VocabularyExportResult,
        VocabularyPlan,
        VocabularyPlanEntry,
        VocabularyPreflight,
    )

    baseline = VocabularyBaseline(
        git_oid=_OID,
        semantic_root=_COORDINATE.semantic_root,
        generation_root=_COORDINATE.generation_root,
        compiler_digest=_COORDINATE.compiler_digest,
        vocabulary_digest="sha256:" + "4" * 64,
    )
    plan = VocabularyPlan(
        path=".cruxible/vocabulary.yaml",
        based_on=baseline,
        entries=(
            VocabularyPlanEntry(
                definition="ClaimType:dev.item.title", action="retire", live_claims=2
            ),
            VocabularyPlanEntry(definition="ClaimType:dev.item.slug", action="not_in_file"),
        ),
        preflight=VocabularyPreflight(verdict="passed"),
    )
    assert str(plan).splitlines() == [
        "Vocabulary plan: .cruxible/vocabulary.yaml (based on 0123456789ab)",
        "Changes:",
        "  retire ClaimType:dev.item.title (2 live Claim(s) retire with it)",
        "Not in file (left as they are): 1",
        "  ClaimType:dev.item.slug",
        "Preflight: passed",
    ]
    applied = VocabularyApplyResult(
        status="landed",
        plan=plan,
        outcome=AuthoringLanded(accepted_coordinate=_COORDINATE),
        next="cruxible vocabulary export",
    )
    assert str(applied).splitlines()[-2:] == [
        "landed: accepted at 0123456789ab",
        "Next: cruxible vocabulary export",
    ]
    exported = VocabularyExportResult(
        path=".cruxible/vocabulary.yaml", status="written", based_on=baseline, kinds=2, fields=5
    )
    assert str(exported) == (
        "Wrote .cruxible/vocabulary.yaml (2 kind(s), 5 field(s)) at 0123456789ab"
    )
    for result in (plan, applied, exported):
        assert repr(result).startswith(type(result).__name__ + "(")


def test_a_query_page_prints_the_cli_table() -> None:
    """Walkthrough W20: print(cx.query(...)) showed no rows."""

    from cruxible_client.authoring.compact_query import QueryResult, render_query_table
    from cruxible_client.contracts.compact_query import QueryResultRecord

    page = QueryResultRecord.model_validate(
        {
            "kind": "dev.item",
            "columns": [{"name": "state", "predicate": "dev.item.state", "type": "string"}],
            "rows": [{"subject": "dev.item/a", "state": "open"}],
            "receipt": {
                "mode": "inline",
                "spec_digest": "sha256:" + "5" * 64,
                "coordinate": _COORDINATE.model_dump(mode="json"),
                "evaluation_time": "2026-11-15T00:00:00Z",
            },
        }
    )
    result = QueryResult(page)
    assert str(result) == render_query_table(page)
    assert "dev.item/a" in str(result) and "open" in str(result)
    assert repr(result).startswith("<QueryResult rows=1")


def _health(state: str, **extra: object) -> dict[str, object]:
    return {"state": state, **extra}


def test_a_next_page_prints_what_cruxible_next_prints() -> None:
    """Walkthrough W20: print(cx.next()) was one 4 kB line of repr."""

    from cruxible_client import contracts as api
    from cruxible_client.contracts.text_display import next_lines

    status = api.NextStatus.model_validate(
        {
            "blocking": False,
            **{
                facet: _health("current")
                for facet in (
                    "instance",
                    "ledger_mirror",
                    "provider_lane",
                    "procedure_catalog",
                    "compiler",
                    "line_dispatch",
                    "consumers",
                    "triggers",
                )
            },
            "floor": _health(
                "stale",
                repair={
                    "operation": "cruxible.floor.export",
                    "target": "inst_demo",
                    "required_change": "replace_installed_floor",
                    "command": "cruxible floor export",
                },
            ),
        }
    )
    item = api.NextItem.model_validate(
        {
            "item_id": "sha256:" + "6" * 64,
            "severity": "repair",
            "reason": "claim_uncovered",
            "subject_identity": "Claim:CLM-" + "a" * 32,
            "related_identities": ["subjects/dev.item/a.json"],
            "detail": {"predicate": "dev.item.state"},
            "repair": {
                "operation": "cruxible.set",
                "target": "dev.item/a",
                "required_change": "restate_on_admitted_evidence",
                "command": 'cx.set("dev.item/a", "dev.item.state", "open", because="<TEXT>")',
                "operands_to_add": ["because"],
            },
        }
    )
    page = sdk.NextPage(
        coordinate=_COORDINATE,
        evaluation_time="2026-11-15T00:00:00Z",
        items=(item,),
        result_digest="sha256:" + "7" * 64,
        observed_domains=("accepted_state",),
        unobserved_domains=("workspace_floor",),
        status=status,
        unobserved=("workspace_floor",),
    )

    assert str(page).splitlines() == [
        "Status: floor stale  next=cruxible floor export",
        f"repair  claim_uncovered  Claim:CLM-{'a' * 32}  (dev.item/a dev.item.state)"
        "  next=cruxible.set",
        '  repair: cx.set("dev.item/a", "dev.item.state", "open", because="<TEXT>")',
        "  fill in: <TEXT> (why, in your words)",
        "Unobserved: workspace_floor",
    ]
    assert str(page) == "\n".join(
        next_lines(status=status, items=page.items, unobserved=page.unobserved)
    )
    assert repr(page).startswith("NextPage(")


def test_a_get_card_prints_what_cruxible_get_prints() -> None:
    """Walkthrough W20: print(cx.get(ref)) was a 1 kB repr."""

    from cruxible_client.contracts.get_reads import GetResult
    from cruxible_client.contracts.get_text import render_get

    result = GetResult.model_validate(
        {
            "ref": "SubjectKind:dev.item",
            "kind": "subject_kind",
            "detail": "summary",
            "coordinate": {"git_oid": _OID[:12], "generation": 3},
            "evaluation_time": "2026-11-15T00:00:00Z",
            "card": {
                "subject_kind": "SubjectKind:dev.item",
                "description": "An item.",
                "lifecycle": "live",
                "rule": "keys",
                "keys": ["dev.item.slug"],
                "uniqueness": "unique",
                "live_subjects": 1,
                "next": ["cruxible get SubjectKind:dev.item --detail history"],
            },
        }
    )
    card = sdk.KnowledgeCard(sdk.RefKind.SUBJECT_KIND, "dev.item", _COORDINATE, result.card, result)

    assert str(card) == render_get(result)
    assert str(card).splitlines() == [
        "subject_kind: SubjectKind:dev.item",
        "description: An item.",
        "lifecycle: live",
        "rule: keys",
        "keys: dev.item.slug",
        "uniqueness: unique",
        "live_subjects: 1",
        "next: cruxible get SubjectKind:dev.item --detail history",
    ]
    assert repr(card).startswith("KnowledgeCard(")


def _review_summary(**updates: object) -> object:
    from cruxible_client.contracts.proposal_summary import ProposalReviewSummary

    payload: dict[str, object] = {
        "proposal_id": "sha256:" + "a" * 64,
        "candidate_digest": "sha256:" + "b" * 64,
        "settlement_base": _COORDINATE.model_dump(mode="json"),
        "rationale": {"value": "Seen in the notes."},
        "counts": {"created": 3},
        "subject_kinds": {"line": {"created": 1}, "security.pair": {"created": 2}},
        "subject_kind_count": 2,
        "required_tier": "governed_write",
        "activation_policy": "snapshot",
        "approval_requirements": [],
        "findings": [],
        "finding_count": 0,
        "groups": [],
        "group_count": 2,
        **updates,
    }
    return ProposalReviewSummary.model_validate(payload)


def test_a_review_summary_prints_changes_and_definitions_not_json() -> None:
    """S6/S17: a proposal reads as the write that made it, plus one block per definition."""

    change = {
        "change": {
            "op": "set",
            "subject": "security.pair/p1",
            "field": "justification",
            "after": "not_in_execute_path",
            "claim": "CLM-" + "e" * 32,
            "verdict": "supported",
        },
        "role": "normative",
        "citations": [
            {
                "capture": "CAP-98cc912e1ba4",
                "origin": "self_source",
                "source": "doc-notes.md",
                "section": ["CVE-1"],
                "text": "## CVE-1\n\nNo caller reaches it.",
            },
            {
                "capture": "CAP-ef62b3dae254",
                "origin": "self_source",
                "source": "Principal:owner",
                "text": "Seen in the notes.",
            },
        ],
    }
    line = {
        "kind": "Line",
        "name": "security.kev-triage",
        "action": "created",
        "detail": {
            "kind": "line",
            "procedure": "security.kev-triage",
            "authority": "settle",
            "bindings": [
                {"slot": "fetch", "provider": "web/f3543a2990f6", "operation": "web.fetch"}
            ],
            "parameters": {"url": {"value": "https://example.test/kev.json"}},
            "acquisition": {"catalog.capture": ["https://example.test"]},
            "budgets": {"max_provider_calls": 1},
            "inbox": ["slot_contested"],
        },
    }
    mandate = {
        "kind": "ProcedureMandate",
        "name": "security.kev-triage-settle",
        "action": "revised",
        "detail": {
            "kind": "procedure-mandate",
            "line": "security.kev-triage",
            "grants": "settle",
            "valid_from": "2026-11-06T00:00:00Z",
            "expires_at": "2027-02-05T00:00:00Z",
            "namespace": ["claims"],
            "scope": [
                {
                    "claim_type": "ClaimType:security.vulnerability.kev_date_added",
                    "changes": {
                        "create": {"basis": ["extracted"]},
                        "retire": {"basis": ["absence"]},
                    },
                    "binds": "subject",
                }
            ],
        },
        "changed": [
            {
                "field": "expires_at",
                "before": {"value": "2027-01-01T00:00:00Z"},
                "after": {"value": "2027-02-05T00:00:00Z"},
            }
        ],
    }
    runbook = {
        "kind": "Runbook",
        "name": "security.maintain",
        "action": "created",
        "detail": {"kind": "runbook", "steps": ["Line:security.kev-triage", "action:x.y"]},
    }
    trigger = {
        "kind": "Trigger",
        "name": "security.daily",
        "action": "created",
        "detail": {
            "kind": "trigger",
            "schedule": "on cron 0 14 * * * (UTC)",
            "target": "Runbook:security.maintain",
        },
    }
    summary = _review_summary(
        changes=[change],
        change_count=2,
        changes_next=1,
        change_counts=[
            {"subject_kind": "security.pair", "field": "justification", "op": "set", "count": 2}
        ],
        definitions=[
            line,
            mandate,
            runbook,
            trigger,
            {"kind": "ClaimType", "name": "x.y", "action": "created"},
            {
                "kind": "CaptureContract",
                "name": "playbill.foreign-source.doc-notes.md",
                "action": "created",
            },
        ],
        definition_count=6,
    )
    text = str(summary)
    expected = [
        "Values (2; showing 1):",
        "  set security.pair/p1 justification: not_in_execute_path"
        f"  [CLM-{'e' * 32}; verdict supported]",
        "    cites CAP-98cc912e1ba4 doc-notes.md#CVE-1 (own words)",
        "      | ## CVE-1",
        "      | No caller reaches it.",
        "    cites CAP-ef62b3dae254 Principal:owner (own words): the Why text above",
        "More values: --changes-from 1",
        "Values by kind and field:",
        "  security.pair  justification  set  2",
        "Definitions (6):",
        "  Line security.kev-triage: created",
        "    binding      fetch -> web/f3543a2990f6#web.fetch",
        "    parameter    url = https://example.test/kev.json",
        "    acquisition  catalog.capture: https://example.test",
        "  ProcedureMandate security.kev-triage-settle: revised",
        "    valid      2026-11-06T00:00:00Z .. 2027-02-05T00:00:00Z",
        "    ClaimType                              create     revise  retire",
        "    security.vulnerability.kev_date_added  extracted  -       absence",
        "    changed expires_at: 2027-01-01T00:00:00Z -> 2027-02-05T00:00:00Z",
        "  Runbook security.maintain: created",
        "    1  Line:security.kev-triage",
        "    2  action:x.y",
        "    schedule  on cron 0 14 * * * (UTC)",
        "    target    Runbook:security.maintain",
        "  ClaimType x.y: created",
        # Reserved stored names read in their public spelling; the JSON keeps them.
        "  CaptureContract cruxible.foreign-source.doc-notes.md: created",
    ]
    lines = text.splitlines()
    for line_text in expected:
        assert line_text in lines, line_text
    assert '{"truncated"' not in text and '"value"' not in text
    assert "playbill." not in text
    assert summary.definitions[-1].name == "playbill.foreign-source.doc-notes.md"  # type: ignore[attr-defined]
    # The SDK's str() and the CLI print one renderer.
    from cruxible_client.contracts.text_display import render_review_summary

    assert text == render_review_summary(summary)  # type: ignore[arg-type]


def test_a_reviewed_proposal_prints_its_summary_without_a_request() -> None:
    from cruxible_client.authoring.approval import ReviewedProposal

    summary = _review_summary()
    reviewed = ReviewedProposal(
        SimpleNamespace(_display_file_paths=dict),  # type: ignore[arg-type]
        "inst",
        b"{}",
        summary.proposal_id,  # type: ignore[attr-defined]
        summary.candidate_digest,  # type: ignore[attr-defined]
        summary.model_dump_json().encode(),  # type: ignore[attr-defined]
    )
    assert str(reviewed) == str(summary)


# -- a file citation reads as the file's path, never only its catalog name ------------

_PATHS = {"doc-introduction-forms.py": "introduction/forms.py"}
_MOVED = {"doc-introduction-forms.py": "intro/forms.py"}


def _file_citation_review() -> object:
    return _review_summary(
        changes=[
            {
                "change": {
                    "op": "set",
                    "subject": "security.pair/p1",
                    "field": "justification",
                    "after": "not_in_execute_path",
                },
                "citations": [
                    {
                        "capture": "CAP-98cc912e1ba4",
                        "origin": "independent",
                        "source": "doc-introduction-forms.py",
                        "section": ["Forms"],
                        "text": "No caller reaches it.",
                    }
                ],
            }
        ],
        change_count=1,
    )


def test_a_review_cites_a_catalogued_file_by_its_current_path() -> None:
    from cruxible_client.contracts.text_display import render_review_summary

    summary = _file_citation_review()

    def cites(**kwargs: object) -> str:
        text = render_review_summary(summary, **kwargs)  # type: ignore[arg-type]
        (line,) = [item for item in text.splitlines() if item.startswith("    cites ")]
        return line

    assert cites(file_paths=_PATHS) == "    cites CAP-98cc912e1ba4 introduction/forms.py#Forms"
    # The file moved and its catalog entry followed: the new path.
    assert cites(file_paths=_MOVED) == "    cites CAP-98cc912e1ba4 intro/forms.py#Forms"
    # Nothing maps the name (no workspace, the entry removed): the stored name.
    assert cites() == "    cites CAP-98cc912e1ba4 doc-introduction-forms.py#Forms"
    assert cites(file_paths={}) == cites()
    # The JSON keeps the stored name.
    assert summary.changes[0].citations[0].source == "doc-introduction-forms.py"  # type: ignore[attr-defined]


def test_a_reviewed_proposal_reads_its_owners_workspace_catalog() -> None:
    from cruxible_client.authoring.approval import ReviewedProposal

    summary = _file_citation_review()
    reviewed = ReviewedProposal(
        SimpleNamespace(_display_file_paths=lambda: _PATHS),  # type: ignore[arg-type]
        "inst",
        b"{}",
        summary.proposal_id,  # type: ignore[attr-defined]
        summary.candidate_digest,  # type: ignore[attr-defined]
        summary.model_dump_json().encode(),  # type: ignore[attr-defined]
    )
    assert "cites CAP-98cc912e1ba4 introduction/forms.py#Forms" in str(reviewed)


def test_a_write_outcome_names_catalogued_and_noted_files_by_path() -> None:
    outcome = WriteOutcome.model_validate(
        {
            "status": "accepted",
            "coordinate": {"git_oid": _OID[:12], "generation": 4},
            "catalogued": [
                {
                    "file": "introduction/forms.py",
                    "name": "doc-introduction-forms.py",
                    "written": True,
                }
            ],
            "changes": [
                {
                    "op": "about",
                    "note": "NOTE-1",
                    "file": "doc-introduction-forms.py",
                    "section": ["Forms"],
                    "about": ["security.pair/p1"],
                }
            ],
        }
    )

    def lines(shown: WriteOutcome) -> list[str]:
        return str(shown).splitlines()[1:]

    # The path first; the catalog name only as a detail.
    assert lines(outcome.with_file_paths(_PATHS)) == [
        "  catalogued introduction/forms.py (as doc-introduction-forms.py)",
        "  note introduction/forms.py#Forms about security.pair/p1  [NOTE-1]",
    ]
    assert lines(outcome.with_file_paths(_MOVED))[1].startswith("  note intro/forms.py#Forms")
    # No catalog was read (a write that names no file by its catalog name): the name.
    assert lines(outcome)[1].startswith("  note doc-introduction-forms.py#Forms")
    # Display only: the fields and the JSON are unchanged.
    shown = outcome.with_file_paths(_PATHS)
    assert shown.model_dump(mode="json") == outcome.model_dump(mode="json")
    assert outcome.file_paths is None and shown.file_paths == _PATHS


def test_a_get_card_names_evidence_and_backlinked_files_by_path() -> None:
    from cruxible_client.contracts.get_reads import (
        Backlink,
        GetBacklinks,
        GetCaptureEvidence,
        GetEvidence,
        GetResult,
    )
    from cruxible_client.contracts.get_text import render_get

    capture = GetCaptureEvidence(
        capture="CAP-0123456789ab",
        contract="CaptureContract:playbill.foreign-source.doc-introduction-forms.py",
        version=1,
        source="doc-introduction-forms.py",
        observed_at=datetime(2026, 11, 15, tzinfo=UTC),
        role="copy",
        origin="independent",
        status="admitted",
    )
    coordinate = {"git_oid": _OID[:12], "generation": 4}
    when = "2026-11-15T00:00:00Z"
    evidence = GetResult.model_validate(
        {
            "ref": "CLM-1",
            "kind": "claim",
            "detail": "evidence",
            "coordinate": coordinate,
            "evaluation_time": when,
            "evidence": GetEvidence(value="x", captures=(capture,), attestations=()).model_dump(
                mode="json"
            ),
        }
    )
    backlinks = GetResult.model_validate(
        {
            "ref": "security.pair/p1",
            "kind": "subject",
            "detail": "backlinks",
            "coordinate": coordinate,
            "evaluation_time": when,
            "backlinks": GetBacklinks(
                target="security.pair/p1",
                links=(
                    Backlink(
                        relation="cites",
                        source_kind="Claim",
                        source="CLM-1",
                        file="doc-introduction-forms.py",
                        section=("Forms",),
                    ),
                ),
            ).model_dump(mode="json"),
        }
    )

    def capture_line(**kwargs: object) -> str:
        text = render_get(evidence, **kwargs)  # type: ignore[arg-type]
        assert text is not None
        return text.splitlines()[1]

    shown = capture_line(file_paths=_PATHS)
    assert "source=introduction/forms.py" in shown
    assert shown.endswith("location in introduction/forms.py: admitted")
    assert "source=intro/forms.py" in capture_line(file_paths=_MOVED)
    assert "source=doc-introduction-forms.py" in capture_line()
    linked = render_get(backlinks, file_paths=_PATHS)
    assert linked == "cites Claim CLM-1 introduction/forms.py#Forms"
    assert render_get(backlinks) == "cites Claim CLM-1 doc-introduction-forms.py#Forms"

    # The SDK card read the catalog with the answer; str() reads nothing.
    card = sdk.KnowledgeCard(
        sdk.RefKind.CLAIM, "CLM-1", _COORDINATE, evidence.evidence, evidence, _PATHS
    )
    assert "source=introduction/forms.py" in str(card)


def test_catalog_file_paths_maps_names_to_current_paths_and_never_raises(
    tmp_path: Path,
) -> None:
    from cruxible_client.authoring.files import catalog_file, remove_catalog_entry
    from cruxible_client.authoring.selectors import catalog_file_paths

    (tmp_path / "introduction").mkdir()
    (tmp_path / "introduction" / "forms.py").write_text("x\n")
    assert dict(catalog_file_paths(None)) == {}
    assert dict(catalog_file_paths(tmp_path)) == {}  # no catalog yet
    catalog_file(tmp_path, path=tmp_path / "introduction" / "forms.py")
    assert dict(catalog_file_paths(tmp_path)) == _PATHS
    # The file moves and the entry follows it under the same name.
    (tmp_path / "intro").mkdir()
    (tmp_path / "introduction" / "forms.py").rename(tmp_path / "intro" / "forms.py")
    catalog_file(
        tmp_path, name="doc-introduction-forms.py", path=tmp_path / "intro/forms.py", replace=True
    )
    assert dict(catalog_file_paths(tmp_path)) == _MOVED
    remove_catalog_entry(tmp_path, name="doc-introduction-forms.py")
    assert dict(catalog_file_paths(tmp_path)) == {}
    # An unreadable catalog maps nothing rather than failing the read.
    (tmp_path / ".cruxible" / "files.yaml").write_text("entries: [")
    assert dict(catalog_file_paths(tmp_path)) == {}


def test_a_get_card_body_prints_reserved_names_in_their_public_spelling() -> None:
    """A card prints one spelling in its body and its next: lines; JSON keeps the stored id."""

    from cruxible_client.contracts.get_reads import GetResult
    from cruxible_client.contracts.get_text import render_get

    stored = "CaptureContract:playbill.coordinator-self-source-v1"
    result = GetResult.model_validate(
        {
            "ref": stored,
            "kind": "capture_contract",
            "detail": "summary",
            "coordinate": {"git_oid": _OID[:12], "generation": 3},
            "evaluation_time": "2026-11-15T00:00:00Z",
            "card": {
                "contract": stored,
                "version": 1,
                "lifecycle": "live",
                "captures": {"latest": "playbill.foreign-source.doc-notes.md"},
                "admitted_by": ["ClaimType:dev.item.status", stored],
                "next": ["cruxible get CaptureContract:cruxible.coordinator-self-source-v1"],
            },
        }
    )

    text = render_get(result)
    assert text is not None
    assert "contract: CaptureContract:cruxible.coordinator-self-source-v1" in text.splitlines()
    assert "captures: latest=cruxible.foreign-source.doc-notes.md" in text.splitlines()
    assert (
        "admitted_by: ClaimType:dev.item.status, "
        "CaptureContract:cruxible.coordinator-self-source-v1"
    ) in text.splitlines()
    assert "playbill." not in text
    assert result.model_dump(mode="json")["card"]["contract"] == stored


def test_a_review_prints_a_multi_line_why_as_lines_not_escapes() -> None:
    """A Procedure's rationale reads as its paragraphs, never a literal \\n."""

    summary = _review_summary(
        rationale={"value": "Settled from the KEV feed.\n\nTwo rows changed.\x1b[2J\n"}
    )
    lines = str(summary).splitlines()
    start = lines.index("Why: Settled from the KEV feed.")
    # Line breaks kept and indented; control characters still escaped.
    assert lines[start + 1 : start + 3] == ["", "  Two rows changed.\\x1b[2J"]
    assert "\\n" not in str(summary)
    cut = _review_summary(rationale={"value": "One.\nTwo", "truncated": True})
    assert "  Two..." in str(cut).splitlines()
    assert "Why: Seen in the notes." in str(_review_summary()).splitlines()
