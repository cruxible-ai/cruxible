"""Gate source adapters: derive candidate values for a declared gate's kind.

INTERNAL SEAM — not a public SDK. Nothing here is exported from any package
``__init__`` or ``__all__``; the interface stays private until a second
adapter kind proves the shape (see wi-shared-condition-grammar). Promotion to
a public extension point is a deliberate later step, not a rewrite.

The seam contract:

- A gate declaration (``GateSchema``) names its candidate SOURCE via
  ``kind``. Each kind maps to exactly one adapter.
- An adapter is given the gate declaration plus a ``GateInvocationContext``
  (how to reach the invocation's inputs, e.g. stdin) and returns the list of
  ``Candidate`` values the gate must evaluate. It never evaluates anything:
  evaluation is always the gate's declared condition against state, owned by
  the CLI verb.
- An empty candidate list means "this invocation has nothing in scope"
  (e.g. no merge commits target the gated branch) and is a legitimate pass.
- Every failure raises ``GateCheckError`` so the caller fails closed
  (exit 2): bad or empty protocol input, git errors, malformed values.
  An adapter must never silence evaluation by swallowing an error.

Adding a source (ci-status, webhook, ...) = add an adapter class here, map
its kind in ``_ADAPTERS``, and add the kind to ``GATE_KINDS`` in the config
schema. No core or CLI change.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from cruxible_core.config.schema import GateSchema

_ZERO_SHA = "0" * 40
# Full 40-hex object names only (the all-zeros sentinel matches too). Enforced
# on every protocol token before it can reach git argv: a token like
# `--max-count=0` must refuse loudly, never silence evaluation.
_SHA_RE = re.compile(r"[0-9a-f]{40}")


class GateCheckError(Exception):
    """A gate check that cannot be evaluated. Always fails closed (exit 2)."""


@dataclass(frozen=True)
class Candidate:
    """One value to evaluate, with optional provenance for verdict lines."""

    value: str
    context: str | None = None


@dataclass(frozen=True)
class GateInvocationContext:
    """How an adapter reaches the invocation's inputs.

    ``read_stdin`` is lazy so adapters that never consume stdin (future
    kinds) do not block on it.
    """

    read_stdin: Callable[[], str]


class GateSourceAdapter(Protocol):
    """One gate kind's candidate source. Internal; see module docstring."""

    kind: str

    def candidates(self, gate: GateSchema, context: GateInvocationContext) -> list[Candidate]:
        """Derive the candidate values this invocation must evaluate.

        Raises ``GateCheckError`` on any failure (fail closed); returns an
        empty list when nothing is legitimately in scope.
        """
        ...  # pragma: no cover - Protocol signature only


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


class GitPrePushAdapter:
    """Candidates from git's pre-push stdin protocol.

    Owns ALL git specifics: protocol parsing, SHA validation, branch-pattern
    filtering (from the gate's adapter config), and merged-parent derivation.

    Protocol lines are ``<local_ref> <local_sha> <remote_ref> <remote_sha>``.
    Only lines whose remote ref matches the adapter config's
    ``branch_pattern`` are gated. Ref deletions (all-zeros local SHA) are
    skipped. For a new remote branch (all-zeros remote SHA) the pushed range
    is ``local_sha --not --remotes``: every merge commit not already
    reachable from a remote-tracking ref. EVERY merged-in parent (``^2`` ..
    ``^N``) of each merge commit is a candidate — an octopus merge passes
    only when all of its merged tips are pinned. v1 gates merge commits only;
    squash and fast-forward merges mint or reuse SHAs no merge commit
    records.
    """

    kind = "git-pre-push"

    def candidates(self, gate: GateSchema, context: GateInvocationContext) -> list[Candidate]:
        if gate.adapter is None:
            # GateSchema requires this for git-pre-push; guard the seam anyway.
            raise GateCheckError(f"gate of kind {self.kind} has no adapter config (branch_pattern)")
        branch_pattern = gate.adapter.branch_pattern
        stdin_text = context.read_stdin()
        if not stdin_text.strip():
            raise GateCheckError(
                "empty pre-push stdin; run from a git pre-push hook (or pipe "
                "protocol lines '<local_ref> <local_sha> <remote_ref> <remote_sha>')"
            )
        candidates: list[Candidate] = []
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
            for token in (local_sha, remote_sha):
                if not _SHA_RE.fullmatch(token):
                    raise GateCheckError(
                        f"invalid commit SHA {token!r} in pre-push stdin line {line!r}; "
                        "expected 40 hex characters or the all-zeros sentinel"
                    )
            if not fnmatch.fnmatchcase(remote_ref, branch_pattern):
                continue
            if local_sha == _ZERO_SHA:
                continue  # ref deletion: nothing new enters the gated branch
            if remote_sha == _ZERO_SHA:
                range_args = [local_sha, "--not", "--remotes"]
            else:
                range_args = [f"{remote_sha}..{local_sha}"]
            # --parents emits '<merge> <parent1> <parent2> [...]' per line; the
            # trailing -- keeps every argument in revision position.
            for merge_line in _git_lines(["rev-list", "--merges", "--parents", *range_args, "--"]):
                merge_sha, *parents = merge_line.split()
                if len(parents) < 2:
                    raise GateCheckError(
                        f"expected a merge commit with two or more parents, got: {merge_line!r}"
                    )
                for parent_number, tip_sha in enumerate(parents[1:], start=2):
                    key = (tip_sha, merge_sha, remote_ref)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        Candidate(
                            value=tip_sha,
                            context=f"merge {merge_sha[:9]}^{parent_number} -> {remote_ref}",
                        )
                    )
        return candidates


_ADAPTERS: dict[str, GateSourceAdapter] = {
    GitPrePushAdapter.kind: GitPrePushAdapter(),
}


def adapter_for(gate: GateSchema) -> GateSourceAdapter:
    """Resolve the adapter for a gate's declared kind, failing closed."""
    adapter = _ADAPTERS.get(gate.kind)
    if adapter is None:
        known = ", ".join(sorted(_ADAPTERS))
        raise GateCheckError(
            f"no source adapter for gate kind '{gate.kind}' "
            f"(this build knows: {known}); refusing to evaluate"
        )
    return adapter
