"""Every daemon spawn that can run customer code passes the hosted policy gate.

`enforce_customer_code_execution_supported` shipped with zero callers: a daemon
on the shared hosted profile with no isolated execution backend still spawned
Provider children. A runtime probe cannot prove the absence of a bypass, so the
law is asserted statically over the spawn sites themselves.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src/cruxible_core/playbill/provider_local_runtime.py"
SEED = ROOT / "src/cruxible_core/playbill/service/provider_seed.py"
LEASES = ROOT / "src/cruxible_core/playbill/provider_process_leases.py"
ENFORCER = "enforce_customer_code_execution_supported"

# Lease probes spawn fixed, core-owned argv (`sysctl -n kern.boottime`,
# `ps -o lstart=`, `ps -Ao ...`) that read the host process table and execute
# nothing from a checkout or a Provider. They are declared exempt by name so a
# NEW spawn in this module cannot inherit the exemption silently.
EXEMPT_LEASE_SPAWNS = {"_current_boot_id", "_process_start_time", "_process_rows"}
# `_git` runs constant Git argv against the adapter checkout; `rev-parse` and
# `status` execute no repository hooks and no checkout code.
EXEMPT_SEED_SPAWNS = {"_git"}


def _spawning_functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Map function name -> node for every function that spawns a process.

    Parsing the module means a `subprocess.run` inside a string constant (the
    child fence wrapper's own source) is correctly not a spawn site here.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in {"Popen", "run"}
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "subprocess"
            ):
                found[node.name] = node
                break
    return found


def _calls_enforcer(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == ENFORCER
        for inner in ast.walk(node)
    )


def test_every_provider_child_spawn_passes_the_hosted_execution_gate() -> None:
    spawning = _spawning_functions(RUNTIME)

    assert set(spawning) == {"_run_child"}, (
        "a new spawn site appeared in provider_local_runtime.py; gate it with "
        f"{ENFORCER} or declare why it runs no customer code: {sorted(spawning)}"
    )
    assert _calls_enforcer(spawning["_run_child"])


def test_the_seed_materialization_spawns_are_gated_or_declared_core_owned() -> None:
    spawning = _spawning_functions(SEED)

    assert set(spawning) == EXEMPT_SEED_SPAWNS | {"_derive_local_seed_pins"}
    assert _calls_enforcer(spawning["_derive_local_seed_pins"])
    for name in EXEMPT_SEED_SPAWNS:
        docstring = ast.get_docstring(spawning[name])
        assert docstring is not None and "core-owned" in docstring


def test_lease_probe_spawns_are_exempt_by_name_and_run_no_customer_code() -> None:
    spawning = _spawning_functions(LEASES)

    assert set(spawning) == EXEMPT_LEASE_SPAWNS, (
        "a new spawn site appeared in provider_process_leases.py; gate it with "
        f"{ENFORCER} or add it to EXEMPT_LEASE_SPAWNS with its fixed argv: {sorted(spawning)}"
    )


def test_the_served_run_verbs_gate_before_any_work() -> None:
    """The served refusal must not depend on reaching the spawn chokepoint."""

    facade = ROOT / "src/cruxible_core/runtime/playbill_api.py"
    tree = ast.parse(facade.read_text(encoding="utf-8"), filename=str(facade))
    gated = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _calls_enforcer(node)
    }

    assert {
        "playbill_procedure_run",
        "playbill_line_run",
        "playbill_provider_seed",
    } <= gated
