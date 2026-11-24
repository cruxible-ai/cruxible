"""0.6.0 kit recipes (``kit adopt``) and hash-verified read-only scripts (``kit run``).

Covers the manifest contract (omitted when empty, release identity unchanged),
the strict ``{{param}}`` renderer, ``kit build``'s recipe preflight, the
client's verified kit file cache, and the two CLI commands end to end against a
real daemon (the kit tests' publisher/consumer pair).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from cruxible_client import CruxibleClient
from cruxible_client.contracts.canonical import pretty_canonical_bytes
from cruxible_client.contracts.kit_recipes import (
    KitRecipeError,
    KitRecipeParamError,
    parse_param_pairs,
    preflight_values,
    render_recipe,
    render_with_dependencies,
    resolve_params,
)
from cruxible_client.contracts.kits import (
    KIT_MANIFEST_FILE,
    InstalledKit,
    KitBuildRequest,
    KitBuildResult,
    KitBundle,
    KitFileBytes,
    KitManifest,
    KitRecipeParamSpec,
    KitRecipeSpec,
    KitScriptSpec,
    KitStatus,
    kit_file_entry,
    kit_file_role_of,
)
from cruxible_core.cli.kit_cache import (
    KitCacheError,
    installed_cache_key,
    kit_cache_directory,
    load_installed_kit,
    write_kit_cache,
)
from cruxible_core.cli.main import cli
from cruxible_core.errors import RequestRefusedError
from cruxible_core.runtime import playbill_api
from tests.test_client.test_artifacts import _bundle
from tests.test_server.test_playbill_kits import (  # noqa: F401 (fixture)
    SEATS,
    _claim_type,
    _World,
    worlds,
)

# -- fixtures and helpers -------------------------------------------------------------

DEFAULT_RECIPE = {
    "kind": "change_set",
    "members": [{"kind": "runbook", "name": "acme.start", "steps": [{"action": "evidence.sweep"}]}],
}
DAILY_RECIPE = {
    "kind": "change_set",
    "members": [
        {
            "kind": "trigger",
            "name": "acme.{{name}}-daily",
            "runbook_name": "acme.start",
            "schedule": {"kind": "cron", "expression": "{{cron}}"},
        }
    ],
}
SCRIPT = """\
import json, os, sys
out = sys.argv[1]
with open(out, "w") as handle:
    json.dump(
        {
            "args": sys.argv[2:],
            "kit": os.environ.get("CRUXIBLE_KIT_ID"),
            "version": os.environ.get("CRUXIBLE_KIT_VERSION"),
            "mode": os.environ.get("CRUXIBLE_KIT_SCRIPT_MODE"),
            "instance": os.environ.get("CRUXIBLE_INSTANCE_ID"),
            "files": os.environ.get("CRUXIBLE_KIT_FILES"),
        },
        handle,
    )
