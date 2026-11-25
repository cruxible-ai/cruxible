"""``cruxible provider publish FILE``: names, versions, the shape pool and packager errors.

Everything here runs without the provider runtime's packager; the end-to-end
publish, run, edit, ``--move`` and one approval is
``tests/test_server/test_provider_publish.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_core.cli import provider_publish as publish
from cruxible_core.errors import ConfigError

INC = {
    "artifact_format": "cruxible-contract-v1",
    "name": "local.increment.value",
    "version": 1,
    "fields": {"n": {"type": {"kind": "int"}}},
}


def test_the_default_name_is_the_canonical_stem() -> None:
    assert publish.default_distribution_name(Path("stock_signals.py")) == "stock-signals"
    assert publish.default_distribution_name(Path("Stock.Signals.py")) == "stock-signals"
    assert publish.canonical_distribution_name("stock-signals") == "stock-signals"
    with pytest.raises(ConfigError, match="not canonical; use 'stock-signals'"):
        publish.canonical_distribution_name("Stock_Signals")
    with pytest.raises(ConfigError, match="not a distribution name"):
        publish.default_distribution_name(Path("-.py"))


def test_an_explicit_version_must_be_normalized() -> None:
    assert publish.normalized_version("1.2.0") == "1.2.0"
    with pytest.raises(ConfigError, match="not normalized; use '1.2.0rc1'"):
        publish.normalized_version("1.2.0-RC1")
    with pytest.raises(ConfigError, match="not a version"):
        publish.normalized_version("one")


def test_the_content_version_names_one_build_per_distinct_input() -> None:
    from packaging.version import Version

    pool = {"contracts": [INC], "capture_kinds": [], "built_against": []}
    first = publish.content_version(
        b"x = 1\n", distribution="local-inc", module_name="local_inc", pool=pool
    )
    assert first == publish.content_version(
        b"x = 1\n", distribution="local-inc", module_name="local_inc", pool=pool
    )
    assert str(Version(first)) == first and first.startswith("0.0.0.dev")
    assert Version(first).is_devrelease
    edits = {
        publish.content_version(
            b"x = 2\n", distribution="local-inc", module_name="local_inc", pool=pool
        ),
        publish.content_version(
            b"x = 1\n", distribution="other", module_name="local_inc", pool=pool
        ),
        publish.content_version(
            b"x = 1\n",
            distribution="local-inc",
            module_name="local_inc",
            pool={**pool, "contracts": []},
        ),
    }
    assert first not in edits and len(edits) == 3


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("import_failed", "ZeroDivisionError: division by zero"),
        ("invalid_derive", "derive 'x' takes no positional parameter"),
        ("invalid_package", "module 'p' defines no @derive operation"),
    ],
)
def test_the_packagers_typed_error_is_a_config_error(
    tmp_path: Path, kind: str, message: str
) -> None:
    document = json.dumps({"error": {"kind": kind, "message": message}})
    command = (sys.executable, "-c", f"import sys; print({document!r}); sys.exit(1)")
    with pytest.raises(ConfigError) as refused:
        publish.run_packager(command, (), file=tmp_path / "p.py", cwd=tmp_path)
    assert str(refused.value) == f"p.py: {kind.replace('_', ' ')}: {message}"


def test_a_packager_that_does_not_answer_names_the_runtime_it_needs(tmp_path: Path) -> None:
    command = (sys.executable, "-c", "import sys; sys.stderr.write('No module named x'); exit(1)")
    with pytest.raises(ConfigError, match="cruxible-provider-runtime>=0.6.0.*No module named x"):
        publish.run_packager(command, (), file=tmp_path / "p.py", cwd=tmp_path)


def test_the_configured_runtime_interpreter_comes_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(publish.RUNTIME_PYTHON_ENV, sys.executable)
    monkeypatch.setattr(publish, "_importable", lambda python: python == sys.executable)
    assert publish.packager_command(runtime_wheel=None, offline=False) == (
        sys.executable,
        "-m",
        "cruxible_provider_runtime.single_file",
    )
    monkeypatch.setattr(publish, "_importable", lambda _python: False)
    with pytest.raises(ConfigError, match="cannot import cruxible_provider_runtime.single_file"):
        publish.packager_command(runtime_wheel=None, offline=False)
    monkeypatch.delenv(publish.RUNTIME_PYTHON_ENV)
    wheel = tmp_path / "cruxible_provider_runtime-0.6.0-py3-none-any.whl"
    command = publish.packager_command(runtime_wheel=wheel, offline=True)
    assert command[1:5] == ("run", "--no-project", "--offline", "--with")
    assert command[5] == str(wheel.resolve())


class _Client:
    """Accepted envelopes by reference; anything else is not found, as ``get`` says."""

    def __init__(self, envelopes: dict[str, dict[str, Any]]) -> None:
        self.envelopes = envelopes
        self.reads: list[str] = []

    def get(self, _instance_id: str, *, request: Any) -> Any:
        self.reads.append(request.ref)
        if request.ref not in self.envelopes:
            error = LookupError(request.ref)
            error.error_code = "cruxible.get.ref_not_found"  # type: ignore[attr-defined]
            raise error
        return SimpleNamespace(proof={"envelope": self.envelopes[request.ref]})


def test_the_pool_is_what_the_operations_reach_from_the_instance_under_declares() -> None:
    row = {
        "artifact_format": "cruxible-contract-v1",
        "name": "feed.row",
        "version": 1,
        "fields": {"id": {"type": {"kind": "string"}}},
    }
    output = {
        "artifact_format": "cruxible-contract-v1",
        "name": "feed.output",
        "version": 1,
        "fields": {
            "doc": {"type": {"kind": "capture", "capture_kind": "feed.json"}},
            "inc": {"type": {"kind": "contract", "contract": {"name": INC["name"], "version": 1}}},
        },
    }
    kind = {
        "identity": {"kind": "CaptureContract", "name": "feed.json"},
        "decoding": {"format": "json"},
        "layout": {"records": {"rows": {"at": "/rows/*", "row": "feed.row@1"}}},
    }
    client = _Client(
        {
            "Contract:feed.output@1": output,
            "Contract:local.increment.value@1": INC,
            "Contract:feed.row@1": row,
            "CaptureContract:feed.json": kind,
            "Contract:unrelated@1": INC,
        }
    )
    given_input = {**INC, "name": "feed.input", "description": "supplied"}
    given = publish._Pool(contracts={("feed.input", 1): given_input})
    operations = [
        {
            "in": "feed.input@1",
            "out": "feed.output@1",
            "refusals": {"gone": {"contract": "feed.missing@1"}},
        }
    ]
    pool = publish.instance_pool(client, "inst", operations, given).document()  # type: ignore[arg-type]
    assert [item["name"] for item in pool["contracts"]] == [
        "feed.input",
        "feed.output",
        "feed.row",
        "local.increment.value",
    ]
    assert pool["contracts"][0] == given_input  # --declares wins; it was never read
    assert pool["capture_kinds"] == [
        {"name": "feed.json", "decoding": kind["decoding"], "layout": kind["layout"]}
    ]
    assert "Contract:feed.input@1" not in client.reads
    assert "Contract:unrelated@1" not in client.reads
    assert "Contract:feed.missing@1" in client.reads  # absent here: the packager says so


def test_publish_renders_the_build_the_move_and_the_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.contracts.provider_installation import (
        CarriedMandateV1,
        ProviderInstallResult,
        ProviderRepinV1,
    )
    from cruxible_core.cli.commands import playbill
    from cruxible_core.cli.main import cli

    file = tmp_path / "local_inc.py"
    file.write_text("x = 1\n")
    seen: dict[str, Any] = {}
    install = ProviderInstallResult(
        installation_id="sha256:" + "1" * 64,
        provider_id="local-inc",
        build="local-inc/abc",
        status="awaiting_approval",
        installed=True,
        registered=False,
        proposal_id="prop-1",
        moved=(
            ProviderRepinV1(
                identity="Line:inc",
                from_build="local-inc/old",
                to_build="local-inc/abc",
                action="successor",
            ),
        ),
        automation_carries=(
            CarriedMandateV1(
                identity="ProcedureMandate:inc-settle",
                line="Line:inc",
                old_line_digest="sha256:" + "2" * 64,
                old_artifact_digest="sha256:" + "3" * 64,
                old_grant="settle",
                new_grant="settle",
                loses_settle=False,
                retained_scope=("inc.value",),
                carries_line_enablement=True,
            ),
        ),
    )

    def fake_publish(_client: Any, _instance_id: str, path: Path, **options: Any) -> Any:
        seen.update(options, file=path)
        return publish.PublishResult(
            package=publish.PublishedPackage(
                file=str(path),
                distribution="local-inc",
                version="0.0.0.dev7",
                module_name="local_inc",
                wheel="local_inc-0.0.0.dev7-py3-none-any.whl",
                operations=("local.increment",),
            ),
            install=install,
        )

    monkeypatch.setattr(publish, "publish_provider_file", fake_publish)
    monkeypatch.setattr(playbill, "_server_call", lambda call, command_name: call(object(), "inst"))
    runner = CliRunner()
    result = runner.invoke(cli, ["provider", "publish", str(file), "--move", "--only", "Line:inc"])
    assert result.exit_code == 0, result.output
    assert seen["move"] is True and seen["only"] == ("Line:inc",)
    assert "Built local-inc 0.0.0.dev7 (local_inc.py): local.increment" in result.output
    assert "local-inc/abc: awaiting_approval" in result.output
    assert "successor: Line:inc: local-inc/old -> local-inc/abc" in result.output
    assert "retains observation settle on inc.value; retains Line enablement" in result.output
    assert "Proposal: prop-1" in result.output
    as_json = runner.invoke(cli, ["provider", "publish", str(file), "--json"])
    assert as_json.exit_code == 0, as_json.output
    document = json.loads(as_json.output)
    assert document["package"]["version"] == "0.0.0.dev7"
    assert document["install"]["moved"][0]["identity"] == "Line:inc"
    only_alone = runner.invoke(cli, ["provider", "publish", str(file), "--only", "Line:inc"])
    assert only_alone.exit_code != 0 and "requires --move" in only_alone.output
