"""CLI verb group for declared repo gates.

Doctrine: a GUARD blocks a write INTO state (inbound); a GATE lets the world
act only if state agrees (outbound). Gates are outbound exclusively.

Gates are named, kind-based config declarations (see the ``gates:`` config
element). A gate's ``kind`` names the source adapter that derives candidate
values (``generic`` accepts caller-supplied values; ``git-pre-push`` derives
them from git); a candidate is satisfied when at least one entity of the
declared type carries it in the declared match property and matches the
declared condition. The verb evaluates the declaration; it never hardcodes
ontology, and generality comes from source-adapter kinds plus declarative
conditions.

``gate check`` exit codes are a machine contract:
  0  every candidate satisfied
  1  at least one candidate unsatisfied
  2  cannot evaluate (unknown gate, no gates declared, unknown kind, adapter
     failure, server unreachable, auth failure, malformed input, git failure)
"""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import sys
from typing import Any

import click
import httpx
from pydantic import ValidationError

from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_client.errors import ServerUnreachableError
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    json_option,
)
from cruxible_core.cli.commands._gate_adapters import (
    Candidate,
    GateCheckError,
    GateInvocationContext,
    adapter_for,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.config.schema import CoreConfig, GateSchema
from cruxible_core.service import service_list, service_schema

EXIT_SATISFIED = 0
EXIT_UNSATISFIED = 1
EXIT_CANNOT_EVALUATE = 2


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
    try:
        return {name: GateSchema.model_validate(entry) for name, entry in raw.items()}
    except ValidationError as exc:
        raise GateCheckError(f"gate declarations from the server failed validation: {exc}") from exc


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


def _condition_label(gate: GateSchema) -> str:
    return " AND ".join(f"{prop}={value}" for prop, value in gate.condition.items())


def _candidate_satisfied(gate: GateSchema, value: str) -> bool:
    """Query state: does any live entity match the candidate AND the condition?"""
    where: dict[str, dict[str, Any]] = {gate.match_property: {"eq": value}}
    for prop, condition_value in gate.condition.items():
        where[prop] = {"eq": condition_value}
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


def _read_stdin() -> str:
    """Read the invocation's stdin for a source adapter, failing closed at a TTY."""
    if sys.stdin.isatty():
        raise GateCheckError(
            "stdin is a terminal; gate check requires piped input for this "
            "kind (run from the hook, pipe input lines, or use a supported "
            "candidate argument)"
        )
    return sys.stdin.read()


@click.group("gate")
def gate_group() -> None:
    """Evaluate declared repo gates against state."""


@gate_group.command("list")
@json_option
@handle_errors
def gate_list(output_json: bool) -> None:
    """Show the active instance's declared gates."""
    try:
        gates = _load_gates()
    except GateCheckError as exc:
        # Same clean error surface as gate check, without the exit-code contract.
        raise click.ClickException(str(exc)) from exc
    if output_json:
        _emit_json({name: gate.model_dump(mode="json") for name, gate in sorted(gates.items())})
        return
    if not gates:
        click.echo("No gates declared in the active instance config.")
        return
    for name, gate in sorted(gates.items()):
        scope = f" (branch_pattern {gate.adapter.branch_pattern})" if gate.adapter else ""
        click.echo(
            f"{name} [{gate.kind}]: {gate.entity_type}.{gate.match_property} "
            f"where {_condition_label(gate)}{scope}"
        )
        if gate.description:
            click.echo(f"  {gate.description}")


@gate_group.command("check")
@click.argument("name")
@click.option(
    "--candidate",
    "candidate_values",
    multiple=True,
    help=(
        "Candidate value for a generic gate. Repeatable; when supplied, "
        "stdin is not read. Refused for other gate kinds."
    ),
)
@click.option(
    "--value",
    "values",
    multiple=True,
    hidden=True,
    help=(
        "Diagnostic/test-only override: evaluate these candidate values "
        "directly, bypassing the gate's declared source adapter. Repeatable. "
        "Not a general primitive — real invocations let the gate's kind "
        "source candidates."
    ),
)
def gate_check(
    name: str,
    candidate_values: tuple[str, ...],
    values: tuple[str, ...],
) -> None:
    """Evaluate gate NAME: is every candidate value pinned by satisfying state?

    Resolves the named declaration, invokes its declared kind's source
    adapter for candidate values (e.g. git-pre-push reads the pre-push
    protocol on stdin), and evaluates each candidate against state. Prints
    one verdict line per candidate on stdout
    ('<gate> <value> satisfied|unsatisfied ...'); errors go to stderr.

    \b
    Exit codes:
      0  every candidate satisfied
      1  at least one candidate unsatisfied
      2  cannot evaluate (unknown gate, no gates declared, unknown kind,
         adapter failure, server unreachable, auth failure, malformed
         input, git failure)

    The generic kind reads one candidate per stdin line or accepts repeatable
    --candidate values. The git-pre-push kind evaluates merge commits only:
    squash merges mint new SHAs no review pins, and fast-forward pushes record
    no merge commit.
    """
    try:
        gate = _resolve_gate(name)
        if values:
            # Hidden diagnostic override; see --value help text.
            if candidate_values:
                raise GateCheckError("--candidate and diagnostic --value cannot be combined")
            candidates = [Candidate(value=value) for value in values]
        else:
            if candidate_values and gate.kind != "generic":
                raise GateCheckError(
                    f"--candidate is supported only for gates of kind generic; "
                    f"gate '{name}' is {gate.kind}"
                )
            adapter = adapter_for(gate)
            context = GateInvocationContext(
                read_stdin=_read_stdin,
                explicit_values=candidate_values,
            )
            candidates = adapter.candidates(gate, context)
            if not candidates:
                click.echo(
                    f"gate {name}: no candidates from {gate.kind} input; nothing to evaluate",
                    err=True,
                )
                sys.exit(EXIT_SATISFIED)

        unsatisfied = 0
        for candidate in candidates:
            satisfied = _candidate_satisfied(gate, candidate.value)
            verdict = "satisfied" if satisfied else "unsatisfied"
            suffix = f" ({candidate.context})" if candidate.context else ""
            click.echo(f"{name} {candidate.value} {verdict}{suffix}")
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
            f"not pinned by {gate.entity_type} where {_condition_label(gate)} "
            f"(gate '{name}'). Satisfy the gate in state or bypass deliberately.",
            fg="red",
            err=True,
        )
        sys.exit(EXIT_UNSATISFIED)
    sys.exit(EXIT_SATISFIED)
