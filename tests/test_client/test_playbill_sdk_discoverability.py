"""The SDK says what to call next from Python itself (roadmap Q20).

An agent in a REPL has ``dir()``, ``help()`` and ``repr()``. Every public SDK
member therefore carries a docstring that names the next call, written against
the read verbs that survive the surface cut (``pb.orient``, ``pb.query``,
``pb.get`` and the greppable floor) and never against the read tools it cut.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cruxible_client
from cruxible_client.authoring import compact_query, sdk, world
from cruxible_client.authoring.sdk_types import (
    CaptureRef,
    ClaimRef,
    ClaimTypeRef,
    PendingSubjectRef,
    ProcedureRef,
    QueryRef,
    SourceRef,
    SubjectRef,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

#: What an SDK user is handed and reads ``help()`` on.
_SDK_CLASSES: tuple[type, ...] = (
    sdk.Playbill,
    sdk.Intent,
    sdk.Proposal,
    sdk.Publication,
    sdk.WriteBatch,
    sdk.ChangeSetDraft,
    sdk.ClaimDraft,
    sdk.ClaimTypeDraft,
    sdk.SubjectDraft,
    sdk.ProcedureDraft,
    sdk.QueryDraft,
    sdk.Prediction,
    sdk.PredictionSettlement,
    sdk.Procedure,
    sdk.ProcedureRun,
    sdk.MeasurementBatch,
    sdk.MeasurementOutcome,
    sdk.NextPage,
    sdk.KnowledgeCard,
    sdk.ClaimView,
    sdk.ProjectionBlocks,
    world.World,
    world.Names,
    world.KindNamespace,
    world.WorldSubject,
    world.WorldClaimType,
    compact_query.CompactQuery,
    compact_query.QueryResult,
)

#: Members the surface cut (cut-a1) rewires onto the new read verbs or
#: removes. They keep the docstrings they have; that branch owns their text,
#: and merging it brings each survivor under the next-call rule.
_SURFACE_CUT_OWNED = frozenset(
    {
        "Playbill.claim_type",
        "Playbill.claim_view",
        "Playbill.claim_views",
        "Playbill.explain",
        "Playbill.list",
        "Playbill.provider_binding",
        "Playbill.query_binding",
        "Playbill.refresh",
        "Playbill.run_query",
        "Playbill.search",
        "Playbill.world",
        "ProjectionBlocks.repin",
        "World.prefetch",
        "World.values",
        "WorldSubject.explain",
    }
)

#: Read tools the surface cut removes; a docstring that names one sends the
#: reader to a call that will not exist.
_CUT_TOOL_CALL = re.compile(
    r"\.(?:search|explain|run_query|claim_values|discover|expand|dereference|claim_view)\("
    r"|\bpb\.list\("
)


def _members(cls: type) -> Iterator[tuple[str, object]]:
    """Public methods and properties a class defines or inherits from SDK code."""

    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue
        attribute = inspect.getattr_static(cls, name)
        if isinstance(attribute, property):
            target: object = attribute.fget
        elif isinstance(attribute, (staticmethod, classmethod)):
            target = attribute.__func__
        elif inspect.isfunction(attribute):
            target = attribute
        else:
            continue
        module = getattr(target, "__module__", "") or ""
        if module.startswith("cruxible_client."):
            yield name, target


def _owner(cls: type, name: str) -> str:
    for base in cls.__mro__:
        if name in vars(base):
            return f"{base.__name__}.{name}"
    return f"{cls.__name__}.{name}"


def test_every_public_sdk_member_names_the_next_call() -> None:
    missing: list[str] = []
    for cls in _SDK_CLASSES:
        for name, target in _members(cls):
            qualified = f"{cls.__name__}.{name}"
            if qualified in _SURFACE_CUT_OWNED or _owner(cls, name) in _SURFACE_CUT_OWNED:
                continue
            doc = inspect.getdoc(target) or ""
            if "Next:" not in doc:
                missing.append(qualified)
    assert not missing, "a public SDK member's docstring names no next call:\n" + "\n".join(missing)


def test_every_sdk_class_has_its_own_docstring() -> None:
    undocumented = [
        cls.__name__
        for cls in _SDK_CLASSES
        # A dataclass without one is given its signature as a docstring.
        if not cls.__doc__ or cls.__doc__.startswith(f"{cls.__name__}(")
    ]
    assert not undocumented, undocumented


def test_no_sdk_docstring_sends_the_reader_to_a_cut_read_tool() -> None:
    offenders: list[str] = []
    for cls in _SDK_CLASSES:
        for text, where in (
            (cls.__doc__ or "", cls.__name__),
            *(
                (inspect.getdoc(target) or "", f"{cls.__name__}.{name}")
                for name, target in _members(cls)
                if f"{cls.__name__}.{name}" not in _SURFACE_CUT_OWNED
                and _owner(cls, name) not in _SURFACE_CUT_OWNED
            ),
        ):
            if _CUT_TOOL_CALL.search(text):
                offenders.append(where)
    assert not offenders, offenders


def test_the_package_dir_lists_every_lazily_loaded_name() -> None:
    listed = dir(cruxible_client)

    assert {"Playbill", "World", "SubjectRef", "CruxibleClient", "__version__"} <= set(listed)
    assert set(cruxible_client.__all__) <= set(listed)
    assert listed == sorted(listed)
    assert "Next:" in (cruxible_client.__dir__.__doc__ or "")


_COORDINATE = AcceptedCoordinate.model_validate(
    {
        "git_oid": "0123456789ab" + "c" * 28,
        "semantic_root": "sha256:" + "1" * 64,
        "generation_root": "sha256:" + "2" * 64,
        "compiler_digest": "sha256:" + "3" * 64,
    }
)


@pytest.mark.parametrize(
    ("ref", "shown"),
    [
        (SubjectRef("sec.package/click", _COORDINATE), "SubjectRef('sec.package/click' @ "),
        (
            PendingSubjectRef("sec.package/new", _COORDINATE),
            "PendingSubjectRef('sec.package/new' @ ",
        ),
        (ClaimTypeRef("sec.vuln.severity", _COORDINATE), "ClaimTypeRef('sec.vuln.severity' @ "),
        (ClaimRef("CLM-" + "a" * 32, _COORDINATE), f"ClaimRef('CLM-{'a' * 32}' @ "),
        (ProcedureRef("triage", _COORDINATE), "ProcedureRef('triage' @ "),
        (QueryRef("open-items", _COORDINATE), "QueryRef('open-items' @ "),
        (SourceRef("corpus.runbook", _COORDINATE), "SourceRef('corpus.runbook' @ "),
    ],
)
def test_a_ref_reprs_as_its_address_and_short_coordinate(ref: object, shown: str) -> None:
    assert repr(ref) == shown + "0123456789ab)"


def test_a_capture_ref_reprs_as_its_handle() -> None:
    ref = CaptureRef(
        capture_digest="sha256:" + "ab" * 32,
        contract_address="capture-contracts/repo.reports.json",
        coordinate=_COORDINATE,
        citation_role="evidence",
    )

    assert ref.handle == "CAP-" + "ab" * 6
    assert repr(ref) == (
        f"CaptureRef(CAP-{'ab' * 6} evidence of 'capture-contracts/repo.reports.json'"
        " @ 0123456789ab)"
    )


class _Client:
    def playbill_whoami(self, _instance_id: str) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(coordinate=_COORDINATE)

    def close(self) -> None:
        return None


def test_playbill_intent_and_proposal_repr_without_io(tmp_path: Path) -> None:
    from cruxible_client import contracts as api

    pb = sdk.Playbill(
        client=_Client(),  # type: ignore[arg-type]
        instance_id="inst_demo",
        workspace=tmp_path,
        access_profile=sdk.AccessProfile(
            profile_id="sdk-default",
            permitted_access_classes=("instance",),
            disclose_restricted_existence=True,
        ),
        clock=lambda: datetime(2026, 9, 30, tzinfo=UTC),
    )
    assert repr(pb) == "Playbill('inst_demo', no coordinate yet, live)"
    pinned = pb.at(_COORDINATE)
    assert repr(pinned) == "Playbill('inst_demo', at 0123456789ab, pinned)"

    intent = sdk.Intent(pinned, None, {"intent_id": "int-1", "intent_revision": 2})
    assert repr(intent) == "Intent('int-1', revision=2, not observed)"
    intent._candidate_status = api.PlaybillCandidateStatus.model_construct(
        state="awaiting_approval", proposal_id="sha256:" + "d" * 64
    )
    assert repr(intent) == (
        f"Intent('int-1', revision=2, awaiting_approval, proposal='sha256:{'d' * 64}')"
    )

    proposal = sdk.Proposal(pinned, "sha256:" + "e" * 64)
    assert repr(proposal) == f"Proposal('sha256:{'e' * 64}')"