sys.exit(int(os.environ.get("KIT_SCRIPT_EXIT", "0")))
"""

NAME_PARAM = KitRecipeParamSpec(
    description="Short identifier.",
    required=True,
    pattern=r"^[a-z][a-z0-9-]{0,20}$",
    example="nightly",
)
DEFAULT_SPEC = KitRecipeSpec(
    name="default", path="recipes/default.json", description="The start Runbook."
)
DAILY_SPEC = KitRecipeSpec(
    name="daily",
    path="recipes/daily.json",
    description="One daily Trigger of the start Runbook.",
    params={
        "name": NAME_PARAM,
        "cron": KitRecipeParamSpec(description="Cron expression.", default="0 6 * * *"),
    },
    depends_on=("default",),
)
REPORT_SPEC = KitScriptSpec(
    name="report", path="scripts/report.py", description="Report.", mode="read_only"
)


def _recipe_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=1) + "\n").encode()


ADOPTION_FILES = {
    "recipes/default.json": _recipe_bytes(DEFAULT_RECIPE),
    "recipes/daily.json": _recipe_bytes(DAILY_RECIPE),
    "scripts/report.py": SCRIPT.encode(),
}


def _adoption_request(**overrides: Any) -> KitBuildRequest:
    fields: dict[str, Any] = {
        "kit_id": "acme",
        "version": "1.0.0",
        "owns": ("acme.",),
        "recipes": (DEFAULT_SPEC, DAILY_SPEC),
        "scripts": (REPORT_SPEC,),
        "files": tuple(
            KitFileBytes.of(path, content) for path, content in sorted(ADOPTION_FILES.items())
        ),
    }
    fields.update(overrides)
    return KitBuildRequest(**fields)


def _adoption_bundle(files: dict[str, bytes] | None = None) -> KitBundle:
    """The CLI test kit: the plain test bundle plus two recipes and a script."""

    carried = dict(sorted((files or ADOPTION_FILES).items()))
    plain = _bundle()
    return KitBundle(
        manifest=plain.manifest.model_copy(
            update={
                "files": tuple(kit_file_entry(path, content) for path, content in carried.items()),
                "recipes": (DEFAULT_SPEC, DAILY_SPEC),
                "scripts": (REPORT_SPEC,),
            }
        ),
        artifacts=plain.artifacts,
        files=tuple(KitFileBytes.of(path, content) for path, content in carried.items()),
    )


# -- manifest contract ---------------------------------------------------------------


def test_recipe_and_script_paths_imply_their_roles() -> None:
    assert kit_file_role_of("recipes/sbom.json") == "recipe"
    assert kit_file_role_of("scripts/export_vex.py") == "script"
    assert kit_file_role_of("recipes") == "reference"
    assert kit_file_role_of("examples/export_vex.py") == "example"


def test_a_manifest_without_recipes_or_scripts_keeps_its_bytes_and_identity() -> None:
    plain = _bundle().manifest
    dumped = plain.model_dump(mode="json")
    assert "recipes" not in dumped and "scripts" not in dumped
    adopted = _adoption_bundle().manifest
    # Recipes and scripts are inert, like the files they name: same release identity.
    assert adopted.content_digest == plain.content_digest
    round_trip = KitManifest.model_validate_json(
        pretty_canonical_bytes(adopted.model_dump(mode="json"))
    )
    assert round_trip == adopted


def test_the_bundled_base_kit_manifest_is_byte_identical_and_keeps_its_digest() -> None:
    from cruxible_core.kits.base_kit_pin import BASE_KIT_PIN

    raw = (
        Path(__file__).resolve().parents[1] / "src/cruxible_core/kits/base" / KIT_MANIFEST_FILE
    ).read_bytes()
    manifest = KitManifest.model_validate_json(raw)
    assert pretty_canonical_bytes(manifest.model_dump(mode="json")) == raw
    assert manifest.content_digest == BASE_KIT_PIN.content_digest


RO = {"mode": "read_only"}


def _manifest_with(**update: Any) -> dict[str, Any]:
    payload = _adoption_bundle().manifest.model_dump(mode="json")
    payload.update(update)
    return payload


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (
            {
                "recipes": [
                    {"name": "x", "path": "recipes/missing.json", "description": "d"},
                ]
            },
            "not one of the kit's files",
        ),
        (
            {
                "recipes": [
                    {"name": "a", "path": "recipes/default.json", "description": "d"},
                    {"name": "a", "path": "recipes/daily.json", "description": "d"},
                ]
            },
            "declared twice",
        ),
        (
            {
                "recipes": [
                    {"name": "a", "path": "recipes/default.json", "description": "d"},
                    {"name": "b", "path": "recipes/default.json", "description": "d"},
                ]
            },
            "two recipes name the file",
        ),
        (
            {
                "scripts": [
                    {"name": "r", "path": "scripts/report.py", "description": "d", **RO},
                    {"name": "r", "path": "scripts/report.py", "description": "d", **RO},
                ]
            },
            "declared twice",
        ),
        (
            {"recipes": [{"name": "a", "path": "scripts/report.py", "description": "d"}]},
            "sits under recipes/",
        ),
        (
            {"scripts": [{"name": "a", "path": "recipes/daily.json", "description": "d", **RO}]},
            "sits under scripts/",
        ),
        (
            {
                "recipes": [
                    {
                        "name": "a",
                        "path": "recipes/default.json",
                        "description": "d",
                        "depends_on": ["b"],
                    },
                    {
                        "name": "b",
                        "path": "recipes/daily.json",
                        "description": "d",
                        "depends_on": ["a"],
                    },
                ]
            },
            "depends on itself",
        ),
        (
            {
                "recipes": [
                    {
                        "name": "a",
                        "path": "recipes/default.json",
                        "description": "d",
                        "depends_on": ["zzz"],
                    }
                ]
            },
            "unknown recipe zzz",
        ),
        (
            {
                "scripts": [
                    {"name": "r", "path": "scripts/report.py", "description": "d", "mode": "write"}
                ]
            },
            "read_only",
        ),
    ],
)
def test_a_manifest_refuses_inconsistent_recipes_and_scripts(
    update: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        KitManifest.model_validate(_manifest_with(**update))


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"type": "enum", "description": "d"}, "lists its allowed values"),
        ({"type": "string", "values": ["a"], "description": "d"}, "only an enum"),
        ({"pattern": "([", "description": "d"}, "not a regular expression"),
        ({"type": "integer", "pattern": "^1$", "description": "d"}, "only a string"),
        ({"required": True, "default": "x", "description": "d"}, "takes no default"),
        ({"pattern": "^[a-z]+$", "default": "A1", "description": "d"}, "does not match"),
        ({"type": "integer", "default": "3", "description": "d"}, "not an integer"),
        ({"type": "integer", "default": True, "description": "d"}, "not an integer"),
        ({"type": "boolean", "example": 1, "description": "d"}, "not a boolean"),
        ({"type": "enum", "values": ["a", "b"], "default": "c", "description": "d"}, "one of"),
        ({"description": " "}, "non-empty"),
    ],
)
def test_a_param_spec_refuses_inconsistent_declarations(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        KitRecipeParamSpec.model_validate(spec)


def test_a_recipe_refuses_a_param_name_a_placeholder_cannot_spell() -> None:
    with pytest.raises(ValidationError, match="lowercase letters, digits and '_'"):
        KitRecipeSpec(
            name="x",
            path="recipes/x.json",
            description="d",
            params={"Bad-Name": KitRecipeParamSpec(description="d")},
        )


def test_a_build_request_carries_exactly_its_recipe_and_script_files() -> None:
    with pytest.raises(ValidationError, match="missing: \\['scripts/report.py'\\]"):
        _adoption_request(
            files=tuple(
                KitFileBytes.of(path, content)
                for path, content in sorted(ADOPTION_FILES.items())
                if path.startswith("recipes/")
            )
        )
    with pytest.raises(ValidationError, match="unnamed: \\['recipes/extra.json'\\]"):
        _adoption_request(
            files=(
                *_adoption_request().files,
                KitFileBytes.of("recipes/extra.json", b"{}"),
            )
        )


# -- the renderer ---------------------------------------------------------------------

TYPED = KitRecipeSpec(
    name="typed",
    path="recipes/typed.json",
    description="Typed parameters.",
    params={
        "name": NAME_PARAM,
        "limit": KitRecipeParamSpec(type="integer", description="A count.", default=5),
        "strict": KitRecipeParamSpec(type="boolean", description="A flag.", default=False),
        "level": KitRecipeParamSpec(
            type="enum", description="A level.", values=("low", "high"), default="low"
        ),
    },
)
TYPED_CONTENT = json.dumps(
    {
        "kind": "change_set",
        "members": [
            {
                "kind": "line",
                "name": "acme.import.{{name}}",
                "limit": "{{limit}}",
                "strict": "{{strict}}",
                "level": "{{level}}",
                "note": "{{name}}: limit {{limit}}, strict {{strict}}",
                "untouched": 7,
            }
        ],
    }
)


def test_rendering_interpolates_strings_and_keeps_exact_placeholders_typed() -> None:
    values = resolve_params(
        "acme", TYPED, {"name": "app", "limit": "12", "strict": "yes", "level": "high"}
    )
    (member,) = render_recipe(TYPED, TYPED_CONTENT, values)["members"]
    assert member == {
        "kind": "line",
        "name": "acme.import.app",
        "limit": 12,
        "strict": True,
        "level": "high",
        "note": "app: limit 12, strict true",
        "untouched": 7,
    }


def test_defaults_fill_unset_params_and_a_value_never_changes_the_structure() -> None:
    hostile = 'x", "kind": "trigger'
    spec = TYPED.model_copy(
        update={
            "params": {
                **TYPED.params,
                "name": KitRecipeParamSpec(description="Any text.", default="x"),
            }
        }
    )
    values = resolve_params("acme", spec, {"name": hostile})
    (member,) = render_recipe(spec, TYPED_CONTENT, values)["members"]
    assert member["kind"] == "line"
    assert member["name"] == f"acme.import.{hostile}"
    assert (member["limit"], member["strict"], member["level"]) == (5, False, "low")


@pytest.mark.parametrize(
    ("given", "problem"),
    [
        ({}, "missing required parameter name"),
        ({"name": "App"}, "does not match"),
        ({"name": "app", "limit": "many"}, "is not an integer"),
        ({"name": "app", "strict": "maybe"}, "is not a boolean"),
        ({"name": "app", "level": "max"}, "is not one of low, high"),
        ({"name": "app", "colour": "red"}, "unknown parameter colour"),
    ],
)
def test_param_problems_refuse_with_the_param_table_and_usage(
    given: dict[str, str], problem: str
) -> None:
    with pytest.raises(KitRecipeParamError) as refused:
        resolve_params("acme", TYPED, given)
    text = str(refused.value)
    assert refused.value.error_code == "cruxible.kit.recipe_param_invalid"
    assert problem in text
    assert "parameters:" in text and "limit: integer, default 5: A count." in text
    assert "usage: cruxible kit adopt acme:typed --param name=nightly" in text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"kind": "change_set", "members": [{"name": "{{other}}"}]}', "undeclared parameter"),
        ('{"kind": "change_set", "members": [{"{{name}}": 1}]}', "object key"),
        ('{"kind": "change_set", "members": [{"name": "{{ name }}"}]}', "is not a placeholder"),
        ('{"kind": "change_set", "members": [{"name": "{{eval(1)}}"}]}', "is not a placeholder"),
        ("[1, 2]", "not a JSON object"),
        ("{not json", "not JSON"),
    ],
)
def test_malformed_or_undeclared_placeholders_refuse(content: str, message: str) -> None:
    with pytest.raises(KitRecipeError, match=message):
        render_recipe(TYPED, content, preflight_values(TYPED))


def test_param_pairs_refuse_malformed_and_repeated_names() -> None:
    assert parse_param_pairs(["name=a=b", "x="]) == {"name": "a=b", "x": ""}
    with pytest.raises(ValueError, match="NAME=VALUE"):
        parse_param_pairs(["novalue"])
    with pytest.raises(ValueError, match="given twice"):
        parse_param_pairs(["a=1", "a=2"])


def test_preflight_values_use_defaults_then_examples_then_synthetic_values() -> None:
    synthetic = KitRecipeSpec(
        name="s",
        path="recipes/s.json",
        description="d",
        params={
            "path": KitRecipeParamSpec(description="d", required=True),
            "slug": KitRecipeParamSpec(description="d", required=True, pattern=r"^[a-z]$"),
            "count": KitRecipeParamSpec(type="integer", description="d", required=True),
            "kind": KitRecipeParamSpec(type="enum", description="d", values=("b", "a")),
        },
    )
    assert preflight_values(synthetic) == {"path": "example", "slug": "a", "count": 1, "kind": "b"}
    assert preflight_values(DAILY_SPEC) == {"cron": "0 6 * * *", "name": "nightly"}
    impossible = synthetic.model_copy(
        update={"params": {"z": KitRecipeParamSpec(description="d", pattern=r"^Z{9}$")}}
    )
    with pytest.raises(KitRecipeError, match="declare an example"):
        preflight_values(impossible)


def test_a_dependent_recipe_renders_after_its_dependencies() -> None:
    rendered = render_with_dependencies(
        (DEFAULT_SPEC, DAILY_SPEC), "daily", ADOPTION_FILES, {"name": "one", "cron": "0 1 * * *"}
    )
    assert [member["kind"] for member in rendered["members"]] == ["runbook", "trigger"]
    assert rendered["members"][1]["name"] == "acme.one-daily"


# -- kit build checks every recipe ----------------------------------------------------


def _publish(world: _World, request: KitBuildRequest | None = None) -> KitBundle:
    world.author(_claim_type(SEATS, {"type": "integer"}))
    return playbill_api.playbill_kit_build(world.instance_id, request or _adoption_request()).bundle


def test_kit_build_checks_each_recipe_and_carries_recipes_and_scripts(
    worlds: tuple[_World, _World],  # noqa: F811
) -> None:
    publisher, _consumer = worlds
    bundle = _publish(publisher)
    manifest = bundle.manifest
    assert [item.name for item in manifest.recipes] == ["default", "daily"]
    assert [item.name for item in manifest.scripts] == ["report"]
    assert {item.path: item.role for item in manifest.files} == {
        "recipes/daily.json": "recipe",
        "recipes/default.json": "recipe",
        "scripts/report.py": "script",
    }
    assert bundle.file_contents() == ADOPTION_FILES
    plain = playbill_api.playbill_kit_build(
        publisher.instance_id, KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",))
    ).bundle
    assert manifest.content_digest == plain.manifest.content_digest


def test_kit_build_refuses_a_recipe_that_does_not_author(
    worlds: tuple[_World, _World],  # noqa: F811
) -> None:
    publisher, _consumer = worlds
    broken = {
        "kind": "change_set",
        "members": [{"kind": "runbook", "name": "acme.start", "steps": [{"line": "acme.gone"}]}],
    }
    files = {**ADOPTION_FILES, "recipes/default.json": _recipe_bytes(broken)}
    request = _adoption_request(
        files=tuple(KitFileBytes.of(path, content) for path, content in sorted(files.items()))
    )
    with pytest.raises(RequestRefusedError) as refused:
        _publish(publisher, request)
    assert refused.value.error_code == "cruxible.kit.recipe_invalid"
    assert "recipe default (recipes/default.json)" in str(refused.value)
    assert "cruxible.authoring.runbook_line_missing" in str(refused.value)


def test_kit_build_refuses_a_recipe_naming_an_undeclared_param(
    worlds: tuple[_World, _World],  # noqa: F811
) -> None:
    publisher, _consumer = worlds
    files = {
        **ADOPTION_FILES,
        "recipes/daily.json": ADOPTION_FILES["recipes/daily.json"].replace(
            b"{{cron}}", b"{{schedule}}"
        ),
    }
    request = _adoption_request(
        files=tuple(KitFileBytes.of(path, content) for path, content in sorted(files.items()))
    )
    with pytest.raises(RequestRefusedError, match="undeclared parameter\\(s\\) schedule"):
        _publish(publisher, request)


# -- the verified cache ---------------------------------------------------------------


def _installed(bundle: KitBundle, source: str | None = "test") -> InstalledKit:
    return InstalledKit(
        kit_id=bundle.manifest.kit_id,
        version=bundle.manifest.version,
        content_digest=bundle.manifest.content_digest,
        source=source,
        bundle_digest=bundle.manifest.pinned_bundle_digest,
    )


def test_the_cache_keeps_verified_read_only_copies_and_refuses_altered_bytes(
    tmp_path: Path,
) -> None:
    bundle = _adoption_bundle()
    directory = write_kit_cache(bundle, tmp_path)
    # Keyed by the bundle digest the receipt pins, not the shared content digest.
    assert bundle.manifest.pinned_bundle_digest is not None
    assert directory == kit_cache_directory("acme", bundle.manifest.pinned_bundle_digest, tmp_path)
    assert (directory / "scripts/report.py").read_bytes() == SCRIPT.encode()
    assert not os.access(directory / "scripts/report.py", os.W_OK)
    # Writing it again keeps the same copy.
    assert write_kit_cache(bundle, tmp_path) == directory

    cached = load_installed_kit(_installed(bundle), root=tmp_path)
    assert cached.verified_bytes("scripts/report.py") == SCRIPT.encode()
    target = directory / "scripts/report.py"
    target.chmod(stat.S_IWUSR | stat.S_IRUSR)
    target.write_bytes(SCRIPT.encode() + b"\nprint('tampered')\n")
    # What was verified stays what is used; a fresh load refuses the altered file.
    assert cached.verified_bytes("scripts/report.py") == SCRIPT.encode()
    with pytest.raises(KitCacheError, match="sha256 mismatch"):
        load_installed_kit(_installed(bundle), root=tmp_path)


def test_the_cache_refuses_another_releases_copy_and_bad_bundles(tmp_path: Path) -> None:
    bundle = _adoption_bundle()
    other = _installed(bundle).model_copy(update={"content_digest": "sha256:" + "b" * 64})
    with pytest.raises(KitCacheError, match="no cached copy"):
        load_installed_kit(other, root=tmp_path, offline=True)
    damaged = bundle.model_copy(
        update={"files": (KitFileBytes.of("recipes/daily.json", b"{}"), *bundle.files[1:])}
    )
    with pytest.raises(KitCacheError, match="differs from its manifest sha256"):
        write_kit_cache(damaged, tmp_path)


def test_a_missing_cache_is_fetched_from_from_and_must_be_the_installed_release(
    tmp_path: Path,
) -> None:
    from cruxible_client.kits import write_kit_directory

    bundle = _adoption_bundle()
    kit_dir = tmp_path / "acme-1.0.0"
    write_kit_directory(bundle, kit_dir)
    root = tmp_path / "cache"
    cached = load_installed_kit(_installed(bundle, source=None), from_path=str(kit_dir), root=root)
    assert cached.recipe("daily") == DAILY_SPEC
    assert (root / "acme").is_dir()

    newer = tmp_path / "acme-1.1.0"
    write_kit_directory(
        bundle.model_copy(
            update={"manifest": bundle.manifest.model_copy(update={"version": "1.1.0"})}
        ),
        newer,
    )
    with pytest.raises(KitCacheError, match="not the installed acme 1.0.0"):
        load_installed_kit(_installed(bundle), from_path=str(newer), root=tmp_path / "c2")


# -- the CLI, against a stub ----------------------------------------------------------

BASE = ["--server-url", "https://kits.example.test", "--instance-id", "inst", "kit"]


class _StatusClient:
    def __init__(self, kits: tuple[InstalledKit, ...]) -> None:
        self.kits = kits

    def kit_status(self, instance_id: str, *, kit_id: str | None = None) -> KitStatus:
        # kit adopt and kit run name their one kit: the receipt is looked up directly.
        assert kit_id is not None
        return KitStatus(kits=tuple(kit for kit in self.kits if kit.kit_id == kit_id))


def test_kit_build_reads_recipe_and_script_specs_and_their_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".cruxible").mkdir(parents=True)
    for path, content in ADOPTION_FILES.items():
        (workspace / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / path).write_bytes(content)
    (workspace / "recipes.json").write_text(
        json.dumps([DEFAULT_SPEC.model_dump(mode="json"), DAILY_SPEC.model_dump(mode="json")])
    )
    (workspace / "scripts.json").write_text(json.dumps([REPORT_SPEC.model_dump(mode="json")]))
    sent: list[KitBuildRequest] = []

    class BuildClient:
        def build_kit(self, instance_id: str, request: KitBuildRequest) -> KitBuildResult:
            sent.append(request)
            return KitBuildResult(bundle=_adoption_bundle())

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: BuildClient())
    monkeypatch.chdir(workspace)
    result = CliRunner().invoke(
        cli,
        [
            *BASE,
            "build",
            "--id",
            "acme",
            "--version",
            "1.0.0",
            "--owns",
            "acme.",
            "--out",
            str(tmp_path / "kit"),
            "--recipes",
            "recipes.json",
            "--scripts",
            "scripts.json",
        ],
    )
    assert result.exit_code == 0, result.output
    (request,) = sent
    assert request.recipes == (DEFAULT_SPEC, DAILY_SPEC)
    assert request.scripts == (REPORT_SPEC,)
    assert {item.path: item.content for item in request.files} == ADOPTION_FILES
    assert "recipes 2 (kit adopt acme --list), scripts 1 (kit run acme --list)" in result.output
    assert (tmp_path / "kit" / "recipes/daily.json").read_bytes() == ADOPTION_FILES[
        "recipes/daily.json"
    ]


def test_kit_adopt_and_run_refuse_a_kit_that_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: _StatusClient(()))
    for command in (["adopt", "acme", "--list"], ["run", "acme:report"]):
        refused = CliRunner().invoke(cli, [*BASE, *command])
        assert refused.exit_code != 0
        assert "cruxible.kit.not_installed" in refused.stderr
        assert "kit acme is not installed" in refused.stderr


def test_kit_status_counts_recipes_and_scripts_of_a_cached_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _adoption_bundle()
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path))
    write_kit_cache(bundle)

    class AllKits:
        def kit_status(self, instance_id: str) -> KitStatus:
            return KitStatus(kits=(_installed(bundle),))

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: AllKits())
    status = CliRunner().invoke(cli, [*BASE, "status", "--offline"])
    assert status.exit_code == 0, status.output
    assert "  recipes 2 (kit adopt acme --list), scripts 1 (kit run acme --list)" in status.output
    assert "mode read_only is the publisher's declaration, not a sandbox" in status.output


# -- the CLI, end to end against a real daemon ----------------------------------------


@pytest.fixture
def adopter(
    worlds: tuple[_World, _World],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_World, Path, list[str]]:
    """The consumer, wired to the CLI, with the publisher's adoption kit on disk."""

    from cruxible_client.kits import write_kit_directory

    publisher, consumer = worlds
    kit_dir = tmp_path / "kits" / "acme-1.0.0"
    write_kit_directory(_publish(publisher), kit_dir)
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = consumer.http  # type: ignore[assignment]
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    base = [
        "--server-url",
        "http://cruxible",
        "--instance-id",
        consumer.instance_id,
        "--principal-id",
        "operator",
        "kit",
    ]
    return consumer, kit_dir, base


