"""CLI verb group for declared repo gates.

Gates are named config declarations (see the ``gates:`` config element) that
couple an external checkpoint to state: a candidate commit SHA is satisfied
when at least one entity of the declared type pins it in the declared SHA
property and matches the declared predicate. The verb evaluates the
declaration; it never hardcodes ontology.

``gate check`` exit codes are a machine contract:
  0  every candidate satisfied
  1  at least one candidate unsatisfied
  2  cannot evaluate (unknown gate, no gates declared, server unreachable,
     auth failure, malformed input, git failure)

Candidate sources are input adapter FLAGS on ``gate check`` (``--sha``,
``--git-pre-push``), never subcommands: a future CI adapter is another flag
against the same evaluation.
"""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import fnmatch
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import click
import httpx

from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_client.errors import ServerUnreachableError
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.config.schema import CoreConfig, GateSchema
from cruxible_core.service import service_list, service_schema

EXIT_SATISFIED = 0
EXIT_UNSATISFIED = 1
EXIT_CANNOT_EVALUATE = 2

_ZERO_SHA = "0" * 40


class GateCheckError(Exception):
    """A gate check that cannot be evaluated. Always fails closed (exit 2)."""


@dataclass(frozen=True)
class _Candidate:
    """One SHA to evaluate, with optional provenance for verdict lines."""

    sha: str
    context: str | None = None


def _load_gates() -> dict[str, GateSchema]:
    """Read the active instance's gate declarations.

    Server mode reads the /schema wire payload (full config dump, READ_ONLY
    tier); local mode reads the loaded config directly. Both surfaces carry
    the ``gates`` element verbatim, so no dedicated route is needed.
    """
    payload = _dispatch_cli_instance(
        lambda client, instance_id: client.schema(instance_id),
        service_schema,
    )
    if isinstance(payload, CoreConfig):
        return dict(payload.gates)
    raw = payload.get("gates") or {}
    return {name: GateSchema.model_validate(entry) for name, entry in raw.items()}


def _resolve_gate(name: str) -> GateSchema:
    """Resolve a named gate declaration, failing closed on every miss."""
    gates = _load_gates()
    if not gates:
        raise GateCheckError(
            "the active instance config declares no gates element "
            "(or the server predates gates support); a gate that silently "
            "passes when unconfigured is forbidden. Declare gates: in the "
            "instance config, then reload."
        )
    gate = gates.get(name)
    if gate is None:
        declared = ", ".join(sorted(gates))
        raise GateCheckError(f"no gate named '{name}' is declared. Declared gates: {declared}")
    return gate


def _predicate_label(gate: GateSchema) -> str:
    return " AND ".join(f"{prop}={value}" for prop, value in gate.predicate.items())


def _candidate_satisfied(gate: GateSchema, sha: str) -> bool:
    """Query state: does any live entity pin *sha* and match the predicate?"""
    where: dict[str, dict[str, Any]] = {gate.sha_property: {"eq": sha}}
    for prop, value in gate.predicate.items():
        where[prop] = {"eq": value}
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="entities",
            entity_type=gate.entity_type,
            where=where,
            limit=1,
        ),
        lambda instance: service_list(
            instance,
            "entities",
            entity_type=gate.entity_type,
            where=where,
            limit=1,
        ),
    )
    return result.total >= 1


def _git_lines(args: list[str]) -> list[str]:
    """Run a git command, failing closed on any error."""
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise GateCheckError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit code {exc.returncode}"
        raise GateCheckError(f"git {' '.join(args)} failed: {detail}") from exc
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _pre_push_candidates(gate: GateSchema, stdin_text: str) -> list[_Candidate]:
    """Derive candidate SHAs from git's pre-push stdin protocol.

    Protocol lines are ``<local_ref> <local_sha> <remote_ref> <remote_sha>``.
    Only lines whose remote ref matches the gate's ``applies_to`` pattern are
    gated. Ref deletions (all-zeros local SHA) are skipped. For a new remote
    branch (all-zeros remote SHA) the pushed range is ``local_sha --not
    --remotes``: every merge commit not already reachable from a
    remote-tracking ref. Each merge commit's SECOND PARENT — the exact tip
    that was merged — is a candidate. v1 gates merge commits only; squash and
    fast-forward merges mint or reuse SHAs no merge commit records.
    """
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str, str]] = set()
    for line in stdin_text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4:
            raise GateCheckError(
                "malformed pre-push stdin line (expected "
                f"'<local_ref> <local_sha> <remote_ref> <remote_sha>'): {line!r}"
            )
        _local_ref, local_sha, remote_ref, remote_sha = fields
        if not fnmatch.fnmatchcase(remote_ref, gate.applies_to):
            continue
        if local_sha == _ZERO_SHA:
            continue  # ref deletion: nothing new enters the gated branch
        if remote_sha == _ZERO_SHA:
            range_args = [local_sha, "--not", "--remotes"]
        else:
            range_args = [f"{remote_sha}..{local_sha}"]
        for merge_sha in _git_lines(["rev-list", "--merges", *range_args]):
            (tip_sha,) = _git_lines(["rev-parse", f"{merge_sha}^2"]) or (None,)
            if tip_sha is None:
                raise GateCheckError(f"could not resolve second parent of merge {merge_sha}")
            key = (tip_sha, merge_sha, remote_ref)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                _Candidate(
                    sha=tip_sha,
                    context=f"merge {merge_sha[:9]} -> {remote_ref}",
                )
            )
    return candidates


