"""A copied template host is the host a fresh `playbill/init` leaves.

`_playbill_http` serves most server tests from a copy of one template host per
process. The copy must register the same instance at its own location, carry
the same trust root, and reopen to the same replay-verified state.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cruxible_client.contracts.types import PlaybillTrustRoot
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._world_templates import WorldTemplates, copy_template
from tests.test_runtime.test_world_templates import _names_template, _observable
from tests.test_server.conftest import _build_http_world


def _registered(state: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(state / "daemon" / "registry.db") as connection:
        return connection.execute("SELECT instance_id, location FROM instances").fetchall()


def _opened(state: Path, instance_id: str) -> PlaybillInstance:
    trust = PlaybillTrustRoot.model_validate_json(
        (state / "trust" / f"{instance_id}.json").read_bytes()
    )
    return PlaybillInstance.open(state / "instances" / instance_id, trust_root=trust)


def test_a_copied_host_registers_and_reopens_to_the_fresh_host(tmp_path: Path) -> None:
    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    template = templates.template(("private-http",), lambda root: _build_http_world(root, False))
    assert template is not None
    destination = tmp_path / "copy"
    destination.mkdir()
    copied = copy_template(template, destination)
    assert copied is not None
    instance_id = template.value.instance_id
    fresh_state = template.root / "server-state"
    copy_state = copied / "server-state"

    assert _registered(copy_state) == [(instance_id, str(copy_state / "instances" / instance_id))]
    assert (copy_state / "trust" / f"{instance_id}.json").read_bytes() == (
        fresh_state / "trust" / f"{instance_id}.json"
    ).read_bytes()
    assert _observable(_opened(copy_state, instance_id)._recovered) == _observable(
        _opened(fresh_state, instance_id)._recovered
    )
    assert _names_template(copied, template.root) == []
    assert (copied / template.value.reviewer_key).is_file()