def test_kit_add_caches_the_release_and_adopt_submits_parameterized_recipes(
    adopter: tuple[_World, Path, list[str]], tmp_path: Path
) -> None:
    consumer, kit_dir, base = adopter
    runner = CliRunner()
    added = runner.invoke(cli, [*base, "add", str(kit_dir), "--yes"])
    assert added.exit_code == 0, added.output
    cache_root = tmp_path / "server-state" / "kit-files" / "acme"
    (cached,) = cache_root.iterdir()
    assert (cached / "recipes/daily.json").read_bytes() == ADOPTION_FILES["recipes/daily.json"]

    listed = runner.invoke(cli, [*base, "adopt", "acme", "--list"])
    assert listed.exit_code == 0, listed.output
    assert "acme 1.0.0: 2 recipe(s)" in listed.output
    assert "daily: One daily Trigger of the start Runbook. (after default)" in listed.output
    assert "usage: cruxible kit adopt acme:daily --param name=nightly" in listed.output
    as_json = json.loads(runner.invoke(cli, [*base, "adopt", "acme", "--list", "--json"]).stdout)
    assert [item["name"] for item in as_json["recipes"]] == ["default", "daily"]

    rendered = runner.invoke(cli, [*base, "adopt", "acme:daily", "--param", "name=one", "--render"])
    assert rendered.exit_code == 0, rendered.output
    change_set = json.loads(rendered.stdout)
    assert change_set["members"][0]["name"] == "acme.one-daily"
    assert change_set["members"][0]["schedule"]["expression"] == "0 6 * * *"
    assert change_set["rationale"] == "kit adopt acme:daily (acme 1.0.0)"

    missing = runner.invoke(cli, [*base, "adopt", "acme:daily", "--render"])
    assert missing.exit_code != 0
    assert "missing required parameter name" in missing.stderr
    assert "usage: cruxible kit adopt acme:daily --param name=nightly" in missing.stderr

    preview = runner.invoke(cli, [*base, "adopt", "acme", "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert "acme:default (1.0.0): would submit" in preview.output
    assert "  Runbook: acme.start" in preview.output
    assert (
        f"Next: cruxible --server-url http://cruxible --instance-id {consumer.instance_id} "
        "--principal-id operator kit adopt acme --yes"
    ) in preview.output
    assert f"target: instance {consumer.instance_id} at http://cruxible" in preview.output
    assert "    steps: action:evidence.sweep" in preview.output
    assert "runbooks/acme.start.json" not in consumer.tree()

    adopted = runner.invoke(cli, [*base, "adopt", "acme", "--yes"])
    assert adopted.exit_code == 0, adopted.output
    assert "acme:default (1.0.0): 1 member(s)" in adopted.output
    assert "outcome: accepted" in adopted.output
    for name in ("one", "two"):
        again = runner.invoke(
            cli, [*base, "adopt", "acme:daily", "--param", f"name={name}", "--yes"]
        )
        assert again.exit_code == 0, again.output
        assert "outcome: accepted" in again.output
    tree = consumer.tree()
    assert "runbooks/acme.start.json" in tree
    assert {path for path in tree if path.startswith("triggers/") and "-daily" in path} == {
        "triggers/acme.one-daily.json",
        "triggers/acme.two-daily.json",
    }

    full = runner.invoke(
        cli, [*base, "adopt", "acme:daily", "--param", "name=three", "--no-accept", "--json"]
    )
    assert full.exit_code == 0, full.output
    assert json.loads(full.stdout)["outcome"]


def test_kit_adopt_refuses_a_tampered_recipe_and_refetches_a_missing_cache(
    adopter: tuple[_World, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    _consumer, kit_dir, base = adopter
    runner = CliRunner()
    assert runner.invoke(cli, [*base, "add", str(kit_dir), "--yes"]).exit_code == 0
    status = KitStatus.model_validate(
        json.loads(runner.invoke(cli, [*base, "status", "--offline", "--json"]).stdout)
    )
    (kit,) = [item for item in status.kits if item.kit_id == "acme"]
    assert kit.bundle_digest is not None  # kit add pinned the recipes and scripts
    directory = kit_cache_directory("acme", installed_cache_key(kit))
    recipe = directory / "recipes/default.json"
    recipe.chmod(stat.S_IWUSR | stat.S_IRUSR)
    recipe.write_bytes(recipe.read_bytes().replace(b"evidence.sweep", b"evidence.sweeps"))
    tampered = runner.invoke(cli, [*base, "adopt", "acme", "--render"])
    assert tampered.exit_code != 0
    assert "cruxible.kit.cache_invalid" in tampered.stderr
    assert "sha256 mismatch" in tampered.stderr

    # Gone entirely: refetched from --from (and verified) or from the recorded source.
    shutil.rmtree(directory)
    from_flag = runner.invoke(cli, [*base, "adopt", "acme", "--render", "--from", str(kit_dir)])
    assert from_flag.exit_code == 0, from_flag.output
    shutil.rmtree(directory)
    monkeypatch.chdir(kit_dir.parent)  # the recorded source is the directory's name
    recorded = runner.invoke(cli, [*base, "adopt", "acme", "--render"])
    assert recorded.exit_code == 0, recorded.output
    assert json.loads(recorded.stdout)["members"][0]["name"] == "acme.start"


def test_kit_run_lists_and_runs_the_verified_script_with_the_sdk_environment(
    adopter: tuple[_World, Path, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer, kit_dir, base = adopter
    runner = CliRunner()
    assert runner.invoke(cli, [*base, "add", str(kit_dir), "--yes"]).exit_code == 0

    listed = runner.invoke(cli, [*base, "run", "acme", "--list"])
    assert listed.exit_code == 0, listed.output
    assert "report (read_only): Report." in listed.output
    unnamed = runner.invoke(cli, [*base, "run", "acme"])
    assert unnamed.exit_code == 2 and "name the script" in unnamed.output

    out = tmp_path / "report.json"
    monkeypatch.setenv("KIT_SCRIPT_EXIT", "3")
    ran = runner.invoke(cli, [*base, "run", "acme:report", "--", str(out), "--format", "x"])
    assert ran.exit_code == 3, ran.output
    report = json.loads(out.read_text())
    assert report["args"] == ["--format", "x"]
    assert (report["kit"], report["version"], report["mode"]) == ("acme", "1.0.0", "read_only")
    assert report["instance"] == consumer.instance_id
    # A private, verified snapshot of the kit files, removed when the script exits.
    assert Path(report["files"]).name == "kit"
    assert not Path(report["files"]).exists()
    assert "mode read_only is the publisher's declaration" in listed.output

    unknown = runner.invoke(cli, [*base, "run", "acme:nope"])
    assert unknown.exit_code != 0 and "cruxible.kit.entry_unknown" in unknown.stderr

    status = KitStatus.model_validate(
        json.loads(runner.invoke(cli, [*base, "status", "--offline", "--json"]).stdout)
    )
    (kit,) = [item for item in status.kits if item.kit_id == "acme"]
    script = kit_cache_directory("acme", installed_cache_key(kit)) / "scripts/report.py"
    script.chmod(stat.S_IWUSR | stat.S_IRUSR)
    script.write_text(SCRIPT + "\nraise SystemExit(9)\n")
    out.unlink()
    tampered = runner.invoke(cli, [*base, "run", "acme:report", "--", str(out)])
    assert tampered.exit_code != 0
    assert "sha256 mismatch" in tampered.stderr
    assert not out.exists()


# -- review fixes: pinning, modes, legacy manifests, recipe validation ----------------


def _rebuilt(bundle: KitBundle, script: bytes) -> KitBundle:
    """``bundle`` with the same definitions (same content digest) but another script."""

    files = {**bundle.file_contents(), "scripts/report.py": script}
    return bundle.model_copy(
        update={
            "manifest": bundle.manifest.model_copy(
                update={
                    "files": tuple(
                        kit_file_entry(path, content) for path, content in sorted(files.items())
                    )
                }
            ),
            "files": tuple(
                KitFileBytes.of(path, content) for path, content in sorted(files.items())
            ),
        }
    )


def test_the_bundle_digest_tells_builds_with_the_same_definitions_apart() -> None:
    first = _adoption_bundle()
    second = _rebuilt(first, SCRIPT.encode() + b"\nraise SystemExit(7)\n")
    assert first.manifest.content_digest == second.manifest.content_digest
    assert first.manifest.bundle_digest != second.manifest.bundle_digest
    assert first.manifest.pinned_bundle_digest == first.manifest.bundle_digest
    # A kit without recipes or scripts pins nothing, so its receipt keeps its bytes.
    plain = _bundle().manifest
    assert plain.pinned_bundle_digest is None
    from cruxible_client.contracts.kits import KitReceipt

    receipt = KitReceipt(
        kit_id="acme", version="1.0.0", content_digest=plain.content_digest, owns=(), artifacts=()
    )
    assert "bundle_digest" not in receipt.model_dump(mode="json")


def test_a_rebuilt_bundle_never_replaces_what_the_installed_kit_runs(tmp_path: Path) -> None:
    first = _adoption_bundle()
    second = _rebuilt(first, b"raise SystemExit(7)\n")
    write_kit_cache(first, tmp_path)
    # kit pull of the rebuilt bundle writes its own entry, never the installed one.
    other = write_kit_cache(second, tmp_path)
    installed = _installed(first)
    cached = load_installed_kit(installed, root=tmp_path)
    assert cached.directory != other
    assert cached.verified_bytes("scripts/report.py") == SCRIPT.encode()
    # Fetching the rebuilt bundle for the installed kit refuses: its bundle is not pinned.
    from cruxible_client.kits import write_kit_directory

    rebuilt_dir = tmp_path / "rebuilt"
    write_kit_directory(second, rebuilt_dir)
    with pytest.raises(KitCacheError, match="not the installed acme 1.0.0 bundle"):
        load_installed_kit(installed, from_path=str(rebuilt_dir), root=tmp_path)
    # A receipt that pins no bundle refuses a copy that declares scripts.
    unpinned = installed.model_copy(update={"bundle_digest": None})
    with pytest.raises(KitCacheError, match="pins none"):
        load_installed_kit(unpinned, from_path=str(rebuilt_dir), root=tmp_path / "c2")


def test_kit_pull_of_a_rebuilt_bundle_leaves_the_installed_script_running(
    adopter: tuple[_World, Path, list[str]], tmp_path: Path
) -> None:
    from cruxible_client.kits import resolve_kit, write_kit_directory

    _consumer, kit_dir, base = adopter
    runner = CliRunner()
    assert runner.invoke(cli, [*base, "add", str(kit_dir), "--yes"]).exit_code == 0
    published, _origin = resolve_kit(str(kit_dir))
    rebuilt_dir = tmp_path / "kits" / "acme-rebuilt"
    write_kit_directory(_rebuilt(published, b"raise SystemExit(7)\n"), rebuilt_dir)
    pulled = runner.invoke(cli, [*base, "pull", str(rebuilt_dir), "--out", str(tmp_path / "p")])
    assert pulled.exit_code == 0, pulled.output

    out = tmp_path / "report.json"
    ran = runner.invoke(cli, [*base, "run", "acme:report", "--", str(out)])
    assert ran.exit_code == 0, ran.output
    assert json.loads(out.read_text())["kit"] == "acme"  # script A, not the rebuilt exit 7
    refused = runner.invoke(cli, [*base, "run", "acme:report", "--from", str(rebuilt_dir)])
    assert refused.exit_code != 0
    assert "cruxible.kit.cache_invalid" in refused.stderr
    assert "not the installed acme 1.0.0 bundle" in refused.stderr


def test_a_script_declares_its_mode_explicitly() -> None:
    with pytest.raises(ValidationError, match="mode"):
        KitScriptSpec.model_validate({"name": "r", "path": "scripts/report.py", "description": "d"})
    with pytest.raises(ValidationError, match="read_only"):
        KitScriptSpec.model_validate(
            {"name": "r", "path": "scripts/report.py", "description": "d", "mode": "write"}
        )


def test_a_manifest_from_before_recipes_keeps_a_scripts_file_as_reference() -> None:
    legacy = _bundle().manifest.model_dump(mode="json")
    legacy["files"] = [
        {"path": "recipes/notes.json", "sha256": "sha256:" + "b" * 64, "role": "reference"},
        {"path": "scripts/helper.py", "sha256": "sha256:" + "a" * 64, "role": "reference"},
    ]
    manifest = KitManifest.model_validate(legacy)
    assert {item.role for item in manifest.files} == {"reference"}
    # A declaration that names it still needs the declared role.
    legacy["scripts"] = [
        {"name": "helper", "path": "scripts/helper.py", "description": "d", "mode": "read_only"}
    ]
    with pytest.raises(ValidationError, match="a reference file, not a script file"):
        KitManifest.model_validate(legacy)


@pytest.mark.parametrize("members", [None, 123, []])
def test_a_dependent_recipe_is_validated_on_its_own_before_composition(members: Any) -> None:
    from cruxible_client.contracts.kit_recipes import validate_rendered_recipe

    child = KitRecipeSpec(
        name="child", path="recipes/child.json", description="d", depends_on=("default",)
    )
    contents = {
        **ADOPTION_FILES,
        "recipes/child.json": _recipe_bytes({"kind": "change_set", "members": members}),
    }
    with pytest.raises(KitRecipeError) as refused:
        render_with_dependencies((DEFAULT_SPEC, child), "child", contents)
    assert refused.value.error_code == "cruxible.kit.recipe_invalid"
    assert "recipe child" in str(refused.value)
    # kit adopt applies exactly this validation to what it submits.
    with pytest.raises(KitRecipeError, match="renders an invalid change set"):
        validate_rendered_recipe("child", {"kind": "change_set", "members": members})


def test_kit_build_refuses_a_dependent_recipe_without_its_own_members(
    worlds: tuple[_World, _World],  # noqa: F811
) -> None:
    publisher, _consumer = worlds
    files = {
        **ADOPTION_FILES,
        "recipes/daily.json": _recipe_bytes({"kind": "change_set", "members": None}),
    }
    request = _adoption_request(
        files=tuple(KitFileBytes.of(path, content) for path, content in sorted(files.items()))
    )
    with pytest.raises(RequestRefusedError) as refused:
        _publish(publisher, request)
    assert refused.value.error_code == "cruxible.kit.recipe_invalid"
    assert "recipe daily" in str(refused.value)


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        (r"^(a)\1$", "backreference"),
        ("^(?=a)a$", "lookaround"),
        ("^(?!b)a$", "lookaround"),
        ("^(a)?(?(1)a|b)$", "conditional"),
        ("^(?>a+)$", "atomic group"),
        ("^a++$", "possessive repeat"),
        ("(?i)^a+$", "flag that changes matching"),
        ("^a{0,1500}$", "more than 2048 matcher states"),
        ("^(a{0,99}){0,99}$", "more than 2048 matcher states"),
        ("a" * 300, "at most 256 characters"),
    ],
)
def test_a_param_pattern_the_linear_matcher_cannot_run_refuses_fast(
    pattern: str, message: str
) -> None:
    import time

    started = time.monotonic()
    with pytest.raises(ValidationError, match=message):
        KitRecipeParamSpec(description="d", pattern=pattern, example="a" * 40 + "!")
    assert time.monotonic() - started < 1.0


def test_a_string_param_value_is_bounded() -> None:
    spec = KitRecipeParamSpec(description="d", pattern=r"^[a-z]+$")
    with pytest.raises(ValueError, match="longer than 4096"):
        from cruxible_client.contracts.kit_recipes import parse_param_value

        parse_param_value(spec, "a" * 5000)
    with pytest.raises(ValidationError, match="longer than 4096"):
        KitRecipeParamSpec(description="d", default="a" * 5000)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"kind": "change_set", "members": [{"name": "a {{ b"}]}', "unmatched"),
        ('{"kind": "change_set", "members": [{"name": "a }} b"}]}', "unmatched"),
        ('{"kind": "change_set", "members": [{"name": "{{name}} }}"}]}', "unmatched"),
        ('{"kind": "change_set", "members": [{"a {{": 1}]}', "object key"),
    ],
)
def test_unmatched_placeholder_delimiters_refuse(content: str, message: str) -> None:
    with pytest.raises(KitRecipeError, match=message):
        render_recipe(TYPED, content, preflight_values(TYPED))


def test_an_optional_param_the_recipe_names_declares_a_default() -> None:
    spec = KitRecipeSpec(
        name="o",
        path="recipes/o.json",
        description="d",
        params={"tag": KitRecipeParamSpec(description="d", example="x")},
    )
    content = '{"kind": "change_set", "members": [{"name": "{{tag}}"}]}'
    with pytest.raises(
        KitRecipeError, match="optional parameter\\(s\\) tag that declare no default"
    ):
        render_recipe(spec, content, {"tag": "x"})


def test_usage_lines_shell_quote_every_token() -> None:
    from cruxible_client.contracts.kit_recipes import recipe_usage

    spec = KitRecipeSpec(
        name="q",
        path="recipes/q.json",
        description="d",
        params={
            "title": KitRecipeParamSpec(description="d", required=True, example="my app; rm -rf"),
            "slug": KitRecipeParamSpec(description="d", required=True, pattern=r"^[a-z]$"),
        },
    )
    usage = recipe_usage("acme", spec)
    assert usage == "cruxible kit adopt acme:q --param slug=a --param 'title=my app; rm -rf'"
    import shlex

    assert shlex.split(usage)[-1] == "title=my app; rm -rf"


def test_the_adopt_preview_discloses_mandate_scope_expiry_and_egress() -> None:
    from cruxible_core.cli.commands.playbill import _adopt_member_details

    mandate = {
        "kind": "procedure_mandate",
        "name": "acme.grant",
        "line_name": "acme.scan",
        "grants": "settle",
        "scope": [{"predicate": "acme.seen", "changes": {"create": ["measured"]}}],
        "expires": "P30D",
    }
    rows = _adopt_member_details(mandate)
    assert "grants settle on Line:acme.scan" in rows
    assert any(row.startswith("scope: ") and "acme.seen" in row for row in rows)
    assert "expires: P30D" in rows
    assert "scope: observations" in _adopt_member_details(
        {**mandate, "scope": "observations", "expires": None}
    )
    assert "expires: policy default" in _adopt_member_details({**mandate, "expires": None})
    line = {
        "kind": "line",
        "name": "acme.scan",
        "procedure_name": "acme.scan",
        "bindings": {"fetch": "acme-http#get"},
        "acquisition": {"page.body": {"origins": ["https://api.example.test"]}},
    }
    rows = _adopt_member_details(line)
    assert "providers: fetch=acme-http#get" in rows
    assert "other slots: fitted (each slot's one live operation)" in rows
    assert "egress page.body: https://api.example.test" in rows
    trigger = {
        "kind": "trigger",
        "name": "acme.daily",
        "runbook_name": "acme.start",
        "schedule": {"kind": "cron", "expression": "0 6 * * *"},
    }
    assert _adopt_member_details(trigger) == [
        "schedule: cron (expression 0 6 * * *)",
        "runs: Runbook:acme.start",
    ]


def test_kit_adopt_preserves_selectors_and_from_in_its_rerun_line(
    adopter: tuple[_World, Path, list[str]],
) -> None:
    consumer, kit_dir, base = adopter
    runner = CliRunner()
    assert runner.invoke(cli, [*base, "add", str(kit_dir), "--yes"]).exit_code == 0
    assert runner.invoke(cli, [*base, "adopt", "acme", "--yes"]).exit_code == 0
    preview = runner.invoke(
        cli,
        [*base, "adopt", "acme:daily", "--param", "name=a-b", "--from", str(kit_dir), "--dry-run"],
    )
    assert preview.exit_code == 0, preview.output
    import shlex

    (line,) = [row for row in preview.output.splitlines() if row.startswith("Next: ")]
    argv = shlex.split(line.removeprefix("Next: "))
    assert argv == [
        "cruxible",
        "--server-url",
        "http://cruxible",
        "--instance-id",
        consumer.instance_id,
        "--principal-id",
        "operator",
        "kit",
        "adopt",
        "acme:daily",
        "--param=name=a-b",
        "--from",
        str(kit_dir),
        "--yes",
    ]


def test_from_and_kit_pull_repair_a_damaged_cache_entry(
    adopter: tuple[_World, Path, list[str]], tmp_path: Path
) -> None:
    _consumer, kit_dir, base = adopter
    runner = CliRunner()
    assert runner.invoke(cli, [*base, "add", str(kit_dir), "--yes"]).exit_code == 0
    status = KitStatus.model_validate(
        json.loads(runner.invoke(cli, [*base, "status", "--offline", "--json"]).stdout)
    )
    (kit,) = [item for item in status.kits if item.kit_id == "acme"]
    directory = kit_cache_directory("acme", installed_cache_key(kit))
    recipe = directory / "recipes/default.json"
    recipe.unlink()  # the manifest survives, a file does not
    missing = runner.invoke(cli, [*base, "adopt", "acme", "--render"])
    assert missing.exit_code != 0 and "is missing" in missing.stderr
    repaired = runner.invoke(cli, [*base, "adopt", "acme", "--render", "--from", str(kit_dir)])
    assert repaired.exit_code == 0, repaired.output
    assert recipe.read_bytes() == ADOPTION_FILES["recipes/default.json"]

    script = directory / "scripts/report.py"
    script.chmod(stat.S_IWUSR | stat.S_IRUSR)
    script.write_bytes(b"raise SystemExit(9)\n")
    pulled = runner.invoke(cli, [*base, "pull", str(kit_dir), "--out", str(tmp_path / "p")])
    assert pulled.exit_code == 0, pulled.output
    assert script.read_bytes() == SCRIPT.encode()


def test_the_cache_refuses_a_symbolic_link_in_an_entry(tmp_path: Path) -> None:
    bundle = _adoption_bundle()
    directory = write_kit_cache(bundle, tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_bytes(SCRIPT.encode())  # the same bytes: only the link is wrong
    target = directory / "scripts/report.py"
    target.chmod(stat.S_IWUSR | stat.S_IRUSR)
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(KitCacheError, match="symbolic link"):
        load_installed_kit(_installed(bundle), root=tmp_path)
    # Writing the release again replaces the entry with a real file.
    write_kit_cache(bundle, tmp_path)
    assert not target.is_symlink()
    assert (
        load_installed_kit(_installed(bundle), root=tmp_path).verified_bytes("scripts/report.py")
        == SCRIPT.encode()
    )


def test_a_missing_local_source_asks_for_from_instead_of_a_registry(tmp_path: Path) -> None:
    bundle = _adoption_bundle()
    installed = _installed(bundle, source="acme-gone-1.0.0")
    with pytest.raises(KitCacheError, match="is not on disk here") as refused:
        load_installed_kit(installed, root=tmp_path)
    assert "rerun with --from KIT" in str(refused.value)


def test_kit_build_reads_spec_paths_from_the_workspace_root_and_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".cruxible").mkdir(parents=True)
    for path, content in ADOPTION_FILES.items():
        (workspace / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / path).write_bytes(content)
    nested = workspace / "deep" / "er"
    nested.mkdir(parents=True)
    (nested / "scripts.json").write_text(json.dumps([REPORT_SPEC.model_dump(mode="json")]))
    sent: list[KitBuildRequest] = []

    class BuildClient:
        def build_kit(self, instance_id: str, request: KitBuildRequest) -> KitBuildResult:
            sent.append(request)
            return KitBuildResult(bundle=_adoption_bundle())

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: BuildClient())
    monkeypatch.chdir(nested)  # spec paths are the kit's paths, not relative to here
    result = CliRunner().invoke(
        cli,
        [
            *BASE,
            "build",
            "--id",
            "acme",
            "--version",
            "1.0.0",
            "--owns",
            "acme.",
            "--out",
            str(tmp_path / "kit"),
            "--recipes",
            "-",
            "--scripts",
            "scripts.json",
        ],
        input=json.dumps(
            [DEFAULT_SPEC.model_dump(mode="json"), DAILY_SPEC.model_dump(mode="json")]
        ),
    )
    assert result.exit_code == 0, result.output
    (request,) = sent
    assert {item.path: item.content for item in request.files} == ADOPTION_FILES


