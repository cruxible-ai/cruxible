"""A dated many-valued membership: typed periods and the unit law (F6, F7 / D-R30).

Held-vs-incoming comparisons read typed values, so an identical rerun of a
template that binds ``effective_from`` writes nothing (F6). On a many-valued
slot the unit is the object: the same unit with a changed period revises its
Claim, a new unit is added beside it, and precedence still protects a holder
(D-R30).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import ClaimResolutionPolicy
from tests.support.procedures import boundary_contract, procedure_spec
from tests.test_procedures.test_claims_terminal import (
    KIND,
    activate,
    candidate_claims,
    claim_type,
    claims_spec,
    claims_world,
    install,
    run,
)

LISTED_IN = KIND + ".listed_in"
ROWS = boundary_contract(
    "test.membership-row",
    {"catalog": "enum[cisa-kev, vulncheck-kev]", "listed_from": "datetime?"},
)
CVE = "CVE-2026-0001"


def _document(*entries: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "entries": [
            {"id": cve, "catalog": catalog, "dateAdded": added} for cve, catalog, added in entries
        ]
    }


def _membership_spec(*, role: str = "observation", name: str = "test.claims") -> Any:
    base = claims_spec().model_dump(mode="python", by_alias=True)
    write = base["steps"][2]
    write["claims"] = [
        {
            "type": LISTED_IN,
            "value": "$item.fields.catalog",
            "role": role,
            "effective_from": "$item.fields.listed_from",
        }
    ]
    return procedure_spec(base=base, procedure=name, reference_text=True)


def _world(tmp_path: Path, document: dict[str, Any], **install_options: Any) -> tuple[Any, Path]:
    membership = claim_type(
        LISTED_IN,
        "enum[cisa-kev, vulncheck-kev]",
        cardinality="many",
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
    )
    from tests.support.layouts import record_set

    instance, root = claims_world(
        tmp_path,
        document,
        types=(membership,),
        rows=ROWS,
        records={
            "entries": record_set(
                at="/entries/*",
                row=ROWS.ref.spelling,
                key={"cve_id": {"at": "id", "type": f"key_part<{KIND}:cve_id>"}},
                fields={
                    "catalog": {"at": "catalog"},
                    "listed_from": {
                        "at": "dateAdded",
                        "parse": "date",
                        "project": "utc_start_of_day",
                    },
                },
            )
        },
    )
    install(instance, _membership_spec(**install_options.pop("spec", {})), **install_options)
    return instance, root


def _summary(state: Any) -> Any:
    assert state.status == "succeeded", state.terminal
    (found,) = [n.summary for n in state.nodes if n.summary and n.summary.kind == "claims"]
    return found


def _live_memberships(instance: Any, held: Any) -> list[Any]:
    """The live memberships of ``held``'s Subject at the accepted head."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claims = (parse_claim(tree[p], path=p) for p in tree if p.startswith("claims/"))
    return [
        c
        for c in claims
        if c.lifecycle.state == "live"
        and c.statement.predicate == LISTED_IN
        and c.statement.subject == held.statement.subject
    ]


def _rewrite(root: Path, document: dict[str, Any]) -> None:
    (root / "data" / "doc.json").write_text(json.dumps(document))


def test_an_identical_rerun_of_a_dated_membership_writes_nothing(tmp_path: Path) -> None:
    document = _document(
        (CVE, "cisa-kev", "2026-10-01"), ("CVE-2026-0002", "cisa-kev", "2026-10-02")
    )
    instance, root = _world(tmp_path, document)
    first = run(instance, root)
    held = candidate_claims(instance, first)
    assert sorted(c.statement.effective_from.isoformat() for c in held) == [
        "2026-10-01T00:00:00+00:00",
        "2026-10-02T00:00:00+00:00",
    ]
    activate(instance, first)
    again = run(instance, root, minute=1)
    counts = _summary(again)
    assert (counts.written, counts.revised) == (0, 0)
    assert again.terminal_egress[0].proposal_id is None
    assert len(_live_memberships(instance, held[0])) == 1


def test_a_changed_period_on_a_held_unit_revises_its_claim(tmp_path: Path) -> None:
    instance, root = _world(tmp_path, _document((CVE, "cisa-kev", "2026-10-01")))
    first = run(instance, root)
    (old,) = candidate_claims(instance, first)
    activate(instance, first)
    _rewrite(root, _document((CVE, "cisa-kev", "2026-10-05")))
    second = run(instance, root, minute=1)
    counts = _summary(second)
    assert (counts.written, counts.revised) == (0, 1)
    (revised,) = candidate_claims(instance, second)
    assert revised.identity == old.identity
    assert revised.lifecycle.predecessor_digest is not None
    assert revised.statement.effective_from.isoformat() == "2026-10-05T00:00:00+00:00"
    activate(instance, second)
    (live,) = _live_memberships(instance, old)
    assert live.identity == old.identity
    assert live.statement.effective_from.isoformat() == "2026-10-05T00:00:00+00:00"


