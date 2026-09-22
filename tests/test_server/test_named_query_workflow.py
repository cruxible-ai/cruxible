"""SDK and declarative CLI/MCP author, submit, accept and run the same queries."""

import json

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.serialization import load_ssh_private_key

from cruxible_client import contracts as api
from cruxible_client.authoring.examples import (
    query_claims_by_type_example,
    query_ontology_example,
    query_procedures_example,
)
from cruxible_client.authoring.sdk import Playbill, Proposal
from cruxible_client.authoring.signing import LocalEd25519ApprovalSigner
from cruxible_client.contracts.authoring.inputs import ChangeSetInput, ClaimTypeInput
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from tests.test_authoring.test_authoring_change_set_intents import _predicate_type


@pytest.mark.parametrize("surface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize(
    "example", [query_ontology_example, query_procedures_example, query_claims_by_type_example]
)
def test_named_query_complete_workflow(playbill_http, tmp_path, monkeypatch, surface, example):
    transport, instance_id, private_key = playbill_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = transport
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pb = Playbill._from_client(client, instance_id=instance_id, workspace=workspace)
    definition = example()
    ordinary = example is query_claims_by_type_example
    input = (
        ChangeSetInput(
            kind="change_set",
            members=(
                ClaimTypeInput(
                    kind="claim_type", claim_type=_predicate_type("project.work_item.status")
                ),
                definition,
            ),
        )
        if ordinary
        else definition
    )
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    monkeypatch.setattr(
        handlers, "_dispatch_remote_or_local", lambda remote, local, **kw: remote(client)
    )

    def invoke(*args):
        result = CliRunner().invoke(
            cli, ["--server-url", "http://cruxible", "--instance-id", instance_id, *args, "--json"]
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    if surface == "sdk":
        if ordinary:
            change = pb.changes()
            ref = change.claim_type(_predicate_type("project.work_item.status"))
            change.query_definition(definition, vocabulary=(ref,))
            intent = change.prepare()
        else:
            intent = pb.query_definition(definition=definition).prepare()
        assert not intent.refused, intent.diagnostics
        proposal = intent.submit().proposal
        assert proposal is not None
    else:
        if surface == "cli":
            payload_file = tmp_path / "query.json"
            payload_file.write_text(input.model_dump_json())
            compiled = invoke("playbill", "authoring", "compile", str(payload_file))
            assert compiled["verdict"] == "passed", compiled
            submitted = invoke(
                "playbill", "authoring", "submit", compiled["certificate"]["intent_id"]
            )
        else:
            compiled = handlers.handle_playbill_authoring_compile(
                instance_id, input.model_dump(mode="json"), intent_id=None
            )
            assert compiled.verdict == "passed", compiled
            submitted = handlers.handle_playbill_authoring_submit(
                instance_id, compiled.certificate["intent_id"]
            ).model_dump(mode="json")
        proposal = Proposal(pb, submitted["status"]["proposal_id"])
    key = load_ssh_private_key(private_key.read_bytes(), password=None)
    signer = LocalEd25519ApprovalSigner(
        signer_id="reviewer",
        private_key_path=private_key,
        public_key=key.public_key().public_bytes_raw().hex(),
    )
    reviewed = proposal.review()
    proposal.approve(signer=signer, reviewed=reviewed)
    assert pb.accept(proposal.proposal_id).status == "accepted"
    name = definition.query_definition.identity.name
    if surface == "cli":
        result = api.PlaybillQueryRun.model_validate(invoke("playbill", "query", "run", name))
    elif surface == "mcp":
        result = handlers.handle_playbill_run_query(
            instance_id, name, parameters=None, evaluation_time=None, budgets=None
        )
    else:
        result = pb.run_query(name)
    assert result.result.verdict == "completed"
    assert result.receipt.definition_digest == result.definition_digest
    if not ordinary:
        assert isinstance(result.artifact_definitions, tuple)
        if example is query_procedures_example:
            assert result.artifact_definitions == ()