# -- the linear pattern matcher ---------------------------------------------------------

ADVERSARIAL_PATTERNS = (
    "(a+)+$",
    "(a|aa)*b",
    "(a?){30}a{30}",
    "a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b",
)


@pytest.mark.parametrize("pattern", ADVERSARIAL_PATTERNS)
def test_backtracking_shapes_match_in_linear_time(pattern: str) -> None:
    import time

    from cruxible_client.contracts.kit_recipes import parse_param_value

    spec = KitRecipeParamSpec(description="d", pattern=pattern)
    for value in ("a" * 29 + "!", "a" * 2000 + "!"):
        started = time.perf_counter()
        with pytest.raises(ValueError, match="does not match"):
            parse_param_value(spec, value)
        assert time.perf_counter() - started < 0.05, (pattern, len(value))


@pytest.mark.parametrize(
    ("pattern", "accepted", "refused"),
    [
        ("(a+)+$", ["a", "aaaa"], ["", "ab", "a\n", "\n"]),
        ("a$\n", ["a\n"], ["a"]),
        ("(a|aa)*b", ["b", "aab", "aaaab"], ["", "aa", "ba"]),
        ("(a?){30}a{30}", ["a" * 30, "a" * 45, "a" * 60], ["a" * 29, "a" * 61]),
        ("a*a*a*b", ["b", "ab", "aaab"], ["", "aaa", "abb"]),
        ("[a-c0-9_]{2,4}", ["a1", "c_9b"], ["a", "abcde", "d1", "A1"]),
        ("[^a-c]x", ["dx", "\nx", "-x"], ["ax", "cx", "x"]),
        (r"\d+\.\w*\s?", ["1.", "42.ab_ ", "7.x\t"], [".", "1", "a.b"]),
        (r"[\D]{2}", ["ab", "a-"], ["a1", "12"]),
        ("^(?:alpha|beta)-[0-9]{1,3}$", ["alpha-1", "beta-999"], ["gamma-1", "beta-1000"]),
        (r"\Afoo\Z", ["foo"], ["foo\n", "fo"]),
        (r"\bfoo\b", ["foo"], ["foox"]),
        ("x*?y+?", ["y", "xxyy"], ["x", ""]),
        (".a", ["ba", "aa"], ["\na", "a"]),
        ("", [""], ["a"]),
    ],
)
def test_the_linear_matcher_agrees_with_re_fullmatch(
    pattern: str, accepted: list[str], refused: list[str]
) -> None:
    import re

    from cruxible_client.contracts._linear_regex import linear_fullmatch

    # ``re`` itself takes minutes on this one (the backtracking this matcher removes).
    check_re = pattern != "(a?){30}a{30}"
    for value in accepted:
        assert not check_re or re.fullmatch(pattern, value) is not None, (pattern, value)
        assert linear_fullmatch(pattern, value), (pattern, value)
    for value in refused:
        assert not check_re or re.fullmatch(pattern, value) is None, (pattern, value)
        assert not linear_fullmatch(pattern, value), (pattern, value)


