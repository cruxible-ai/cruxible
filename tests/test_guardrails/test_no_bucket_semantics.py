"""Core reads bucket data as inert bytes: no bucket semantics anywhere it can run.

Classifier removal (Phase A) left provider-runtime 0.2.x package metadata --
bucket vocabularies, conformance fixtures and selector claims -- in old formats
and in the packages that still ship it. Core must not judge it: no selector is
parsed or matched, no measured bucket is checked, and no package is read through
the provider runtime's validating registration reader, which does all of that.
This guard scans the shipped source for every name that would reintroduce it.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SOURCES = (
    _ROOT / "src" / "cruxible_core",
    _ROOT / "packages" / "cruxible-client" / "src" / "cruxible_client",
)

#: Bucket-semantic routines: core's retired vocabulary matching and the provider
#: runtime's validating readers and bucket model.
_FORBIDDEN_NAMES = frozenset(
    {
        "parse_selector",
        "selector_matches",
        "validate_bucket",
        "BucketSelector",
        "parse_bucket_id",
        "load_bucket_vocabulary",
        "load_registration",
        "wheel_registration",
        "registration_from_distribution",
        "export_document",
        "classify_web_fetch",
        "admitted_bucket_selectors",
    }
)
#: Provider-runtime modules that hold bucket semantics (``toolchain(<name>)``).
_FORBIDDEN_TOOLCHAIN_MODULES = frozenset({"buckets", "registration", "registry", "execute"})


def _violations(path: Path, root: Path = _ROOT) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name
        elif isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
        if name in _FORBIDDEN_NAMES:
            found.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')} {name}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "toolchain"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in _FORBIDDEN_TOOLCHAIN_MODULES
        ):
            found.append(
                f"{path.relative_to(root)}:{node.lineno} toolchain({node.args[0].value!r})"
            )
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "cruxible_provider_runtime"
        ):
            module = (node.module or "").split(".")
            if len(module) > 1 and module[1] in _FORBIDDEN_TOOLCHAIN_MODULES:
                found.append(f"{path.relative_to(root)}:{node.lineno} from {node.module}")
    return found


def test_no_shipped_source_reaches_bucket_semantics() -> None:
    found = [
        violation
        for source in _SOURCES
        for path in sorted(source.rglob("*.py"))
        for violation in _violations(path)
    ]
    assert found == []


def test_the_guard_sees_each_forbidden_spelling(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from cruxible_provider_runtime.registration import thing\n"
        "vocabulary.selector_matches(a, b)\n"
        "toolchain('registration').load_registration(root)\n"
        "with wheels.wheel_registration(path) as bundle:\n"
        "    pass\n"
    )
    found = _violations(probe, tmp_path)
    assert sorted(item.split(" ", 1)[1] for item in found) == sorted(
        [
            "from cruxible_provider_runtime.registration",
            "selector_matches",
            "toolchain('registration')",
            "load_registration",
            "wheel_registration",
        ]
    )