@click.group("gate")
def gate_group() -> None:
    """Evaluate declared repo gates against state."""


@gate_group.command("list")
@json_option
@handle_errors
def gate_list(output_json: bool) -> None:
    """Show the active instance's declared gates."""
    gates = _load_gates()
    if output_json:
        _emit_json({name: gate.model_dump(mode="json") for name, gate in sorted(gates.items())})
        return
    if not gates:
        click.echo("No gates declared in the active instance config.")
        return
    for name, gate in sorted(gates.items()):
        click.echo(
            f"{name}: {gate.entity_type}.{gate.sha_property} "
            f"where {_predicate_label(gate)} (applies_to {gate.applies_to})"
        )
        if gate.description:
            click.echo(f"  {gate.description}")


@gate_group.command("check")
@click.argument("name")
@click.option(
    "--sha",
    "shas",
    multiple=True,
    help="Candidate commit SHA to evaluate. Repeatable.",
)
@click.option(
    "--git-pre-push",
    is_flag=True,
    help=(
        "Input adapter: derive candidates from git's pre-push stdin protocol "
        "(lines of '<local_ref> <local_sha> <remote_ref> <remote_sha>'). "
        "Run from the repository root, as git hooks do. Pushed refs are "
        "filtered to the gate's applies_to pattern; each merge commit's "
        "second parent in the pushed range is a candidate. A new remote "
        "branch (all-zeros remote SHA) evaluates merges not reachable from "
        "any remote-tracking ref; a ref deletion (all-zeros local SHA) is "
        "skipped."
    ),
)
def gate_check(name: str, shas: tuple[str, ...], git_pre_push: bool) -> None:
    """Evaluate gate NAME: is every candidate SHA pinned by satisfying state?

    Prints one verdict line per candidate on stdout
    ('<gate> <sha> satisfied|unsatisfied ...'); errors go to stderr.

    \b
    Exit codes:
      0  every candidate satisfied
      1  at least one candidate unsatisfied
      2  cannot evaluate (unknown gate, no gates declared, server
         unreachable, auth failure, malformed input, git failure)

    v1 evaluates merge commits only: squash merges mint new SHAs no review
    pins, and fast-forward pushes record no merge commit.
    """
    if git_pre_push and shas:
        raise click.UsageError("Provide either --sha or --git-pre-push, not both.")
    if not git_pre_push and not shas:
        raise click.UsageError("Provide candidate SHAs via --sha or --git-pre-push.")

    try:
        gate = _resolve_gate(name)
        if git_pre_push:
            candidates = _pre_push_candidates(gate, sys.stdin.read())
            if not candidates:
                click.echo(
                    f"gate {name}: no merge commits target {gate.applies_to} "
                    "in this push; nothing to evaluate",
                    err=True,
                )
                sys.exit(EXIT_SATISFIED)
        else:
            candidates = [_Candidate(sha=sha) for sha in shas]

        unsatisfied = 0
        for candidate in candidates:
            satisfied = _candidate_satisfied(gate, candidate.sha)
            verdict = "satisfied" if satisfied else "unsatisfied"
            suffix = f" ({candidate.context})" if candidate.context else ""
            click.echo(f"{name} {candidate.sha} {verdict}{suffix}")
            if not satisfied:
                unsatisfied += 1
    except GateCheckError as exc:
        click.secho(f"gate check: cannot evaluate: {exc}", fg="red", err=True)
        sys.exit(EXIT_CANNOT_EVALUATE)
    except ServerUnreachableError as exc:
        click.secho(f"gate check: cannot evaluate: {exc}", fg="red", err=True)
        sys.exit(EXIT_CANNOT_EVALUATE)
    except ClientCoreError as exc:
        click.secho(
            f"gate check: cannot evaluate: {exc.__class__.__name__}: {exc}",
            fg="red",
            err=True,
        )
        sys.exit(EXIT_CANNOT_EVALUATE)
    except httpx.TransportError as exc:
        click.secho(
            f"gate check: cannot evaluate: could not reach Cruxible server: {exc}",
            fg="red",
            err=True,
        )
        sys.exit(EXIT_CANNOT_EVALUATE)

    if unsatisfied:
        click.secho(
            f"gate check: REFUSED - {unsatisfied} of {len(candidates)} candidate(s) "
            f"not pinned by {gate.entity_type} where {_predicate_label(gate)} "
            f"(gate '{name}'). Satisfy the gate in state or bypass deliberately.",
            fg="red",
            err=True,
        )
        sys.exit(EXIT_UNSATISFIED)
    sys.exit(EXIT_SATISFIED)
