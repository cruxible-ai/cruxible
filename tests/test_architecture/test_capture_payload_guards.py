"""Capture = payload, enforced (D-R25..D-R27; procedures-e2e.md R1-R4, §4).

Guards over core's source (``src/cruxible_core`` and the client package):

(a) name-based dispatch is refused structurally: no comparison, membership,
    ``match`` or dict lookup branches a provider/operation/builtin identifier (or
    a local alias of one) on a string that is not core-declared vocabulary
    (``_payload_guard_analysis.dispatch_violations``); the builtin names branch
    only at their recognition sites. As a second, vocabulary layer, no known
    provider id or operation name appears as a literal or identifier, and
    ``workspace.file`` is spelled only at its declaration sites.
(b) Capture bytes cross one boundary: they are read from the body store only by
    the declared readers and decoded only by the extract runtime
    (``decoding_violations``); and no core function reads a wrapper key
    (``body_base64``, ``retrieved``, ``exchanges``, ``derived``) except the L3
    transport functions allow-listed per function.
(d) no kit or domain names in core logic; docstrings, ``description=``/``help=``
    text and the allow-listed examples module are documentation.

(c) -- Capture admission never parses content -- is a runtime guard and lives in
``tests/test_e2e/test_skeleton_kev_epss.py`` beside the skeleton it shares.

Every exemption is in ``payload_guard_allowlist.json``, scoped to a function
(``<path>::<qualified function>``) with its reason, and must still be needed.
Mutation tests below hold each guard to the bypasses it exists to stop.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

import pytest
from tests.support.provider_checkout import provider_checkout_path
from tests.test_architecture._payload_guard_analysis import (
    Corpus,
    Function,
    decoding_violations,
    dispatch_violations,
    material_read_sites,
)

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


BUILTIN = ALLOW["builtin"]
#: The vocabulary layer: every known name but the builtin, which is checked by site.
NAME_LITERAL = _bounded(tuple(name for name in _names() if name not in BUILTIN))
BUILTIN_LITERAL = _bounded(("workspace.file",))
#: In an identifier ``_`` joins words, so a name is a token between other words.
NAME_IDENTIFIER = _bounded(
    tuple(_identifier_form(name) for name in _names() if name not in BUILTIN),
    separators="",
)
DOMAIN = re.compile(
    rf"(?<![a-z0-9])(?:{'|'.join(DOMAIN_WORDS)})(?![a-z0-9])|(?<![a-z0-9_])security\.",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _repository_modules() -> dict[str, ast.Module]:
    return {
        path.relative_to(REPO).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for base in SOURCES
        for path in sorted(base.rglob("*.py"))
    }


@lru_cache(maxsize=1)
def _corpus() -> Corpus:
    return Corpus.of(_repository_modules())


def _modules() -> Iterator[tuple[str, ast.Module]]:
    yield from _repository_modules().items()


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


def _function_strings(corpus: Corpus) -> Iterator[tuple[Function, ast.Constant]]:
    """Every non-documentation string constant, with the function that holds it."""

    from tests.test_architecture._payload_guard_analysis import _own_nodes

    documentation = {path: _documentation(tree) for path, tree in corpus.modules.items()}
    for function in corpus.functions:
        for node in _own_nodes(function):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in documentation[function.path]
            ):
                yield function, node


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


def _corpus_of(sources: dict[str, str]) -> Corpus:
    return Corpus.of({path: ast.parse(textwrap.dedent(text)) for path, text in sources.items()})


def _builtin_dispatch_sites() -> dict[str, list[str]]:
    return {name: list(entry.get("dispatch", {})) for name, entry in BUILTIN.items()}


def _dispatch(corpus: Corpus) -> list[str]:
    return dispatch_violations(
        corpus,
        builtin_sites=_builtin_dispatch_sites(),
        dispatch_sites=ALLOW["dispatch_sites"],
    )


# -- (a) dispatch, structurally -------------------------------------------------------


def test_a_no_name_based_dispatch_in_core() -> None:
    found = _dispatch(_corpus())
    assert not found, (
        "core never branches on a provider id, operation name or builtin name outside its "
        "recognition sites (R3); act on the declared type instead:\n" + "\n".join(found)
    )


@pytest.mark.parametrize(
    "source",
    [
        # An unknown name: no vocabulary needed to refuse it.
        """
        def route(operation):
            if operation == "x":
                return 1
        """,
        # An alias of the identifier, against a set.
        """
        def route(occurrence):
            op = occurrence.operation.name
            return op in {"vendor.fetch", "vendor.parse"}
        """,
        # Through a module-level constant.
        """
        SPECIAL = "vendor.fetch"

        def route(receipt):
            return receipt.provider_id != SPECIAL
        """,
        # Dict-key dispatch, subscripted and by .get.
        """
        HANDLERS = {"vendor.fetch": 1, "vendor.parse": 2}

        def route(occurrence):
            return HANDLERS[occurrence.operation.name]
        """,
        """
        HANDLERS = {"vendor.fetch": 1}

        def route(provider):
            return HANDLERS.get(provider.identity.name)
        """,
        # match on the identifier.
        """
        def route(bound):
            match bound.entrypoint:
                case "vendor.module:Fetch":
                    return 1
        """,
        # The builtin, branched on outside its recognition sites.
        """
        def route(bound):
            return bound.builtin == "workspace.file"
        """,
    ],
    ids=["unknown-name", "alias", "constant", "dict-subscript", "dict-get", "match", "builtin"],
)
def test_a_name_based_dispatch_is_caught(source: str) -> None:
    corpus = _corpus_of({"src/cruxible_core/mutant.py": source})
    assert _dispatch(corpus), source


def test_a_declared_vocabulary_and_the_real_builtin_site_pass() -> None:
    corpus = _corpus_of(
        {
            "src/cruxible_core/mutant.py": """
            from typing import Literal

            Kind = Literal["call", "transform"]

            def route(body):
                return body.operation == "call"
            """
        }
    )
    assert _dispatch(corpus) == []
    real = next(
        function
        for function in _corpus().functions
        if function.site in BUILTIN["workspace.file"]["dispatch"]
    )
    assert real.qualname == "ProcedureExecutor._run_acquiring_call"


def test_a_every_dispatch_exemption_is_still_needed() -> None:
    from tests.test_architecture._payload_guard_analysis import _branch_values

    corpus = _corpus()
    by_site = {function.site: function for function in corpus.functions}
    exempt = {
        **ALLOW["dispatch_sites"],
        **{site: name for name, entry in BUILTIN.items() for site in entry.get("dispatch", {})},
    }
    for site in exempt:
        assert site in by_site, f"allow-listed site {site} does not exist"
        assert list(_branch_values(by_site[site], corpus)), f"{site} no longer branches"


# -- (a) the vocabulary layer -----------------------------------------------------------


def test_a_no_known_provider_or_operation_names_in_core() -> None:
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
    assert not found, "core never spells a provider id or operation name (R3):\n" + "\n".join(found)


def test_a_the_builtin_is_spelled_only_at_its_declaration_sites() -> None:
    sites = BUILTIN["workspace.file"]["declarations"]
    found = sorted(
        {
            f"{function.site}:{node.lineno}"
            for function, node in _function_strings(_corpus())
            if BUILTIN_LITERAL.search(node.value) and function.site not in sites
        }
    )
    assert not found, (
        "workspace.file is recognized by its builtin signature; spell it only where it is "
        "declared (use WORKSPACE_FILE_BUILTIN to branch on it):\n" + "\n".join(found)
    )
    used = {
        function.site
        for function, node in _function_strings(_corpus())
        if BUILTIN_LITERAL.search(node.value)
    }
    assert set(sites) <= used, sorted(set(sites) - used)


def test_the_builtin_allow_list_holds_core_builtins_only() -> None:
    assert set(BUILTIN) == {"workspace.file", "cruxible-builtin"}


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


# -- (b) the Capture-byte boundary -------------------------------------------------------


def _decoding(corpus: Corpus) -> list[str]:
    return decoding_violations(
        corpus,
        read_sites=ALLOW["material_readers"],
        decode_sites=ALLOW["material_decoders"],
    )


def test_b_capture_bytes_are_decoded_only_by_extract() -> None:
    found = _decoding(_corpus())
    assert not found, (
        "core reads Capture bytes only through extract under the CaptureKind's decoding "
        "(R1):\n" + "\n".join(found)
    )


def test_b_the_declared_readers_are_exactly_the_readers() -> None:
    readers = {site.rsplit(":", 1)[0] for site in material_read_sites(_corpus())}
    assert readers == set(ALLOW["material_readers"])


_LEGACY_WORKSPACE_READER = """
import json

