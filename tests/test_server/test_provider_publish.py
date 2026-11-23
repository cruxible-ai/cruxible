"""End to end: publish one ``@derive`` file, run it, edit it, publish ``--move``, approve once.

Under ``--require-independent-approval`` iterating on a pure single-file
provider is one command and one approval: ``provider publish FILE --move``
proposes the new build and the Line moved onto it together. Needs a
``cruxible-provider-runtime`` with the single-file packager (0.6.0 and later)
installed here and a providers checkout (``CRUXIBLE_PROVIDERS_CHECKOUT``) whose
runtime wheel the publish transfers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.provider_publish import RUNTIME_PYTHON_ENV, publish_provider_file
from cruxible_core.errors import ConfigError
from tests.support.provider_installation import (
    local_call_capability_files,
    local_call_contract_files,
)
from tests.test_server.test_provider_installation import (  # noqa: F401 (fixture)
    _accept,
    _run_call,
    _run_line,
    installer_http,
)

pytestmark = pytest.mark.executor_only

SOURCE = '''"""Adds {step} to n."""

from cruxible_provider_runtime import derive


@derive(
    "local.increment",
    serves="local.increment",
    input_contract="local.increment.value@1",
    output_contract="local.increment.value@1",
)
def increment(n: int) -> dict:
    """Adds {step} to n."""
    return {{"n": n + {step}}}
'''


def _require_packager() -> None:
    try:
        import cruxible_provider_runtime.single_file  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip("the installed cruxible-provider-runtime has no single-file packager (0.6.0)")


@pytest.fixture
def publisher(installer_http, provider_checkout, monkeypatch):  # noqa: F811
    _require_packager()
    # The packager runs in this interpreter, whose runtime built the wheel transferred.
    monkeypatch.setenv(RUNTIME_PYTHON_ENV, sys.executable)
    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    runtime = next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    return http, client, instance_id, reviewer, runtime


@pytest.mark.parametrize("installer_http", [True], indirect=True)
def test_publish_run_edit_and_move_is_one_command_and_one_approval(
    publisher, tmp_path: Path
) -> None:
    from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

    http, client, instance_id, reviewer, runtime = publisher
    # The capability a slot names, and its Contract: the publish reads the pool here.
    _accept(
        http,
        instance_id,
        reviewer,
        {**local_call_contract_files(), **local_call_capability_files()},
        name="local-call-capability",
    )
    source = tmp_path / "local_inc.py"
    source.write_text(SOURCE.format(step=1))

    first = publish_provider_file(client, instance_id, source, runtime_wheel=runtime)
    package = first.package
    assert (package.distribution, package.module_name) == ("local-inc", "local_inc")
    assert package.operations == ("local.increment",)
    assert package.carries == ("local.increment.value@1",)
    assert package.version.startswith("0.0.0.dev")
    assert first.install.status == "awaiting_approval", first.install
    assert first.install.moved == ()
    _approve_and_activate(http, instance_id, reviewer, first.install.proposal_id)

    # The same file is the same version, wheel and build: nothing to propose.
    same = publish_provider_file(client, instance_id, source, runtime_wheel=runtime)
    assert same.package.version == package.version
    assert same.install.build == first.install.build
    assert same.install.installation_id == first.install.installation_id
    assert same.install.status == "ready" and same.install.registered

    line = "test.published-call-line"
    assert _run_call(
        http,
        instance_id,
        reviewer,
        build=first.install.build,
        value={"n": 2},
        name="published-call",
    ) == {"n": 3}

    source.write_text(SOURCE.format(step=2))
    moved = publish_provider_file(client, instance_id, source, runtime_wheel=runtime, move=True)
    assert moved.package.version != package.version
    assert moved.install.build != first.install.build
    assert moved.install.status == "awaiting_approval", moved.install
    (repin,) = moved.install.moved
    assert (repin.identity, repin.action) == (f"Line:{line}", "successor")
    assert (repin.from_build, repin.to_build) == (first.install.build, moved.install.build)
    review = client.review_proposal(instance_id, moved.install.proposal_id)
    actions = {(item.kind, item.action) for item in review.definitions}
    assert {("Provider", "created"), ("Line", "revised")} <= actions, actions
    # The Line still runs its accepted build until the one approval lands both.
    assert _run_line(instance_id, line) == {"n": 3}
    _approve_and_activate(http, instance_id, reviewer, moved.install.proposal_id)
    assert _run_line(instance_id, line) == {"n": 4}


@pytest.mark.parametrize(
    ("body", "kind", "fragment"),
    [
        ("raise RuntimeError('boom')\n", "import failed", "RuntimeError: boom"),
        ("x = 1\n", "invalid package", "defines no @derive operation"),
        (
            "from __future__ import annotations\n"
            "from cruxible_provider_runtime import derive\n\n"
            "@derive('a.b', serves='a.b', input_contract='a.in@1', output_contract='a.out@1')\n"
            "def f(n: Undefined) -> dict:\n"
            '    """Does f."""\n'
            "    return {}\n",
            "invalid derive",
            "name 'Undefined' is not defined",
        ),
        (
            # A declaration the decorator refuses raises while the file is imported.
            "from cruxible_provider_runtime import derive\n\n"
            "@derive('a.b', serves='a.b', input_contract='a.in@1', output_contract='a.out@1')\n"
            "def f(n: int) -> dict:\n"
            "    return {}\n",
            "import failed",
            "DeriveDefinitionError",
        ),
    ],
)
def test_a_file_the_packager_refuses_is_a_typed_error_before_anything_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, kind: str, fragment: str
) -> None:
    _require_packager()
    monkeypatch.setenv(RUNTIME_PYTHON_ENV, sys.executable)
    source = tmp_path / "broken_provider.py"
    source.write_text(body)

    class Untouched:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"the daemon was called ({name}) for a refused file")

    with pytest.raises(ConfigError) as refused:
        publish_provider_file(Untouched(), "inst", source)  # type: ignore[arg-type]
    assert str(refused.value).startswith(f"broken_provider.py: {kind}: ")
    assert fragment in str(refused.value)
