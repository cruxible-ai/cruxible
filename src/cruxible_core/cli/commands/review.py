"""CLI helpers for project-state review requests."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    json_option,
)
from cruxible_core.cli.commands.mutations import (
    _batch_direct_write_result_payload,
    _contract_batch_payload_to_service,
    _emit_direct_write_group_notices,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import handle_errors
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.provenance import SOURCE_REF_BATCH_DIRECT_WRITE
from cruxible_core.service import (
    service_batch_direct_write,
    service_get_entity,
    service_list,
)

_WORK_ITEM_CONTEXT_LINKS: tuple[tuple[str, str, str], ...] = (
    ("work_item_in_release", "review_request_in_release", "ReleaseLine"),
    ("work_item_in_milestone", "review_request_in_milestone", "Milestone"),
)
_LIST_PAGE_SIZE = 500


@dataclass(frozen=True)
class _ReviewSubmitInput:
    work_item_id: str
    review_request_id: str
    title: str
    status: str
    summary: str
    change_repo: str
    change_base: str
    change_head: str
    requested_by: str | None
    reviewer: str | None
    requested_at: str


@dataclass(frozen=True)
class _ReviewSubmitResult:
    batch_result: Any
    context_relationships: list[contracts.BatchRelationshipInput]


def _git_output(args: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            check=True,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _normalize_repo_url(remote_url: str) -> str:
    value = remote_url.strip().rstrip("/")
    github_prefixes = (
        "git@github.com:",
        "ssh://git@github.com/",
        "https://github.com/",
        "http://github.com/",
    )
    for prefix in github_prefixes:
        if value.startswith(prefix):
            repo = value.removeprefix(prefix)
            if repo.endswith(".git"):
                repo = repo[:-4]
            parts = repo.strip("/").split("/")
            if len(parts) >= 2:
                return "/".join(parts[:2])
    return value


def _infer_change_repo() -> str | None:
    remote = _git_output(("config", "--get", "remote.origin.url"))
    return _normalize_repo_url(remote) if remote else None


def _infer_change_head() -> str | None:
    return _git_output(("rev-parse", "HEAD"))


def _infer_change_base() -> str | None:
    for ref in ("@{upstream}", "origin/main", "origin/master", "main", "master"):
        base = _git_output(("merge-base", "HEAD", ref))
        if base:
            return base
    return None


def _resolve_change_value(
    *,
    option_value: str | None,
    env_name: str,
    option_name: str,
    label: str,
    infer: Callable[[], str | None],
) -> str:
    for value in (option_value, os.environ.get(env_name), infer()):
        if value is not None and value.strip():
            return value.strip()
    raise click.UsageError(f"Could not infer {label}; pass {option_name} or set {env_name}.")


def _id_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return segment or "review"


def _default_review_request_id(work_item_id: str, change_head: str) -> str:
    return f"rr-{_id_segment(work_item_id)}-{_id_segment(change_head[:12])}"


def _resolve_summary(
    *,
    summary: str | None,
    summary_file: Path | None,
    work_item_id: str,
    change_repo: str,
    change_base: str,
    change_head: str,
) -> str:
    if summary is not None and summary_file is not None:
        raise click.UsageError("Use either --summary or --summary-file, not both.")
    if summary_file is not None:
        try:
            return summary_file.read_text()
        except OSError as exc:
            raise click.BadParameter(f"Could not read --summary-file: {exc}") from exc
    if summary is not None:
        return summary
    return (
        f"Review request for WorkItem {work_item_id}.\n\n"
        f"Change repo: {change_repo}\n"
        f"Change base: {change_base}\n"
        f"Change head: {change_head}"
    )


def _build_review_payload(
    review: _ReviewSubmitInput,
    context_relationships: Sequence[contracts.BatchRelationshipInput],
) -> contracts.BatchDirectWritePayload:
    properties: dict[str, Any] = {
        "review_request_id": review.review_request_id,
        "title": review.title,
        "status": review.status,
        "summary": review.summary,
        "change_repo": review.change_repo,
        "change_base": review.change_base,
        "change_head": review.change_head,
        "requested_at": review.requested_at,
    }
    if review.requested_by is not None:
        properties["requested_by"] = review.requested_by
    if review.reviewer is not None:
        properties["reviewer"] = review.reviewer

    return contracts.BatchDirectWritePayload(
        entities=[
            contracts.EntityInput(
                entity_type="ReviewRequest",
                entity_id=review.review_request_id,
                properties=properties,
            )
        ],
        relationships=[
            contracts.BatchRelationshipInput(
                from_type="ReviewRequest",
                from_id=review.review_request_id,
                relationship_type="review_request_for_work_item",
                to_type="WorkItem",
                to_id=review.work_item_id,
            ),
            *context_relationships,
        ],
        shared_evidence={},
    )


def _context_relationship_payloads(
    relationships: Iterable[contracts.BatchRelationshipInput],
) -> list[dict[str, str]]:
    return [
        {
            "relationship_type": edge.relationship_type,
            "from_type": edge.from_type,
            "from_id": edge.from_id,
            "to_type": edge.to_type,
            "to_id": edge.to_id,
        }
        for edge in relationships
    ]


def _infer_context_relationships(
    review: _ReviewSubmitInput,
    *,
    list_edges: Callable[[str], list[dict[str, Any]]],
) -> list[contracts.BatchRelationshipInput]:
    relationships: list[contracts.BatchRelationshipInput] = []
    seen: set[tuple[str, str, str]] = set()
    milestone_ids: set[str] = set()

    def add_context_relationship(
        *,
        review_relationship: str,
        target_type: str,
        target_id: str,
    ) -> None:
        if not target_id:
            return
        key = (review_relationship, target_type, target_id)
        if key in seen:
            return
        seen.add(key)
        relationships.append(
            contracts.BatchRelationshipInput(
                from_type="ReviewRequest",
                from_id=review.review_request_id,
                relationship_type=review_relationship,
                to_type=target_type,
                to_id=target_id,
            )
        )

    for work_relationship, review_relationship, target_type in _WORK_ITEM_CONTEXT_LINKS:
        for edge in list_edges(work_relationship):
            if (
                edge.get("from_type") != "WorkItem"
                or edge.get("from_id") != review.work_item_id
                or edge.get("to_type") != target_type
            ):
                continue
            to_id = str(edge.get("to_id") or "")
            if target_type == "Milestone" and to_id:
                milestone_ids.add(to_id)
            add_context_relationship(
                review_relationship=review_relationship,
                target_type=target_type,
                target_id=to_id,
            )

    for edge in list_edges("milestone_in_release"):
        if (
            edge.get("from_type") != "Milestone"
            or edge.get("from_id") not in milestone_ids
            or edge.get("to_type") != "ReleaseLine"
        ):
            continue
        add_context_relationship(
            review_relationship="review_request_in_release",
            target_type="ReleaseLine",
            target_id=str(edge.get("to_id") or ""),
        )
    return relationships


def _list_all_client_edges(
    client: Any,
    instance_id: str,
    relationship_type: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = client.list(
            instance_id,
            resource_type="edges",
            relationship_type=relationship_type,
            limit=_LIST_PAGE_SIZE,
            offset=offset,
        )
        page = list(result.items)
        items.extend(page)
        offset += len(page)
        if not page or offset >= result.total:
            return items


def _list_all_local_edges(
    instance: CruxibleInstance,
    relationship_type: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = service_list(
            instance,
            "edges",
            relationship_type=relationship_type,
            limit=_LIST_PAGE_SIZE,
            offset=offset,
        )
        page = list(result.items)
        items.extend(page)
        offset += len(page)
        if not page or offset >= result.total:
            return items


def _submit_review_remote(
    client: Any,
    instance_id: str,
    review: _ReviewSubmitInput,
    *,
    dry_run: bool,
) -> _ReviewSubmitResult:
    work_item = client.get_entity(instance_id, "WorkItem", review.work_item_id)
    if not work_item.found:
        raise DataValidationError(f"WorkItem {review.work_item_id} not found")

    existing = client.get_entity(instance_id, "ReviewRequest", review.review_request_id)
    if existing.found:
        raise DataValidationError(f"ReviewRequest {review.review_request_id} already exists")

    context_relationships = _infer_context_relationships(
        review,
        list_edges=lambda relationship_type: _list_all_client_edges(
            client,
            instance_id,
            relationship_type,
        ),
    )
    result = client.batch_direct_write(
        instance_id,
        _build_review_payload(review, context_relationships),
        dry_run=dry_run,
    )
    return _ReviewSubmitResult(result, context_relationships)


def _submit_review_local(
    instance: CruxibleInstance,
    review: _ReviewSubmitInput,
    *,
    dry_run: bool,
) -> _ReviewSubmitResult:
    if service_get_entity(instance, "WorkItem", review.work_item_id) is None:
        raise DataValidationError(f"WorkItem {review.work_item_id} not found")
    if service_get_entity(instance, "ReviewRequest", review.review_request_id) is not None:
        raise DataValidationError(f"ReviewRequest {review.review_request_id} already exists")

    context_relationships = _infer_context_relationships(
        review,
        list_edges=lambda relationship_type: _list_all_local_edges(instance, relationship_type),
    )
    payload = _build_review_payload(review, context_relationships)
    result = service_batch_direct_write(
        instance,
        _contract_batch_payload_to_service(payload),
        dry_run=dry_run,
        source="cli_review_submit",
        source_ref=SOURCE_REF_BATCH_DIRECT_WRITE,
    )
    return _ReviewSubmitResult(result, context_relationships)


def _review_submit_json_payload(
    review: _ReviewSubmitInput,
    submit_result: _ReviewSubmitResult,
) -> dict[str, Any]:
    batch_payload = _batch_direct_write_result_payload(submit_result.batch_result)
    return {
        "review_request_id": review.review_request_id,
        "work_item_id": review.work_item_id,
        "change_repo": review.change_repo,
        "change_base": review.change_base,
        "change_head": review.change_head,
        "receipt_id": batch_payload["receipt_id"],
        "dry_run": batch_payload["dry_run"],
        "valid": batch_payload["valid"],
        "context_relationships": _context_relationship_payloads(
            submit_result.context_relationships
        ),
        "batch_direct_write": batch_payload,
    }


def _emit_review_submit_result(
    review: _ReviewSubmitInput,
    submit_result: _ReviewSubmitResult,
    *,
    output_json: bool,
) -> None:
    payload = _review_submit_json_payload(review, submit_result)
    if output_json:
        _emit_json(payload)
        return

    action = "validated" if payload["dry_run"] else "submitted"
    if payload["valid"]:
        click.echo(f"ReviewRequest {review.review_request_id} {action}.")
    else:
        click.echo(f"ReviewRequest {review.review_request_id} {action} with errors.")
    click.echo(f"  WorkItem: {review.work_item_id}")
    click.echo(
        f"  Change: {review.change_repo} {review.change_base[:12]}..{review.change_head[:12]}"
    )
    click.echo(f"  Context links: {len(submit_result.context_relationships)} release/milestone")
    for warning in payload["batch_direct_write"]["validation_warnings"]:
        click.secho(f"  Warning: {warning}", fg="yellow")
    for error in payload["batch_direct_write"]["validation_errors"]:
        click.secho(f"  Error: {error}", fg="red")
    if payload["receipt_id"]:
        click.echo(f"  Receipt: {payload['receipt_id']}")
    _emit_direct_write_group_notices(payload["batch_direct_write"], prefix="  ")


@click.group("review")
def review_group() -> None:
    """Review request helpers."""


@review_group.command("submit")
@click.argument("work_item_id")
@click.option("--review-request-id", default=None, help="ReviewRequest id to create.")
@click.option("--title", default=None, help="ReviewRequest title.")
@click.option(
    "--status",
    type=click.Choice(["requested", "in_review"]),
    default="requested",
    show_default=True,
    help="Initial ReviewRequest status.",
)
@click.option("--summary", default=None, help="ReviewRequest summary text.")
@click.option(
    "--summary-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read ReviewRequest summary from a file.",
)
@click.option("--change-repo", default=None, help="Repository under review.")
@click.option("--change-base", default=None, help="Base commit under review.")
@click.option("--change-head", default=None, help="Exact reviewed commit SHA.")
@click.option("--requested-by", default=None, help="Requester identity.")
@click.option("--reviewer", default=None, help="Requested reviewer identity.")
@click.option(
    "--requested-at",
    default=None,
    help="Request date in YYYY-MM-DD form. Defaults to today.",
)
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@json_option
@handle_errors
def review_submit_cmd(
    work_item_id: str,
    review_request_id: str | None,
    title: str | None,
    status: str,
    summary: str | None,
    summary_file: Path | None,
    change_repo: str | None,
    change_base: str | None,
    change_head: str | None,
    requested_by: str | None,
    reviewer: str | None,
    requested_at: str | None,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Create a project-state ReviewRequest for a completed WorkItem change."""
    resolved_change_repo = _resolve_change_value(
        option_value=change_repo,
        env_name="CRUXIBLE_CHANGE_REPO",
        option_name="--change-repo",
        label="change repo",
        infer=_infer_change_repo,
    )
    resolved_change_base = _resolve_change_value(
        option_value=change_base,
        env_name="CRUXIBLE_CHANGE_BASE",
        option_name="--change-base",
        label="change base",
        infer=_infer_change_base,
    )
    resolved_change_head = _resolve_change_value(
        option_value=change_head,
        env_name="CRUXIBLE_CHANGE_HEAD",
        option_name="--change-head",
        label="change head",
        infer=_infer_change_head,
    )
    resolved_review_request_id = (
        review_request_id
        if review_request_id is not None
        else _default_review_request_id(work_item_id, resolved_change_head)
    )
    resolved_title = title or (f"Review {work_item_id} at {resolved_change_head[:12]}")
    review = _ReviewSubmitInput(
        work_item_id=work_item_id,
        review_request_id=resolved_review_request_id,
        title=resolved_title,
        status=status,
        summary=_resolve_summary(
            summary=summary,
            summary_file=summary_file,
            work_item_id=work_item_id,
            change_repo=resolved_change_repo,
            change_base=resolved_change_base,
            change_head=resolved_change_head,
        ),
        change_repo=resolved_change_repo,
        change_base=resolved_change_base,
        change_head=resolved_change_head,
        requested_by=requested_by,
        reviewer=reviewer,
        requested_at=requested_at or date.today().isoformat(),
    )

    submit_result = _dispatch_cli_instance(
        lambda client, instance_id: _submit_review_remote(
            client,
            instance_id,
            review,
            dry_run=dry_run,
        ),
        lambda instance: _submit_review_local(instance, review, dry_run=dry_run),
        command_name="review submit",
    )
    _emit_review_submit_result(review, submit_result, output_json=output_json)