def test_a_new_unit_is_added_beside_the_held_one(tmp_path: Path) -> None:
    instance, root = _world(tmp_path, _document((CVE, "cisa-kev", "2026-10-01")))
    first = run(instance, root)
    (old,) = candidate_claims(instance, first)
    activate(instance, first)
    _rewrite(root, _document((CVE, "vulncheck-kev", "2026-10-03")))
    second = run(instance, root, minute=1)
    counts = _summary(second)
    assert (counts.written, counts.revised) == (1, 0)
    (added,) = candidate_claims(instance, second)
    assert added.identity != old.identity
    assert added.lifecycle.predecessor_digest is None
    activate(instance, second)
    assert sorted(c.statement.object.value for c in _live_memberships(instance, old)) == [
        "cisa-kev",
        "vulncheck-kev",
    ]


def test_precedence_protects_a_held_unit_from_a_changed_period(tmp_path: Path) -> None:
    instance, root = _world(
        tmp_path, _document((CVE, "cisa-kev", "2026-10-01")), spec={"role": "normative"}
    )
    first = run(instance, root)
    (held,) = candidate_claims(instance, first)
    activate(instance, first)
    install(instance, _membership_spec(name="test.observed"), line_name="test.observed-line")
    _rewrite(root, _document((CVE, "cisa-kev", "2026-10-05")))
    state = run(instance, root, line="test.observed-line", minute=1)
    counts = _summary(state)
    assert (counts.written, counts.revised) == (0, 0)
    assert state.terminal_egress[0].proposal_id is None
    (live,) = _live_memberships(instance, held)
    assert live.identity == held.identity and live.statement.role == "normative"
    assert live.statement.effective_from.isoformat() == "2026-10-01T00:00:00+00:00"


def test_a_readmitted_period_change_revises_the_unit_that_landed_first(tmp_path: Path) -> None:
    """Delivery re-reads the unit law at the head: a retained period change revises."""

    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from cruxible_core.service.proposals.proposals import service_readmit_playbill_proposal

    instance, root = _world(tmp_path, _document((CVE, "cisa-kev", "2026-10-01")))
    install(instance, _membership_spec(name="test.observed"), line_name="test.observed-line")
    first = run(instance, root)
    (landed,) = candidate_claims(instance, first)
    _rewrite(root, _document((CVE, "cisa-kev", "2026-10-05")))
    stale = run(instance, root, line="test.observed-line", minute=1)
    (proposed,) = candidate_claims(instance, stale)
    assert proposed.lifecycle.predecessor_digest is None  # nothing was held at its base
    activate(instance, first)

    readmitted = service_readmit_playbill_proposal(
        instance, proposal_id=stale.terminal_egress[0].proposal_id, actor_id="owner"
    )

    assert readmitted.status == "readmitted" and readmitted.proposal is not None
    successor = readmitted.proposal.proposal.admission.proposal_id
    service_activate_playbill_proposal(instance, proposal_id=successor, activated_by="owner")
    (live,) = _live_memberships(instance, landed)
    assert live.identity == landed.identity
    assert live.lifecycle.predecessor_digest is not None
    assert live.statement.effective_from.isoformat() == "2026-10-05T00:00:00+00:00"


def test_get_shows_a_claims_effective_period_on_its_card_and_history(
    tmp_path: Path, capsys: Any
) -> None:
    """F8: the one ``get`` read model (CLI, MCP and HTTP) carries the period."""

    from datetime import datetime

    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.cli.commands.playbill import _emit_get_text
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.support.operation_world import NOW

    instance, root = _world(tmp_path, _document((CVE, "cisa-kev", "2026-10-01")))
    first = run(instance, root)
    (claim,) = candidate_claims(instance, first)
    activate(instance, first)
    _rewrite(root, _document((CVE, "cisa-kev", "2026-10-05")))
    second = run(instance, root, minute=1)
    activate(instance, second)

    def get(detail: str) -> Any:
        return service_playbill_get(
            instance,
            request=GetRequest(ref=claim.identity.name, detail=detail, evaluation_time=NOW),
            access=BodyAccessContext(principal_id="reader", can_read_body=True),
        )

    summary = get("summary")
    assert summary.card.effective_from == datetime.fromisoformat("2026-10-05T00:00:00+00:00")
    assert summary.card.effective_until is None
    wire = summary.model_dump(mode="json")["card"]
    assert wire["effective_from"] == "2026-10-05T00:00:00Z" and "effective_until" not in wire
    history = get("history").history
    assert [r.effective_from.isoformat() for r in history.revisions] == [
        "2026-10-05T00:00:00+00:00",
        "2026-10-01T00:00:00+00:00",
    ]
    _emit_get_text(summary)
    assert "effective 2026-10-05T00:00:00+00:00.." in capsys.readouterr().out
    _emit_get_text(get("history"))
    shown = capsys.readouterr().out
    assert "effective 2026-10-05T00:00:00+00:00.." in shown
    assert "effective 2026-10-01T00:00:00+00:00.." in shown
