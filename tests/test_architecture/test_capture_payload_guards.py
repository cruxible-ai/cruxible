"""Capture = payload, enforced (D-R25..D-R27; procedures-e2e.md R1-R4, §4).

Static guards over core's source (``src/cruxible_core`` and the client package):

(a) no provider id or operation name -- as a string literal or an identifier --
    outside the builtin registry; the allow-list names ``workspace.file`` only;
(b) no reads of Capture-wrapper keys (``body_base64``, ``retrieved``,
    ``exchanges``, ``derived``); L3 transport uses are allow-listed by file with
    a reason (compilation bundles, dereference selections); the provider-result
    ``content_base64`` wire field is not a wrapper key;
(d) no kit or domain names in core logic; docstrings, ``description=``/``help=``
    text and the allow-listed examples module are documentation.

(c) -- Capture admission never parses content -- is a runtime guard and lives in
``tests/test_e2e/test_skeleton_kev_epss.py`` beside the skeleton it shares.

The allow-list (``payload_guard_allowlist.json``) is the only way past a guard.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.provider_checkout import provider_checkout_path

REPO = Path(__file__).resolve().parents[2]
SOURCES = (REPO / "src/cruxible_core", REPO / "packages/cruxible-client/src/cruxible_client")
ALLOW = json.loads((Path(__file__).with_name("payload_guard_allowlist.json")).read_text())

#: Every provider id and operation name the cruxible-providers packages declare.
#: ``test_the_vocabulary_covers_the_providers_checkout`` keeps it current.
PROVIDER_IDS = (
    "cruxible-provider-docs",
    "cruxible-provider-noop",
    "cruxible-provider-quant",
    "cruxible-provider-web",
    "cruxible-provider-workspace",
)
OPERATIONS = (
    "calc.calibrate",
    "calc.reduce",
    "doc.to_markdown",
    "match.record",
    "noop.echo",
    "ocr.extract",
    "score.rank",
    "search.web",
    "stat.test",
    "ts.anomaly",
    "ts.forecast",
    "web.fetch",
    "web.render",
    "workspace.file",
)
#: Kit and domain vocabulary core logic must never branch on (R3).
DOMAIN_WORDS = (
    "kev",
    "epss",
    "cisa",
    "osv",
    "nvd",
    "ghsa",
    "sarif",
    "cve",
    "cvss",
    "cwe",
    "caselaw",
    "case_law",
)
WRAPPER_KEYS = frozenset({"body_base64", "retrieved", "exchanges", "derived"})
#: ``derived`` is also an ordinary word for attributes (``instance.derived``);
#: only its use as a key is the wrapper read.
WRAPPER_ATTRIBUTES = WRAPPER_KEYS - {"derived"}


def _names() -> tuple[str, ...]:
    modules = tuple(item.replace("-", "_") for item in PROVIDER_IDS)
    return tuple(name for name in (*PROVIDER_IDS, *modules, *OPERATIONS))


def _bounded(words: tuple[str, ...], *, separators: str = "_-") -> re.Pattern[str]:
    """``words`` as whole tokens; ``separators`` are the characters that join a token."""

    alternatives = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    edge = f"A-Za-z0-9{separators}"
    return re.compile(rf"(?<![{edge}])(?:{alternatives})(?![{edge}])")


def _identifier_form(name: str) -> str:
    return re.sub(r"[.-]", "_", name).lower()


ALLOWED_NAMES = frozenset(ALLOW["provider_names"])
NAME_LITERAL = _bounded(tuple(name for name in _names() if name not in ALLOWED_NAMES))
#: In an identifier ``_`` joins words, so a name is a token between other words.
NAME_IDENTIFIER = _bounded(
    tuple(_identifier_form(name) for name in _names() if name not in ALLOWED_NAMES),
    separators="",
)
DOMAIN = re.compile(
    rf"(?<![a-z0-9])(?:{'|'.join(DOMAIN_WORDS)})(?![a-z0-9])|(?<![a-z0-9_])security\.",
    re.IGNORECASE,
)


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for base in SOURCES:
        for path in sorted(base.rglob("*.py")):
            yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _documentation(tree: ast.Module) -> set[int]:
    """Constants that are documentation: docstrings and description/help text."""

    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            found.add(id(node.value))
        if isinstance(node, ast.keyword) and node.arg in {"description", "help"}:
            found.update(id(sub) for sub in ast.walk(node.value) if isinstance(sub, ast.Constant))
    return found


def _strings(tree: ast.Module) -> Iterator[ast.Constant]:
    documentation = _documentation(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in documentation
        ):
            yield node


def _identifiers(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, node.name
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg
        elif isinstance(node, ast.alias):
            yield node.lineno, node.asname or node.name


def test_a_no_provider_or_operation_names_outside_the_builtin_registry() -> None:
    found = []
    for path, tree in _modules():
        found += [
            f"{path}:{node.lineno}: {match.group(0)!r}"
            for node in _strings(tree)
            if (match := NAME_LITERAL.search(node.value))
        ]
        found += [
            f"{path}:{line}: identifier {name!r}"
            for line, name in _identifiers(tree)
            if NAME_IDENTIFIER.search(name.lower())
        ]
    assert not found, (
        "core never branches on a provider id or operation name (R3); act on the declared "
        "type instead:\n" + "\n".join(found)
    )


def test_the_name_allow_list_is_the_builtin_only() -> None:
    assert ALLOWED_NAMES == {"workspace.file"}


def test_the_vocabulary_covers_the_providers_checkout() -> None:
    """Every provider package's id and operations are in the guarded vocabulary."""

    checkout = provider_checkout_path()
    if checkout is None:
        pytest.skip("set CRUXIBLE_PROVIDERS_CHECKOUT to check the vocabulary against it")
    ids, operations = set(), set()
    for registration in sorted(checkout.glob("packages/*/src/*/registration.json")):
        operations |= {item["name"] for item in json.loads(registration.read_text())["operations"]}
        manifest = registration.with_name("manifest.yaml").read_text()
        ids |= set(re.findall(r"^provider_id:\s*(\S+)\s*$", manifest, re.MULTILINE))
    assert ids and operations
    assert ids <= set(PROVIDER_IDS), sorted(ids - set(PROVIDER_IDS))
    assert operations <= set(OPERATIONS), sorted(operations - set(OPERATIONS))


