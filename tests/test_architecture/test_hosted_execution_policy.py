"""Every daemon spawn that can run customer code passes the hosted policy gate.

`enforce_customer_code_execution_supported` shipped with zero callers: a daemon
on the shared hosted profile with no isolated execution backend still spawned
Provider children. A runtime probe cannot prove the absence of a bypass, so the
law is asserted statically over the spawn sites themselves.

The detector below deliberately recognises far more than `subprocess.Popen`.
The first version matched only `subprocess.<name>` and bare imported names, so
`asyncio.create_subprocess_exec`, `import subprocess as sp; sp.Popen(...)`,
`os.posix_spawn` and `os.fork` all slid through — and, because presence was the
only test, so did a gate placed AFTER the spawn or inside `if False:`. Every
one of those holes is pinned by the mutation table at the bottom of this file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src/cruxible_core/playbill/provider_local_runtime.py"
SEED = ROOT / "src/cruxible_core/playbill/service/provider_seed.py"
LEASES = ROOT / "src/cruxible_core/playbill/provider_process_leases.py"
FACADE = ROOT / "src/cruxible_core/runtime/playbill_api.py"
ENFORCER = "enforce_customer_code_execution_supported"
PERMISSION_CHECK = "check_permission"

#: Every primitive that can start a process. Matched on ANY receiver, not just
#: `subprocess`, so an aliased import cannot rename its way out of the law.
SPAWN_NAMES = frozenset(
    {
        # subprocess
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        # asyncio
        "create_subprocess_exec",
        "create_subprocess_shell",
        # os
        "system",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    }
)

#: Calls that share a spelling with a spawn primitive but start no process.
#: Declared by (receiver, attribute) so the detector can stay receiver-agnostic
#: -- an aliased `import subprocess as sp` must not rename its way out -- while
#: `platform.system()` (a string) is not read as `os.system()` (a shell).
NON_SPAWN_ATTRIBUTES = frozenset({("platform", "system")})

# Lease probes spawn fixed, core-owned argv (`sysctl -n kern.boottime`,
# `ps -o lstart=`, `ps -Ao ...`) that read the host process table and execute
# nothing from a checkout or a Provider. They are declared exempt by name so a
# NEW spawn in this module cannot inherit the exemption silently.
EXEMPT_LEASE_SPAWNS = {"_current_boot_id", "_process_start_time", "_process_rows"}
# `_git` runs constant Git argv against the adapter checkout; `rev-parse` and
# `status` execute no repository hooks and no checkout code.
EXEMPT_SEED_SPAWNS = {"_git"}


def _is_spawn(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr not in SPAWN_NAMES:
            return False
        receiver = func.value.id if isinstance(func.value, ast.Name) else None
        return (receiver, func.attr) not in NON_SPAWN_ATTRIBUTES
    # `from subprocess import Popen` / `from os import posix_spawn`.
    return isinstance(func, ast.Name) and func.id in SPAWN_NAMES


def _spawning_functions(source: str, filename: str) -> dict[str, ast.FunctionDef]:
    """Map function name -> node for every function that spawns a process.

    Parsing the module means a `subprocess.run` inside a string constant (the
    child fence wrapper's own source) is correctly not a spawn site here.
    """

    tree = ast.parse(source, filename=filename)
    found: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_spawn(inner) for inner in ast.walk(node)):
            found[node.name] = node  # type: ignore[assignment]
    return found


def _module_spawning_functions(path: Path) -> dict[str, ast.FunctionDef]:
    return _spawning_functions(path.read_text(encoding="utf-8"), str(path))


def _body_without_docstring(node: ast.FunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        return body[1:]
    return body


def _enforcer_statement_index(node: ast.FunctionDef) -> int | None:
    """Index of the TOP-LEVEL statement that is the bare enforcer call.

    Top level only: a call nested inside `if`, `try` or a loop is a call that
    some path can skip, and `if False:` is not a gate at all.
    """

    for index, statement in enumerate(_body_without_docstring(node)):
        if not isinstance(statement, ast.Expr):
            continue
        call = statement.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == ENFORCER
        ):
            return index
    return None


def _first_spawn_statement_index(node: ast.FunctionDef) -> int | None:
    for index, statement in enumerate(_body_without_docstring(node)):
        if any(_is_spawn(inner) for inner in ast.walk(statement)):
            return index
    return None


def _gate_dominates_the_spawn(node: ast.FunctionDef) -> bool:
    """Return whether the gate runs before the spawn on every path."""

    gate = _enforcer_statement_index(node)
    if gate is None:
        return False
    spawn = _first_spawn_statement_index(node)
    return spawn is None or gate < spawn


def _gates_before_any_work(node: ast.FunctionDef) -> bool:
    """Return whether the gate precedes everything but the permission check."""

    gate = _enforcer_statement_index(node)
    if gate is None:
        return False
    for statement in _body_without_docstring(node)[:gate]:
        if not isinstance(statement, ast.Expr):
            return False
        call = statement.value
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == PERMISSION_CHECK
        ):
            return False
    return True


def test_every_provider_child_spawn_passes_the_hosted_execution_gate() -> None:
    spawning = _module_spawning_functions(RUNTIME)

    assert set(spawning) == {"_run_child"}, (
        "a new spawn site appeared in provider_local_runtime.py; gate it with "
        f"{ENFORCER} or declare why it runs no customer code: {sorted(spawning)}"
    )
    assert _gate_dominates_the_spawn(spawning["_run_child"])


def test_the_seed_materialization_spawns_are_gated_or_declared_core_owned() -> None:
    spawning = _module_spawning_functions(SEED)

    assert set(spawning) == EXEMPT_SEED_SPAWNS | {"_derive_local_seed_pins"}
    assert _gate_dominates_the_spawn(spawning["_derive_local_seed_pins"])
    for name in EXEMPT_SEED_SPAWNS:
        docstring = ast.get_docstring(spawning[name])
        assert docstring is not None and "core-owned" in docstring


def test_lease_probe_spawns_are_exempt_by_name_and_run_no_customer_code() -> None:
    spawning = _module_spawning_functions(LEASES)

    assert set(spawning) == EXEMPT_LEASE_SPAWNS, (
        "a new spawn site appeared in provider_process_leases.py; gate it with "
        f"{ENFORCER} or add it to EXEMPT_LEASE_SPAWNS with its fixed argv: {sorted(spawning)}"
    )


def test_the_served_run_verbs_gate_before_any_work() -> None:
    """The served refusal must not depend on reaching the spawn chokepoint."""

    tree = ast.parse(FACADE.read_text(encoding="utf-8"), filename=str(FACADE))
    verbs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    for name in ("playbill_procedure_run", "playbill_line_run", "playbill_provider_seed"):
        assert name in verbs, name
        assert _gates_before_any_work(verbs[name]), name  # type: ignore[arg-type]


def test_the_driver_entry_gates_before_it_resolves_a_tenant_secret() -> None:
    """No customer secret is materialized on the way to a refusal."""

    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"), filename=str(RUNTIME))
    invokes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "invoke"
    ]

    assert invokes, "the local provider driver no longer has an `invoke` entry"
    for node in invokes:
        gate = _enforcer_statement_index(node)
        assert gate == 0, "the driver entry must gate before anything else runs"


# ---------------------------------------------------------------------------
# Mutation table: each row is a bypass the detector previously accepted.
# ---------------------------------------------------------------------------

_GATED = f"""
def _run_child(argv):
    {ENFORCER}()
    return subprocess.Popen(argv)
"""

_MUTATIONS: tuple[tuple[str, str], ...] = (
    (
        "asyncio-create-subprocess-exec",
        f"""
async def _run_child(argv):
    {ENFORCER}()
    return await asyncio.create_subprocess_exec(*argv)
""",
    ),
    (
        "aliased-subprocess-import",
        f"""
def _run_child(argv):
    {ENFORCER}()
    return sp.Popen(argv)
""",
    ),
    (
        "bare-from-subprocess-import",
        f"""
def _run_child(argv):
    {ENFORCER}()
    return Popen(argv)
""",
    ),
    (
        "os-posix-spawn",
        f"""
def _run_child(argv):
    {ENFORCER}()
    return os.posix_spawn(argv[0], argv, os.environ)
""",
    ),
    ("os-fork", f"\ndef _run_child(argv):\n    {ENFORCER}()\n    return os.fork()\n"),
    ("os-system", f"\ndef _run_child(argv):\n    {ENFORCER}()\n    return os.system(argv)\n"),
    ("os-execv", f"\ndef _run_child(argv):\n    {ENFORCER}()\n    return os.execv(argv[0], argv)\n"),
)


@pytest.mark.parametrize(("name", "source"), _MUTATIONS, ids=[item[0] for item in _MUTATIONS])
def test_every_spawn_primitive_is_seen_as_a_spawn(name: str, source: str) -> None:
    """A renamed or re-imported spawn primitive is still a spawn."""

    assert set(_spawning_functions(source, f"<mutation:{name}>")) == {"_run_child"}


_ORDERING_MUTATIONS: tuple[tuple[str, str], ...] = (
    (
        "gate-after-the-spawn",
        f"""
def _run_child(argv):
    process = subprocess.Popen(argv)
    {ENFORCER}()
    return process
""",
    ),
    (
        "gate-in-dead-code",
        f"""
def _run_child(argv):
    if False:
        {ENFORCER}()
    return subprocess.Popen(argv)
""",
    ),
    (
        "gate-on-one-branch-only",
        f"""
def _run_child(argv):
    if argv:
        {ENFORCER}()
    return subprocess.Popen(argv)
""",
    ),
    (
        "no-gate-at-all",
        """
def _run_child(argv):
    return subprocess.Popen(argv)
""",
    ),
)


@pytest.mark.parametrize(
    ("name", "source"),
    _ORDERING_MUTATIONS,
    ids=[item[0] for item in _ORDERING_MUTATIONS],
)
def test_a_gate_that_does_not_dominate_the_spawn_is_not_a_gate(name: str, source: str) -> None:
    spawning = _spawning_functions(source, f"<mutation:{name}>")

    assert set(spawning) == {"_run_child"}
    assert not _gate_dominates_the_spawn(spawning["_run_child"])


def test_the_baseline_gate_is_accepted() -> None:
    """The control: the shape the real chokepoint uses must still pass."""

    spawning = _spawning_functions(_GATED, "<mutation:baseline>")

    assert _gate_dominates_the_spawn(spawning["_run_child"])