def test_a_bounded_length_pattern_of_255_compiles_and_matches() -> None:
    import time

    from cruxible_client.contracts._linear_regex import compile_linear
    from cruxible_client.contracts.kit_recipes import parse_param_value

    pattern = r"^[a-z][a-z0-9-]{0,255}$"
    spec = KitRecipeParamSpec(description="d", pattern=pattern)
    assert len(compile_linear(pattern).states) <= 2048
    assert parse_param_value(spec, "a" + "b" * 255) == "a" + "b" * 255
    started = time.perf_counter()
    for value in ("a" + "b" * 256, "a" + "b" * 254 + "!", "1abc"):
        with pytest.raises(ValueError, match="does not match"):
            parse_param_value(spec, value)
    assert time.perf_counter() - started < 0.05


def test_usage_shows_a_placeholder_when_no_candidate_fits_the_pattern() -> None:
    from cruxible_client.contracts.kit_recipes import recipe_usage

    spec = KitRecipeSpec(
        name="y",
        path="recipes/y.json",
        description="d",
        params={
            "job": KitRecipeParamSpec(description="d", required=True, pattern="[0-9]{4}"),
            "slug": KitRecipeParamSpec(description="d", required=True, pattern=r"^[a-z]$"),
        },
    )
    usage = recipe_usage("acme", spec)
    assert usage == "cruxible kit adopt acme:y --param 'job=<job>' --param slug=a"
    assert "VALUE" not in usage


