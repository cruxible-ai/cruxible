"""CLI commands for the group subgroup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _groups_from_payload,
    _members_from_payload,
    console,
    json_option,
)
from cruxible_core.cli.formatting import group_detail_table, groups_table, resolutions_table
from cruxible_core.cli.main import handle_errors
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    CandidateSignal,
    GroupResolution,
)
from cruxible_core.service import (
    service_get_group,
    service_group_status,
    service_list_groups,
    service_list_resolutions,
    service_propose_group,
    service_resolve_group,
    service_update_trust_status,
)


@click.group("group")
def group_group() -> None:
    """Manage candidate groups for batch edge review."""


@group_group.command("propose")
@click.option("--relationship", required=True, help="Relationship type for the group.")
@click.option(
    "--members-file",
    type=click.Path(exists=True),
    default=None,
    help="JSON file with member list.",
)
@click.option("--members", "members_json", default=None, help="Inline JSON array of members.")
@click.option("--thesis", default="", help="Human-readable thesis text.")
@click.option("--thesis-facts", default=None, help="JSON object of structured thesis facts.")
@click.option("--analysis-state", default=None, help="JSON object of opaque analysis state.")
@click.option("--integration", multiple=True, help="Integration name used in this proposal.")
@handle_errors
def group_propose(
    relationship: str,
    members_file: str | None,
    members_json: str | None,
    thesis: str,
    thesis_facts: str | None,
    analysis_state: str | None,
    integration: tuple[str, ...],
) -> None:
    """Propose a candidate group of edges for batch review."""
    if members_file and members_json:
        raise click.BadParameter("Provide --members-file or --members, not both.")
    if not members_file and not members_json:
        raise click.BadParameter("Provide --members-file or --members.")

    try:
        if members_file:
            raw_members = json.loads(Path(members_file).read_text())
        else:
            raw_members = json.loads(members_json)  # type: ignore[arg-type]
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"Members must be valid JSON: {exc}") from exc

    if not isinstance(raw_members, list):
        raise click.BadParameter("Members must be a JSON array.")

    try:
        facts = json.loads(thesis_facts) if thesis_facts else None
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--thesis-facts must be valid JSON") from exc

    try:
        state = json.loads(analysis_state) if analysis_state else None
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--analysis-state must be valid JSON") from exc

    client_members = [
        contracts.MemberInput(
            from_type=m["from_type"],
            from_id=m["from_id"],
            to_type=m["to_type"],
            to_id=m["to_id"],
            relationship_type=m["relationship_type"],
            signals=[
                contracts.SignalInput(
                    integration=s["integration"],
                    signal=s["signal"],
                    evidence=s.get("evidence", ""),
                )
                for s in m.get("signals", [])
            ],
            properties=m.get("properties", {}),
        )
        for m in raw_members
    ]
    domain_members = [
        CandidateMember(
            from_type=m["from_type"],
            from_id=m["from_id"],
            to_type=m["to_type"],
            to_id=m["to_id"],
            relationship_type=m["relationship_type"],
            signals=[
                CandidateSignal(
                    integration=s["integration"],
                    signal=s["signal"],
                    evidence=s.get("evidence", ""),
                )
                for s in m.get("signals", [])
            ],
            properties=m.get("properties", {}),
        )
        for m in raw_members
    ]
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.propose_group(
            instance_id,
            relationship_type=relationship,
            members=client_members,
            thesis_text=thesis,
            thesis_facts=facts,
            analysis_state=state,
            integrations_used=list(integration) if integration else None,
        ),
        lambda instance: service_propose_group(
            instance,
            relationship,
            domain_members,
            thesis_text=thesis,
            thesis_facts=facts,
            analysis_state=state,
            integrations_used=list(integration) if integration else None,
        ),
        allow_local=False,
        command_name="group propose",
    )

    if result.group_id is None or result.suppressed:
        click.echo("No reviewable group proposed.")
    else:
        click.echo(f"Group {result.group_id} proposed.")
    click.echo(f"  Status: {result.status}")
    click.echo(f"  Priority: {result.review_priority}")
    click.echo(f"  Members: {result.member_count}")
    click.echo(f"  Signature: {result.signature[:16]}...")
    suppressed_members = getattr(result, "suppressed_members", [])
    if suppressed_members:
        click.echo(f"  Suppressed members: {len(suppressed_members)}")
        for item in suppressed_members:
            click.echo(
                "    "
                f"{item.from_type}:{item.from_id} -[{item.relationship_type}]-> "
                f"{item.to_type}:{item.to_id} ({item.reason})"
            )
    if result.receipt_id:
        click.echo(f"  Receipt: {result.receipt_id}")


@group_group.command("resolve")
@click.option("--group", "group_id", required=True, help="Group ID to resolve.")
@click.option(
    "--action",
    required=True,
    type=click.Choice(["approve", "reject"]),
    help="Resolution action.",
)
@click.option("--rationale", default="", help="Rationale for this resolution.")
@click.option(
    "--source",
    type=click.Choice(["human", "agent"]),
    default="human",
    help="Who resolved (default: human).",
)
@click.option(
    "--expected-pending-version",
    required=True,
    type=int,
    help="Pending version the reviewer saw when deciding.",
)
@json_option
@handle_errors
def group_resolve(
    group_id: str,
    action: str,
    rationale: str,
    source: str,
    expected_pending_version: int,
    output_json: bool,
) -> None:
    """Resolve a candidate group (approve or reject)."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.resolve_group(
            instance_id,
            group_id,
            action=cast(contracts.GroupAction, action),
            rationale=rationale,
            resolved_by=cast(contracts.GroupResolvedBy, source),
            expected_pending_version=expected_pending_version,
        ),
        lambda instance: service_resolve_group(
            instance,
            group_id,
            action,  # type: ignore[arg-type]
            rationale=rationale,
            resolved_by=source,  # type: ignore[arg-type]
            expected_pending_version=expected_pending_version,
        ),
        allow_local=False,
        command_name="group resolve",
    )

    if output_json:
        _emit_json({
            "group_id": result.group_id,
            "action": result.action,
            "edges_created": result.edges_created,
            "edges_skipped": result.edges_skipped,
            "resolution_id": result.resolution_id,
            "receipt_id": result.receipt_id,
        })
        return

    click.echo(f"Group {result.group_id} {result.action}d.")
    if result.action == "approve":
        click.echo(f"  Edges created: {result.edges_created}")
        if result.edges_skipped:
            click.echo(f"  Edges skipped: {result.edges_skipped}")
    if result.resolution_id:
        click.echo(f"  Resolution: {result.resolution_id}")
    if result.receipt_id:
        click.echo(f"  Receipt: {result.receipt_id}")


