"""Guardrail: no public surface says Playbill.

Playbill was the development name. The product is Cruxible on every surface a
caller meets: CLI commands and help, MCP tools and their schemas and
instructions, HTTP routes and the OpenAPI document, SDK names, served error
codes, and the docs. What legitimately keeps the old spelling is frozen by
format, not by habit, and is allowlisted by pattern:

- format tags (``playbill-*-vN``), whether stored or served: they name the
  bytes' own format, and MCP input schemas hide them anyway;
- versioned dotted identifiers (law ids, capture contract and component pins,
  curation detector ids) and the persisted unversioned capture identities and
  taint labels;
- ledger Git names (``refs/notes/playbill-*``, mirror pins, lock files, the
  ``playbill-daemon`` committer) and the legacy-detection file names;
- the ConsumptionOperation literals inside hash-chained consumption receipts.

Docs may also name internal paths (``tests/...``, ``benchmarks/playbill_*``,
``scripts/update_playbill_*``): internal module and file names are out of scope
for the public name. The CHANGELOG is history and is not checked.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
import re
from pathlib import Path

import click
import pytest

ROOT = Path(__file__).resolve().parents[2]

_CONSUMPTION_OPERATIONS = (
    "claim.get",
    "claim_type.get",
    "coverage.resolve",
    "discover.match",
    "expand",
    "procedure.run.resolve",
    "query.run",
    "query_definition.get",
    "search.match",
    "subject.get",
)
FROZEN = re.compile(
    "|".join(
        [
            r"playbill-[a-z0-9-]+-v\d+(?:\.\d+)?(?:-row)?",
            r"playbill-\*-vN",
            r"playbill\.[a-z0-9_.{}-]*[.-]v\d+",
            r"playbill\.(?:foreign-source\.[A-Za-z0-9_.{}*-]*|direct-authoring"
            r"|coordinator-authoring|taint\.[a-z-]+)",
            r"refs/notes/playbill-[a-z]+",
            r"refs/playbill-mirror-pins",
            r"playbill-(?:activation|notes|generations-in-flight)\.lock",
            r"playbill-daemon",
            r"daemon@playbill\.invalid",
            r"playbill-(?:gen|eval|approval)\b",
            r"playbill-v1",
            r"playbill\.(?:%s)(?![a-z_.])"
            % "|".join(re.escape(op) for op in _CONSUMPTION_OPERATIONS),
        ]
    )
)
INTERNAL_PATHS = re.compile(
    r"(?:tests|benchmarks|scripts|src|packages)/[\w./-]*playbill[\w./-]*"
    r"|\bplaybill_(?:taubench|adoption_scale)\b"
    r"|\btest_playbill_\w*"
    r"|\bupdate_playbill_\w+"
)


def _offending(text: str, *, internal_paths: bool = False) -> list[str]:
    stripped = FROZEN.sub("", text)
    if internal_paths:
        stripped = INTERNAL_PATHS.sub("", stripped)
    return [match.group(0) for match in re.finditer(r"[\w./:-]*playbill[\w./:-]*", stripped, re.I)]


def test_cli_commands_and_help_never_say_playbill() -> None:
    from cruxible_core.cli.main import cli

    offenders: list[str] = []

    def walk(command: click.Command, path: tuple[str, ...]) -> None:
        texts = [" ".join(path), command.help or "", command.short_help or ""]
        for parameter in command.params:
            texts.extend(parameter.opts)
            texts.append(getattr(parameter, "help", None) or "")
        for text in texts:
            offenders.extend(f"{' '.join(path)}: {hit}" for hit in _offending(text))
        if isinstance(command, click.Group):
            for name, child in command.commands.items():
                walk(child, (*path, name))

    walk(cli, ("cruxible",))
    assert offenders == []


@pytest.mark.parametrize("profile", ["default", "full"])
def test_mcp_tools_schemas_and_instructions_never_say_playbill(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    from cruxible_core.mcp.server import create_server

    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", profile)
    server = create_server()
    tools = asyncio.run(server.list_tools())
    offenders = [
        f"{tool.name}: {hit}"
        for tool in tools
        for text in (
            tool.name,
            tool.description or "",
            json.dumps(tool.inputSchema),
            json.dumps(tool.outputSchema or {}),
        )
        for hit in _offending(text)
    ]
    offenders.extend(
        f"instructions: {hit}" for hit in _offending(server._mcp_server.instructions or "")
    )
    assert offenders == []


def test_http_routes_tags_and_schemas_never_say_playbill() -> None:
    from tests.support.http_surface import generate_openapi_spec

    assert _offending(json.dumps(generate_openapi_spec())) == []


def _public_client_names() -> set[str]:
    import cruxible_client

    names = set(cruxible_client.__all__)
    for info in pkgutil.walk_packages(cruxible_client.__path__, "cruxible_client."):
        if any(part.startswith("_") for part in info.name.split(".")):
            continue
        module = importlib.import_module(info.name)
        names.update(name for name in vars(module) if not name.startswith("_"))
    from cruxible_client import Cruxible, CruxibleClient

    names.update(name for name in dir(Cruxible) if not name.startswith("_"))
    names.update(name for name in dir(CruxibleClient) if not name.startswith("_"))
    return names


def test_sdk_names_never_say_playbill() -> None:
    assert sorted(name for name in _public_client_names() if "playbill" in name.lower()) == []


def test_served_error_codes_are_cruxible_codes() -> None:
    from cruxible_core.service.refusals import ALL_SERVED_REFUSAL_CODES

    assert sorted(code for code in ALL_SERVED_REFUSAL_CODES if _offending(code)) == []


def _docs() -> list[Path]:
    paths = [
        ROOT / "README.md",
        ROOT / "context7.json",
        ROOT / "mkdocs.yml",
        ROOT / "packages" / "cruxible-client" / "README.md",
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "skills").rglob("*.md"),
        *(path for path in (ROOT / "integrations").rglob("*") if path.is_file()),
    ]
    return [path for path in paths if path.is_file()]


def test_docs_never_say_playbill() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {hit}"
        for path in _docs()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for hit in _offending(line, internal_paths=True)
    ]
    assert offenders == []


# The product is "Cruxible" in prose: never "Cruxible Core" and never "Core" as
# the product's name. Identifiers keep their spelling (cruxible_core,
# cruxible-core, CoreError), and released CHANGELOG sections are history.
FORBIDDEN_PRODUCT_SPELLING = re.compile(r"\bCruxible Core\b|(?<![\w`.-])Core\b(?![\w`(-])")


def _unreleased_changelog() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("## Unreleased", 1)[1]
    return re.split(r"\n## \[?\d", unreleased, maxsplit=1)[0]


def _prose_documents() -> list[tuple[str, str]]:
    paths = [
        *_docs(),
        ROOT / "AGENTS.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
    ]
    documents = [
        (str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
        for path in paths
        if path.is_file()
    ]
    documents.append(("CHANGELOG.md (Unreleased)", _unreleased_changelog()))
    return documents


def test_prose_never_names_the_product_core() -> None:
    offenders = [
        f"{name}:{number}: {line.strip()[:100]}"
        for name, text in _prose_documents()
        for number, line in enumerate(text.splitlines(), 1)
        if FORBIDDEN_PRODUCT_SPELLING.search(line)
    ]
    assert offenders == []


def test_the_forbidden_spelling_pattern_spares_identifiers() -> None:
    assert FORBIDDEN_PRODUCT_SPELLING.search("Core verifies it")
    assert FORBIDDEN_PRODUCT_SPELLING.search("the old Cruxible Core")
    for allowed in ("cruxible_core.errors", "cruxible-core", "`CoreError`", "CoreError"):
        assert not FORBIDDEN_PRODUCT_SPELLING.search(allowed), allowed