def _legacy_workspace_content(bodies, envelope):
    commitment = envelope.commitment
    material = json.loads(bodies.read(commitment.digest, access=_ACCESS))
    source = material.get("source") if isinstance(material, dict) else None
    return source.get("bytes_digest")
"""


@pytest.mark.parametrize(
    ("path", "source"),
    [
        # The deleted refusal-index reader, restored.
        ("src/cruxible_core/proposals/refusal_index.py", _LEGACY_WORKSPACE_READER),
        # A declared reader that starts decoding a new wrapper key.
        (
            "packages/cruxible-client/src/cruxible_client/contracts/captures.py",
            """
            import json

            def _verify_capture_once(store, envelope, access):
                material = store.read(envelope.commitment.digest, access=access)
                return json.loads(material)["payload"]
            """,
        ),
        # The same, one call away.
        (
            "packages/cruxible-client/src/cruxible_client/contracts/captures.py",
            """
            import json

            def _unwrap_payload_mutant(content):
                return json.loads(content)["payload"]

            def _verify_capture_once(store, envelope, access):
                material = store.read(envelope.commitment.digest, access=access)
                return _unwrap_payload_mutant(material)
            """,
        ),
        # Text-decoding the bytes by way of a local commitment digest.
        (
            "packages/cruxible-client/src/cruxible_client/contracts/claims.py",
            """
            def _resolve_cited_source_window(store, envelope, access):
                digest = envelope.commitment.digest
                content = store.read(digest, access=access)
                return content.decode("utf-8")
            """,
        ),
    ],
    ids=["legacy-reader", "new-wrapper-key", "one-call-away", "text-decode"],
)
def test_b_decoding_capture_bytes_outside_extract_is_caught(path: str, source: str) -> None:
    assert _decoding(_corpus_of({path: source})), source


def _wrapper_reads(function: Function) -> Iterator[tuple[int, str]]:
    from tests.test_architecture._payload_guard_analysis import _own_nodes

    for node in _own_nodes(function):
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


def _wrapper_violations(corpus: Corpus) -> list[str]:
    allowed = {site: set(entry["keys"]) for site, entry in ALLOW["wrapper_reads"].items()}
    return [
        f"{function.site}:{line}: reads {key!r}"
        for function in corpus.functions
        for line, key in _wrapper_reads(function)
        if key not in allowed.get(function.site, set())
    ]


def test_b_no_capture_wrapper_reads_in_core() -> None:
    found = _wrapper_violations(_corpus())
    assert not found, (
        "a provider payload carries no wrapper; transport reads are exempt per function:\n"
        + "\n".join(found)
    )


def test_b_every_wrapper_exemption_is_still_needed_and_scoped() -> None:
    reads = {
        function.site: {key for _line, key in _wrapper_reads(function)}
        for function in _corpus().functions
    }
    for site, entry in ALLOW["wrapper_reads"].items():
        assert entry["reason"]
        assert set(entry["keys"]) <= reads.get(site, set()), (site, entry["keys"])
    # The exemption covers its function, not its file: a second function there is caught.
    mutant = _corpus_of(
        {
            "packages/cruxible-client/src/cruxible_client/contracts/source_references.py": """
            def peek(result):
                return result.body_base64
            """
        }
    )
    assert _wrapper_violations(mutant)


# -- (d) domain names ----------------------------------------------------------------------


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
def test_the_word_guards_catch_what_they_forbid(guard: re.Pattern[str], sample: str) -> None:
    assert guard.search(sample.replace("_", " ") if guard is DOMAIN else sample)


def test_the_name_guard_ignores_the_runtime_library() -> None:
    assert not NAME_LITERAL.search("cruxible-provider-runtime")
    assert not NAME_IDENTIFIER.search("workspace_file_contracts")