def _wrapper_reads(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in WRAPPER_KEYS
            and not (isinstance(node.value, ast.Name) and node.value.id == "Literal")
        ):
            yield node.lineno, str(node.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "pop", "setdefault"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in WRAPPER_KEYS
        ):
            yield node.lineno, str(node.args[0].value)
        elif (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and node.left.value in WRAPPER_KEYS
            and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
        ):
            yield node.lineno, str(node.left.value)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in WRAPPER_ATTRIBUTES
        ):
            yield node.lineno, node.attr


def test_b_no_capture_wrapper_reads_in_core() -> None:
    allowed = {path: set(entry["keys"]) for path, entry in ALLOW["wrapper_reads"].items()}
    found = [
        f"{path}:{line}: reads {key!r}"
        for path, tree in _modules()
        for line, key in _wrapper_reads(tree)
        if key not in allowed.get(path, set())
    ]
    assert not found, (
        "core reads Capture bytes only through extract under the CaptureKind's decoding "
        "(R1); a provider payload carries no wrapper:\n" + "\n".join(found)
    )


def test_b_every_wrapper_allow_list_entry_is_still_needed() -> None:
    reads = {path: {key for _line, key in _wrapper_reads(tree)} for path, tree in _modules()}
    for path, entry in ALLOW["wrapper_reads"].items():
        assert entry["reason"]
        assert set(entry["keys"]) <= reads.get(path, set()), (path, entry["keys"])


def test_d_no_kit_or_domain_names_in_core_logic() -> None:
    exempt = set(ALLOW["domain_names"])
    found = []
    for path, tree in _modules():
        if path in exempt:
            continue
        found += [
            f"{path}:{node.lineno}: {match.group(0)!r}"
            for node in _strings(tree)
            if (match := DOMAIN.search(node.value))
        ]
        found += [
            f"{path}:{line}: identifier {name!r}"
            for line, name in _identifiers(tree)
            if DOMAIN.search(name.replace("_", " "))
        ]
    assert not found, (
        "core logic carries no kit or domain vocabulary (R3); examples and docs are exempt:\n"
        + "\n".join(found)
    )


@pytest.mark.parametrize(
    ("guard", "sample"),
    [
        (NAME_LITERAL, 'if operation.name == "web.fetch":'),
        (NAME_LITERAL, "contracts/web.fetch.result/v1.json"),
        (NAME_LITERAL, "cruxible-provider-web"),
        (NAME_IDENTIFIER, "web_fetch_body_digest"),
        (NAME_IDENTIFIER, "cruxible_provider_web"),
        (DOMAIN, "kev_listed"),
        (DOMAIN, "security.vulnerability"),
    ],
)
def test_the_guards_catch_what_they_forbid(guard: re.Pattern[str], sample: str) -> None:
    assert guard.search(sample.replace("_", " ") if guard is DOMAIN else sample)


def test_the_name_guard_ignores_the_builtin_and_runtime_library() -> None:
    assert not NAME_LITERAL.search("workspace.file.request@1")
    assert not NAME_LITERAL.search("cruxible-provider-runtime")
    assert not NAME_IDENTIFIER.search("workspace_file_contracts")