@group_group.command("trust")
@click.option("--resolution", "resolution_id", required=True, help="Resolution ID.")
@click.option(
    "--status",
    "trust_status",
    required=True,
    type=click.Choice(["watch", "trusted", "invalidated"]),
    help="Trust status to set.",
)
@click.option("--reason", default="", help="Reason for trust status change.")
@handle_errors
def group_trust(resolution_id: str, trust_status: str, reason: str) -> None:
    """Update trust status on a resolution."""
    _dispatch_cli_instance(
        lambda client, instance_id: client.update_trust_status(
            instance_id,
            resolution_id,
            trust_status=cast(contracts.GroupTrustStatus, trust_status),
            reason=reason,
        ),
        lambda instance: service_update_trust_status(
            instance,
            resolution_id,
            trust_status,  # type: ignore[arg-type]
            reason=reason,
        ),
        allow_local=False,
        command_name="group trust",
    )
    click.echo(f"Resolution {resolution_id} trust status set to '{trust_status}'.")


@group_group.command("get")
@click.option("--group", "group_id", required=True, help="Group ID.")
@json_option
@handle_errors
def group_get(group_id: str, output_json: bool) -> None:
    """Get details of a candidate group."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.get_group(instance_id, group_id),
        lambda instance: service_get_group(instance, group_id),
    )
    if isinstance(result, contracts.GetGroupToolResult):
        group = CandidateGroup.model_validate(result.group)
        members = _members_from_payload(result.members)
        resolution = (
            GroupResolution.model_validate(result.resolution)
            if result.resolution is not None
            else None
        )
    else:
        group = result.group
        members = result.members
        resolution = result.resolution
    if output_json:
        payload: dict[str, Any] = {
            "group": group.model_dump(mode="python"),
            "members": [m.model_dump(mode="python") for m in members],
        }
        if resolution is not None:
            payload["resolution"] = resolution.model_dump(mode="python")
        _emit_json(payload)
        return
    console.print(group_detail_table(group, members, resolution))


@group_group.command("list")
@click.option("--relationship", default=None, help="Filter by relationship type.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending_review", "auto_resolved", "applying", "resolved"]),
    help="Filter by status.",
)
@click.option("--limit", default=50, help="Max groups to show.")
@json_option
@handle_errors
def group_list(relationship: str | None, status: str | None, limit: int, output_json: bool) -> None:
    """List candidate groups."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_groups(
            instance_id,
            relationship_type=relationship,
            status=cast(contracts.GroupStatus | None, status),
            limit=limit,
        ),
        lambda instance: service_list_groups(
            instance,
            relationship_type=relationship,
            status=status,
            limit=limit,
        ),
    )
    if isinstance(result, contracts.ListGroupsToolResult):
        groups = _groups_from_payload(result.groups)
        total = result.total
    else:
        groups = result.groups
        total = result.total
    if output_json:
        _emit_json({
            "groups": [g.model_dump(mode="python") for g in groups],
            "total": total,
        })
        return
    console.print(groups_table(groups))
    click.echo(f"{len(groups)} of {total} group(s) shown.")