@pytest.mark.parametrize("pattern", [r"\B", r"\b", r"a?\B", r"\Ba?"])
def test_a_word_boundary_on_empty_text_agrees_with_re_fullmatch(pattern: str) -> None:
    import re

    from cruxible_client.contracts._linear_regex import linear_fullmatch

    assert linear_fullmatch(pattern, "") == (re.fullmatch(pattern, "") is not None)
    assert not linear_fullmatch(pattern, "")


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("^(?i:a){0}$", "flag that changes matching"),
        (r"^(a)\1{0}$", "backreference"),
        ("^(?=a){0}$", "lookaround"),
        ("^(a{0,1500}){0}$", "more than 2048 matcher states"),
    ],
)
def test_a_zero_repeat_still_checks_what_it_repeats(pattern: str, message: str) -> None:
    from cruxible_client.contracts._linear_regex import LinearPatternError, compile_linear

    with pytest.raises(LinearPatternError, match=message):
        compile_linear(pattern)
    with pytest.raises(ValidationError, match=message):
        KitRecipeParamSpec(description="d", pattern=pattern)


def test_a_zero_repeat_of_a_runnable_pattern_matches_only_empty() -> None:
    from cruxible_client.contracts._linear_regex import linear_fullmatch

    assert linear_fullmatch("a{0}", "")
    assert not linear_fullmatch("a{0}", "a")
    assert linear_fullmatch("^x(ab){0}y$", "xy")


