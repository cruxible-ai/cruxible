"""Require an approved Cruxible ReviewRequest before merge/release.

The check is intentionally outside Cruxible core runtime code: it treats the
project-state graph as the authority for 0.2 review handoff policy while
remaining runnable from CI or a local release shell.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any, Protocol

REVIEW_ENTITY_TYPE = "ReviewRequest"
WORK_ITEM_ENTITY_TYPE = "WorkItem"
WORK_REVIEW_RELATIONSHIP = "review_request_for_work_item"
DEFAULT_EXPECTED_CONFIG_NAME = "project_state"


class CruxibleReviewClient(Protocol):
    def schema(self, instance_id: str) -> dict[str, Any]:
        """Return the loaded instance config schema."""

    def list(
        self,
        instance_id: str,
        *,
        resource_type: str,
        entity_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        property_filter: dict[str, Any] | None = None,
    ) -> Any:
        """List graph resources."""

    def inspect_entity(
        self,
        instance_id: str,
        entity_type: str,
        entity_id: str,
        *,
        direction: str = "both",
        relationship_type: str | None = None,
        limit: int | None = None,
    ) -> Any:
        """Inspect one entity and its neighbors."""


@dataclass(frozen=True)
class ReviewCandidate:
    review_request_id: str
    status: str | None
    title: str | None
    change_repo: str | None
    change_head: str | None
    work_item_ids: list[str]


@dataclass(frozen=True)
class ReviewHandoffResult:
    ok: bool
    head: str
    repo: str | None
    config_name: str | None
    expected_config_name: str | None
    approved: list[ReviewCandidate]
    candidates: list[ReviewCandidate]
    failures: list[str]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        if isinstance(dumped, dict):
            return dumped
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError(f"Expected mapping-like value, got {type(value).__name__}")


def _result_items(result: Any) -> list[Any]:
    if isinstance(result, dict):
        items = result.get("items", [])
    else:
        items = getattr(result, "items", [])
    if not isinstance(items, list):
        raise TypeError("Cruxible list result did not contain an item list")
    return items


def _config_name(client: CruxibleReviewClient, *, instance_id: str) -> str | None:
    payload = client.schema(instance_id)
    name = payload.get("name")
    return name if isinstance(name, str) else None


def _review_entity_id(item: Any) -> str:
    payload = _mapping(item)
    properties = _mapping(payload.get("properties", {}))
    entity_id = payload.get("entity_id") or properties.get("review_request_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise ValueError("ReviewRequest entity is missing entity_id/review_request_id")
    return entity_id


def _review_properties(item: Any) -> dict[str, Any]:
    payload = _mapping(item)
    return _mapping(payload.get("properties", {}))


def _review_work_item_ids(
    client: CruxibleReviewClient,
    *,
    instance_id: str,
    review_request_id: str,
) -> list[str]:
    result = client.inspect_entity(
        instance_id,
        REVIEW_ENTITY_TYPE,
        review_request_id,
        direction="outgoing",
        relationship_type=WORK_REVIEW_RELATIONSHIP,
        limit=100,
    )
    payload = _mapping(result)
    if payload.get("found") is False:
        return []
    work_item_ids: list[str] = []
    for raw_neighbor in payload.get("neighbors", []):
        neighbor = _mapping(raw_neighbor)
        if neighbor.get("direction") != "outgoing":
            continue
        if neighbor.get("relationship_type") != WORK_REVIEW_RELATIONSHIP:
            continue
        entity = _mapping(neighbor.get("entity", {}))
        if entity.get("entity_type") != WORK_ITEM_ENTITY_TYPE:
            continue
        entity_id = entity.get("entity_id")
        if isinstance(entity_id, str) and entity_id:
            work_item_ids.append(entity_id)
    return sorted(set(work_item_ids))


def _list_review_requests_for_head(
    client: CruxibleReviewClient,
    *,
    instance_id: str,
    head: str,
) -> list[Any]:
    items: list[Any] = []
    limit = 100
    offset = 0
    while True:
        result = client.list(
            instance_id,
            resource_type="entities",
            entity_type=REVIEW_ENTITY_TYPE,
            limit=limit,
            offset=offset,
            property_filter={"change_head": head},
        )
        page = _result_items(result)
        items.extend(page)
        total = getattr(result, "total", None)
        if isinstance(result, dict):
            total = result.get("total", total)
        if isinstance(total, int):
            if offset + len(page) >= total:
                break
        elif len(page) < limit:
            break
        offset += len(page)
        if not page:
            break
    return items


def check_review_handoff(
    client: CruxibleReviewClient,
    *,
    instance_id: str,
    head: str,
    repo: str | None = None,
    expected_config_name: str | None = None,
) -> ReviewHandoffResult:
    """Return whether a commit has an approved, work-linked ReviewRequest."""

    failures: list[str] = []
    config_name: str | None = None
    if expected_config_name:
        config_name = _config_name(client, instance_id=instance_id)
        if config_name != expected_config_name:
            failures.append(
                f"Target instance config is {config_name!r}, "
                f"expected {expected_config_name!r}"
            )
            return ReviewHandoffResult(
                ok=False,
                head=head,
                repo=repo,
                config_name=config_name,
                expected_config_name=expected_config_name,
                approved=[],
                candidates=[],
                failures=failures,
            )

    raw_reviews = _list_review_requests_for_head(client, instance_id=instance_id, head=head)
    candidates: list[ReviewCandidate] = []
    for raw_review in raw_reviews:
        review_id = _review_entity_id(raw_review)
        properties = _review_properties(raw_review)
        change_repo = properties.get("change_repo")
        if repo is not None and change_repo != repo:
            failures.append(
                f"{review_id}: change_repo is {change_repo!r}, expected {repo!r}"
            )
            continue
        work_item_ids = _review_work_item_ids(
            client,
            instance_id=instance_id,
            review_request_id=review_id,
        )
        candidate = ReviewCandidate(
            review_request_id=review_id,
            status=properties.get("status"),
            title=properties.get("title"),
            change_repo=change_repo,
            change_head=properties.get("change_head"),
            work_item_ids=work_item_ids,
        )
        candidates.append(candidate)
        if candidate.status != "approved":
            failures.append(f"{review_id}: status is {candidate.status!r}, expected 'approved'")
            continue
        if not candidate.work_item_ids:
            failures.append(f"{review_id}: missing {WORK_REVIEW_RELATIONSHIP} WorkItem link")

    approved = [
        candidate
        for candidate in candidates
        if candidate.status == "approved" and candidate.work_item_ids
    ]
    if not raw_reviews:
        failures.append(f"No ReviewRequest found with change_head {head!r}")
    elif not approved and not failures:
        failures.append("No approved ReviewRequest linked to a WorkItem was found")
    return ReviewHandoffResult(
        ok=bool(approved),
        head=head,
        repo=repo,
        config_name=config_name,
        expected_config_name=expected_config_name,
        approved=approved,
        candidates=candidates,
        failures=failures,
    )


def _default_head() -> str:
    for env_name in ("CRUXIBLE_CHANGE_HEAD", "GITHUB_SHA"):
        value = os.environ.get(env_name)
        if value:
            return value
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Require an approved Cruxible ReviewRequest for a commit.",
    )
    parser.add_argument("--server-url", default=os.environ.get("CRUXIBLE_SERVER_URL"))
    parser.add_argument("--server-socket", default=os.environ.get("CRUXIBLE_SERVER_SOCKET"))
    parser.add_argument("--instance-id", default=os.environ.get("CRUXIBLE_INSTANCE_ID"))
    parser.add_argument("--token", default=os.environ.get("CRUXIBLE_SERVER_BEARER_TOKEN"))
    parser.add_argument("--head", default=None, help="Commit SHA to check. Defaults to HEAD.")
    parser.add_argument(
        "--expected-config-name",
        default=os.environ.get("CRUXIBLE_EXPECTED_CONFIG_NAME", DEFAULT_EXPECTED_CONFIG_NAME),
        help=(
            "Expected Cruxible config name for this review state. "
            "Set to empty to skip the check."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("CRUXIBLE_CHANGE_REPO") or os.environ.get("GITHUB_REPOSITORY"),
        help="Optional repository full name that ReviewRequest.change_repo must match.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable result.")
    return parser


def _make_client(*, server_url: str | None, server_socket: str | None, token: str | None) -> Any:
    from cruxible_client import CruxibleClient

    if bool(server_url) == bool(server_socket):
        raise ValueError("Set exactly one of CRUXIBLE_SERVER_URL or CRUXIBLE_SERVER_SOCKET")
    if server_socket:
        return CruxibleClient(socket_path=server_socket, token=token)
    return CruxibleClient(base_url=server_url, token=token)


def _normalize_expected_config_name(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _print_human(result: ReviewHandoffResult) -> None:
    subject = result.head
    if result.repo:
        subject = f"{result.repo}@{result.head}"
    if result.ok:
        print(f"Cruxible review handoff approved for {subject}.")
        for candidate in result.approved:
            print(
                f"- {candidate.review_request_id}: approved, "
                f"work_items={', '.join(candidate.work_item_ids)}"
            )
        return

    print(f"Cruxible review handoff check failed for {subject}.", file=sys.stderr)
    for failure in result.failures:
        print(f"- {failure}", file=sys.stderr)
    if result.candidates:
        print("Candidates:", file=sys.stderr)
        for candidate in result.candidates:
            work_items = ", ".join(candidate.work_item_ids) or "<none>"
            print(
                f"- {candidate.review_request_id}: status={candidate.status!r}, "
                f"work_items={work_items}",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.instance_id:
        print("Error: set CRUXIBLE_INSTANCE_ID or pass --instance-id", file=sys.stderr)
        return 2
    try:
        head = args.head or _default_head()
        expected_config_name = _normalize_expected_config_name(args.expected_config_name)
        client = _make_client(
            server_url=args.server_url,
            server_socket=args.server_socket,
            token=args.token,
        )
        try:
            result = check_review_handoff(
                client,
                instance_id=args.instance_id,
                head=head,
                repo=args.repo,
                expected_config_name=expected_config_name,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