@group_group.command("resolutions")
@click.option("--relationship", default=None, help="Filter by relationship type.")
@click.option(
    "--action",
    default=None,
    type=click.Choice(["approve", "reject"]),
    help="Filter by action.",
)
@click.option("--limit", default=50, help="Max resolutions to show.")
@json_option
@handle_errors
def group_resolutions(
    relationship: str | None,
    action: str | None,
    limit: int,
    output_json: bool,
) -> None:
    """List group resolutions."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_resolutions(
            instance_id,
            relationship_type=relationship,
            action=cast(contracts.GroupAction | None, action),
            limit=limit,
        ),
        lambda instance: service_list_resolutions(
            instance,
            relationship_type=relationship,
            action=action,
            limit=limit,
        ),
    )
    if isinstance(result, contracts.ListResolutionsToolResult):
        resolutions = [GroupResolution.model_validate(r) for r in result.resolutions]
        total = result.total
    else:
        resolutions = result.resolutions
        total = result.total
    if output_json:
        _emit_json({
            "resolutions": [r.model_dump(mode="python") for r in resolutions],
            "total": total,
        })
        return
    console.print(resolutions_table(resolutions))
    click.echo(f"{len(resolutions)} of {total} resolution(s) shown.")


@group_group.command("status")
@click.option("--group", "group_id", default=None, help="Concrete group ID.")
@click.option("--signature", default=None, help="Signature bucket ID.")
@json_option
@handle_errors
def group_status(group_id: str | None, signature: str | None, output_json: bool) -> None:
    """Show lifecycle status for a signature bucket."""
    if not group_id and not signature:
        raise click.BadParameter("Provide --group or --signature.")
    if group_id and signature:
        raise click.BadParameter("Provide --group or --signature, not both.")

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.get_group_status(
            instance_id,
            group_id=group_id,
            signature=signature,
        ),
        lambda instance: service_group_status(
            instance,
            group_id=group_id,
            signature=signature,
        ),
    )

    if isinstance(result, contracts.GroupBucketStatusToolResult):
        payload = result.model_dump(mode="python")
    else:
        payload = {
            "signature": result.signature,
            "relationship_type": result.relationship_type,
            "thesis_text": result.thesis_text,
            "thesis_facts": result.thesis_facts,
            "latest_trust_status": result.latest_trust_status,
            "accepted_tuple_count": result.accepted_tuple_count,
            "pending_delta_count": result.pending_delta_count,
            "pending_group_id": result.pending_group_id,
            "pending_version": result.pending_version,
            "latest_approved_resolution_id": result.latest_approved_resolution_id,
            "approved_history": [
                {
                    "resolution_id": item.resolution_id,
                    "action": item.action,
                    "trust_status": item.trust_status,
                    "confirmed": item.confirmed,
                    "resolved_at": item.resolved_at,
                    "tuple_count": item.tuple_count,
                }
                for item in result.approved_history
            ],
        }

    if output_json:
        _emit_json(payload)
        return

    click.echo(f"Signature: {payload['signature']}")
    click.echo(f"Relationship: {payload['relationship_type']}")
    click.echo(f"Accepted tuples: {payload['accepted_tuple_count']}")
    click.echo(f"Pending delta: {payload['pending_delta_count']}")
    if payload.get("pending_group_id"):
        click.echo(f"Pending group: {payload['pending_group_id']} (v{payload['pending_version']})")
    if payload.get("latest_trust_status"):
        click.echo(f"Latest trust: {payload['latest_trust_status']}")
    if payload.get("latest_approved_resolution_id"):
        click.echo(f"Latest approved resolution: {payload['latest_approved_resolution_id']}")