def test_usage_skips_an_enum_value_too_long_to_pass() -> None:
    from cruxible_client.contracts.kit_recipes import recipe_usage

    long_choice = "z" * 4097
    spec = KitRecipeSpec(
        name="e",
        path="recipes/e.json",
        description="d",
        params={
            "mode": KitRecipeParamSpec(
                description="d", required=True, type="enum", values=(long_choice, "ok")
            ),
        },
    )
    assert recipe_usage("acme", spec) == "cruxible kit adopt acme:e --param mode=ok"


def test_usage_shows_a_placeholder_when_every_enum_value_is_too_long() -> None:
    from cruxible_client.contracts.kit_recipes import recipe_usage

    spec = KitRecipeSpec(
        name="e",
        path="recipes/e.json",
        description="d",
        params={
            "mode": KitRecipeParamSpec(
                description="d", required=True, type="enum", values=("y" * 4097, "z" * 4097)
            ),
        },
    )
    usage = recipe_usage("acme", spec)
    assert usage == "cruxible kit adopt acme:e --param 'mode=<mode>'"
    assert "y" * 10 not in usage


# -- the adopt preview -----------------------------------------------------------------


def test_the_adopt_preview_shows_egress_per_capture_output() -> None:
    from cruxible_core.cli.commands.playbill import _adopt_member_details

    line = {
        "kind": "line",
        "name": "acme.scan",
        "procedure_name": "acme.scan",
        "acquisition": {
            "page.body": {"origins": ["https://api.example.test"]},
            "feed.body": {"max_age": "P1D"},
        },
    }
    rows = _adopt_member_details(line)
    assert "egress page.body: https://api.example.test" in rows
    # A rule without origins is unrestricted, never hidden behind the restricted one.
    assert "egress feed.body: any origin" in rows
    assert "egress other capture outputs: any origin (core default)" in rows
    assert "providers: fitted (each slot's one live operation)" in rows
    bare = _adopt_member_details({**line, "acquisition": {}})
    assert "egress: any origin (no origin rules; core default)" in bare


