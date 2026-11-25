"""CLI and review frictions from the 0.6.0 security-audit benchmark.

- `cruxible write --example` prints a worked FILE that validates as one;
- a reviewed write's approve call never names its author or the caller;
- `based_on` pins show in `proposal review` text and `get CLM --detail proof`;
- `cruxible query NAME --param k=v` binds the positional NAME;
- `principal add --commit --yes` composes;
- `cruxible python -` runs a script that imports the CLI's own SDK.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from click.testing import CliRunner

import cruxible_client
import cruxible_core
from cruxible_client import contracts
from cruxible_client.authoring.examples import WRITE_FILE_EXAMPLE as WRITE_EXAMPLE
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.get_text import claim_proof_summary, claim_proof_summary_lines
from cruxible_client.contracts.text_display import render_review_summary, render_write_outcome
from cruxible_core.cli.commands.playbill import _WriteFileV1
from cruxible_core.cli.main import cli
from cruxible_core.service.authoring.write_verbs import (
    _independent_signer,
    _render_proposal_call,
)
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.proposals.summary import service_review_summary
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._pair_world import write
from tests.test_consumers.test_next_based_on import _world

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
_READER = BodyAccessContext(principal_id="owner", can_read_body=True)


@pytest.fixture(autouse=True)
def _isolated_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))


# -- write --example ----------------------------------------------------------------


def test_write_example_is_a_valid_write_file_covering_every_authoring_shape() -> None:
    result = CliRunner().invoke(cli, ["write", "--example"])
    assert result.exit_code == 0, result.output
    assert result.output == WRITE_EXAMPLE
    payload = json.loads(result.output)

    schema = json.loads(CliRunner().invoke(cli, ["write", "--schema"]).output)
    jsonschema.validate(payload, schema)
    parsed = _WriteFileV1.model_validate(payload)

    assert parsed.because and parsed.docs[0].op == "append"
    create, observed, sectioned = parsed.changes
    assert create.op == "create"
    assert observed.op == "set" and observed.subject.created == create.label  # type: ignore[union-attr]
    assert observed.role == "normative"  # type: ignore[union-attr]
    assert observed.evidence.kind == "file"  # type: ignore[union-attr]
    assert observed.based_on  # type: ignore[union-attr]
    assert sectioned.evidence.kind == "section"  # type: ignore[union-attr]
    assert len(WRITE_EXAMPLE.splitlines()) <= 22


def test_write_help_names_example_and_schema() -> None:
    result = CliRunner().invoke(cli, ["write", "--help"])
    assert result.exit_code == 0
    assert "--example" in result.output and "--schema" in result.output
    both = CliRunner().invoke(cli, ["write", "--example", "--schema"])
    assert both.exit_code != 0 and "not both" in both.output


# -- independent approval signer ----------------------------------------------------


def test_a_reviewed_write_names_only_an_independent_signer() -> None:
    assert _independent_signer(("reviewer",), author="owner") == "reviewer"
    # Several approvers, none, or only the author: a placeholder, never the author.
    for approvers in (("alice", "bob"), (), ("owner",)):
        assert _independent_signer(approvers, author="owner") == "<INDEPENDENT_REVIEWER>"
    placeholder = "<INDEPENDENT_REVIEWER>"
    assert _render_proposal_call("cli", "approve", "P", signer=placeholder) == (
        "cruxible proposal approve P --signer-id '<INDEPENDENT_REVIEWER>'"
    )
    # Never the caller's own configured signer, which an omitted signer means.
    assert 'signer_id="<INDEPENDENT_REVIEWER>"' in _render_proposal_call(
        "mcp", "approve", "P", signer=placeholder
    )
    assert 'cx.signer("<INDEPENDENT_REVIEWER>")' in _render_proposal_call(
        "sdk", "approve", "P", signer=placeholder
    )


def test_an_accept_never_write_does_not_suggest_its_author_as_signer(tmp_path: Path) -> None:
    instance, _scope, _judgment, occurrence = _world(tmp_path)
    kept = write(
        instance,
        {"op": "set", "subject": occurrence, "field": "scope", "value": "optional"},
        accept="never",
        surface="cli",
    )
    assert kept.status == "awaiting_approval" and kept.approval is not None
    assert kept.approval.approve is not None
    assert "--signer-id owner" not in kept.approval.approve
    assert "owner" not in kept.approval.eligible_approvers


# -- based_on in review and proof ---------------------------------------------------


def test_based_on_pins_show_in_proposal_review_and_proof_summary(tmp_path: Path) -> None:
    instance, scope, judgment, occurrence = _world(tmp_path)
    exposure = (
        service_playbill_get(
            instance, request=GetRequest(ref=judgment), access=_READER
        ).card.subject  # type: ignore[union-attr]
    )
    kept = write(
        instance,
        {
            "op": "set",
            "subject": exposure,
            "field": "justification",
            "value": "component_not_present",
            "based_on": [scope],
        },
        accept="never",
    )
    assert kept.proposal is not None
    summary = service_review_summary(
        instance, proposal_id=kept.proposal.proposal_id, access=_READER
    )
    (change,) = [item for item in summary.changes if item.change.field == "justification"]
    (pin,) = change.based_on
    assert pin.claim == scope and pin.subject == occurrence and pin.field == "scope"
    text = render_review_summary(summary, command="cruxible proposal review P")
    assert f"based on {scope} {occurrence} scope" in text

    proof = service_playbill_get(
        instance, request=GetRequest(ref=judgment, detail="proof"), access=_READER
    ).proof
    assert proof is not None
    assert claim_proof_summary(proof)["based_on"] == [{"claim": scope}]
    lines = claim_proof_summary_lines(proof, based_on_context={scope: (occurrence, "scope")})
    assert f"based on: {scope} {occurrence} scope" in lines


def test_a_dry_run_shows_the_named_role_and_based_on_and_review_shows_the_role(
    tmp_path: Path,
) -> None:
    instance, scope, judgment, _occurrence = _world(tmp_path)
    exposure = (
        service_playbill_get(
            instance, request=GetRequest(ref=judgment), access=_READER
        ).card.subject  # type: ignore[union-attr]
    )
    change = {
        "op": "set",
        "subject": exposure,
        "field": "justification",
        "value": "component_not_present",
        "based_on": [scope],
    }
    kept = write(instance, change, accept="never")
    assert kept.proposal is not None
    summary = service_review_summary(
        instance, proposal_id=kept.proposal.proposal_id, access=_READER
    )
    (reviewed,) = [item for item in summary.changes if item.change.field == "justification"]
    assert reviewed.role is not None
    text = render_review_summary(summary, command="cruxible proposal review P")
    # The role shows in review even when it is the common normative one.
    assert f"role {reviewed.role}" in text
    assert text.count(f"based on {scope}") == 1

    preview = write(instance, {**change, "role": reviewed.role}, dry_run=True)
    (planned,) = [item for item in preview.changes if item.field == "justification"]
    assert planned.role == reviewed.role and planned.based_on == (scope,)
    rendered = render_write_outcome(preview)
    assert f"role {reviewed.role}" in rendered and f"based on {scope}" in rendered
    # A write that names no role shows none: the default says nothing new.
    plain = write(instance, change, dry_run=True)
    assert all(item.role is None for item in plain.changes)


# -- query NAME --param -------------------------------------------------------------


class _QueryStub:
    def __init__(self) -> None:
        self.requests: list[QueryRequest] = []

    def query(self, instance_id: str, *, request: QueryRequest) -> contracts.QueryResultRecord:
        self.requests.append(request)
        return contracts.QueryResultRecord(
            kind=request.kind,
            columns=(),
            rows=(),
            receipt=contracts.QueryReceipt(
                mode="named" if request.name else "inline",
                spec_digest="sha256:" + "5" * 64,
                coordinate=COORDINATE,
                evaluation_time=datetime(2026, 11, 24, tzinfo=UTC),
            ),
        )


def test_a_positional_query_name_binds_params(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _QueryStub()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: stub)
    prefix = ["--server-url", "http://server", "--instance-id", "inst_query", "query"]
    result = CliRunner().invoke(
        cli, [*prefix, "security.code_facts", "--param", "repo=acme", "--param", "limit=3"]
    )
    assert result.exit_code == 0, result.output
    request = stub.requests[0]
    assert request.name == "security.code_facts" and request.kind is None
    assert request.params == {"repo": "acme", "limit": 3}
    # A compact query keeps its kind: shaping options say KIND.
    assert CliRunner().invoke(cli, [*prefix, "security.finding", "--select", "a"]).exit_code == 0
    assert stub.requests[1].kind == "security.finding" and stub.requests[1].name is None


# -- principal add --commit --yes ---------------------------------------------------


def test_principal_add_commit_and_yes_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    dry_runs: list[object] = []

    class StubClient:
        def orient(self, instance_id: str, **values: Any) -> Any:
            return SimpleNamespace(principals=(), truncated=False, next_cursor=None)

        def propose_principal_change(
            self, instance_id: str, *, dry_run: object = None, **_: object
        ) -> contracts.ProposalInspection:
            dry_runs.append(dry_run)
            return contracts.ProposalInspection(
                proposal={"proposal_id": "sha256:" + "5" * 64},
                accepted_coordinate=COORDINATE,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    base = ["--server-url", "https://p.example.test", "--instance-id", "inst_p", "principal"]
    result = CliRunner().invoke(
        cli,
        [*base, "add", "reviewer", "--key-dir", str(tmp_path / "custody"), "--commit", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert dry_runs == [False]
    dry = CliRunner().invoke(
        cli,
        [*base, "add", "other", "--key-dir", str(tmp_path / "c2"), "--dry-run", "--yes"],
    )
    assert dry.exit_code != 0 and "drop --dry-run" in dry.output


# -- cruxible python ----------------------------------------------------------------


def test_cruxible_python_runs_stdin_against_the_clis_own_sdk(tmp_path: Path) -> None:
    roots = [
        str(Path(module.__file__ or "").resolve().parent.parent)
        for module in (cruxible_core, cruxible_client)
    ]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(roots))
    script = (
        "import cruxible_client\n"
        "from cruxible_client import Cruxible\n"
        "print('sdk', cruxible_client.__version__, Cruxible.__name__)\n"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cruxible_core.cli.main import cli; cli()",
            "--no-workspace",
            "--server-url",
            "http://127.0.0.1:9",
            "python",
            "-",
        ],
        cwd=tmp_path,
        input=script,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"sdk {cruxible_client.__version__} Cruxible"


def test_cruxible_python_help_shows_the_ways_to_run_and_the_stub() -> None:
    result = CliRunner().invoke(cli, ["python", "--help"])
    assert result.exit_code == 0
    assert "cruxible python -c" in result.output
    assert "cruxible python - <<'PY'" in result.output
    assert ".cruxible/world.pyi" in result.output