def test_the_adopt_preview_shows_explicit_and_fitted_provider_slots() -> None:
    from cruxible_core.cli.commands.playbill import _adopt_member_details

    rows = _adopt_member_details(
        {
            "kind": "line",
            "name": "acme.scan",
            "procedure_name": "acme.scan",
            "bindings": {"fetch": "acme-http#get", "parse": "acme-parse"},
        }
    )
    assert "providers: fetch=acme-http#get, parse=acme-parse" in rows
    assert "other slots: fitted (each slot's one live operation)" in rows


def test_the_adopt_preview_tells_mandates_apart_by_condition_and_subject_scope() -> None:
    from cruxible_core.cli.commands.playbill import _adopt_member_details

    mandate = {
        "kind": "procedure_mandate",
        "name": "acme.grant",
        "line_name": "acme.scan",
        "grants": "settle",
        "scope": [{"predicate": "acme.seen", "changes": {"create": ["measured"]}}],
    }
    first = _adopt_member_details(
        {
            **mandate,
            "condition": {"query": "acme.open", "params": {"min": 1}},
            "subject_scope": ["subjects/acme.host/alpha"],
        }
    )
    second = _adopt_member_details(
        {
            **mandate,
            "condition": {"query": "acme.closed"},
            "subject_scope": ["subjects/acme.host/beta"],
        }
    )
    assert first != second
    assert 'condition: {"params":{"min":1},"query":"acme.open"}' in first
    assert 'subject scope: ["subjects/acme.host/alpha"]' in first
    assert 'condition: {"query":"acme.closed"}' in second
    assert 'subject scope: ["subjects/acme.host/beta"]' in second


# -- cache replacement and links -------------------------------------------------------


def test_a_failed_promotion_keeps_the_original_entry_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _adoption_bundle()
    directory = write_kit_cache(bundle, tmp_path)
    # Damage one file so the next write replaces the entry.
    target = directory / "scripts/report.py"
    target.chmod(stat.S_IWUSR | stat.S_IRUSR)
    target.write_bytes(b"print('stale')\n")
    before = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    real_rename = os.rename

    def failing_rename(source: Any, destination: Any, **dir_fds: Any) -> None:
        if Path(source).name.startswith(".staging-"):
            raise OSError("simulated failure promoting the staged entry")
        real_rename(source, destination, **dir_fds)

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(OSError, match="simulated failure"):
        write_kit_cache(bundle, tmp_path)
    monkeypatch.setattr(os, "rename", real_rename)

    after = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    assert after == before  # the original entry is back, byte for byte
    assert sorted(item.name for item in directory.parent.iterdir()) == [directory.name]
    # Still readable (and still refused as altered, as it was before).
    with pytest.raises(KitCacheError, match="sha256 mismatch"):
        load_installed_kit(_installed(bundle), root=tmp_path)
    # A successful write then replaces it and leaves no backup behind.
    write_kit_cache(bundle, tmp_path)
    assert sorted(item.name for item in directory.parent.iterdir()) == [directory.name]
    assert (
        load_installed_kit(_installed(bundle), root=tmp_path).verified_bytes("scripts/report.py")
        == SCRIPT.encode()
    )


def test_a_backup_left_by_a_crash_is_restored_under_the_lock(tmp_path: Path) -> None:
    bundle = _adoption_bundle()
    directory = write_kit_cache(bundle, tmp_path)
    # A writer crashed after moving the entry aside, before publishing its successor.
    os.replace(directory, directory.with_name(f".prev-{directory.name}"))
    cached = load_installed_kit(_installed(bundle), root=tmp_path, offline=True)
    assert cached.verified_bytes("scripts/report.py") == SCRIPT.encode()
    assert sorted(item.name for item in directory.parent.iterdir()) == [directory.name]


def test_the_cache_refuses_a_symbolic_link_at_the_kit_directory(tmp_path: Path) -> None:
    bundle = _adoption_bundle()
    cache = tmp_path / "cache"
    elsewhere = tmp_path / "elsewhere"
    written = write_kit_cache(bundle, elsewhere)
    cache.mkdir()
    (cache / "acme").symlink_to(written.parent, target_is_directory=True)
    outside_before = sorted(str(path) for path in elsewhere.rglob("*"))

    with pytest.raises(KitCacheError, match="symbolic link"):
        load_installed_kit(_installed(bundle), root=cache)
    with pytest.raises(KitCacheError, match="symbolic link"):
        write_kit_cache(bundle, cache)

    assert sorted(str(path) for path in elsewhere.rglob("*")) == outside_before
    assert (cache / "acme").is_symlink()
    assert not (cache / ".locks").exists()


def test_cache_changes_stay_in_the_held_kit_directory_when_its_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the entry's lock is held, staging, the backup, the swap and cleanup are
    made through the kit directory's held descriptor: ``<cache>/<kit_id>`` replaced
    by a link out of the cache afterwards receives nothing."""

    from cruxible_core.cli import kit_cache

    bundle = _adoption_bundle()
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = write_kit_cache(bundle, cache)
    # Damage one file so the next write replaces the entry (backup and swap).
    damaged = directory / "scripts/report.py"
    damaged.chmod(stat.S_IWUSR | stat.S_IRUSR)
    damaged.write_bytes(b"print('stale')\n")
    kit_dir = cache / "acme"
    held = cache / "acme-held"

    def swapped_after_the_checks(target: Path, manifest_bytes: bytes) -> bool:
        os.rename(kit_dir, held)
        kit_dir.symlink_to(outside, target_is_directory=True)
        return False

    monkeypatch.setattr(kit_cache, "_intact", swapped_after_the_checks)
    write_kit_cache(bundle, cache)

    assert list(outside.iterdir()) == []
    assert sorted(item.name for item in held.iterdir()) == [directory.name]
    entry = held / directory.name
    assert (entry / "scripts/report.py").read_bytes() == SCRIPT.encode()
    assert (entry / "cruxible-kit.json").exists()


def test_kit_status_refuses_an_invalid_kit_id_query_cleanly(
    worlds: tuple[_World, _World],  # noqa: F811
) -> None:
    _publisher, consumer = worlds
    response = consumer.http.get(
        f"/api/v1/{consumer.instance_id}/kits", params={"kit_id": "SECURITY"}
    )
    assert 400 <= response.status_code < 500, response.text
    assert "kit id must be lowercase" in response.text
    fine = consumer.http.get(f"/api/v1/{consumer.instance_id}/kits", params={"kit_id": "absent"})
    assert fine.status_code == 200, fine.text
