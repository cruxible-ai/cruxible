"""Egress selects one exact mandate at the proposal door's accepted coordinate."""

import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_core.compiler.compiler import (
    artifact_codec_for_compiler,
    current_compiler_coordinate,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.indexes.typed_sqlite import insert_members, parse_static_owners
from cruxible_core.indexes.typed_state import TypedStateReader, insert_owners, schema_sql
from cruxible_core.procedures.egress import (
    TerminalAuthorityRefusal,
    require_procedure_mandate_at_head,
)
from cruxible_core.procedures.terminal_services import ProposalTerminalAdapter
from tests.test_procedures.test_procedure_effectful_terminals import (
    NOW,
    _admission,
    _effectful_request,
    _item,
    _runtime_mandate,
)
from tests.test_procedures.test_procedure_execution import _digest

PATH = "subjects/project.work_item/wi-1.json"


@pytest.fixture
def projection_factory():
    connections = []

    def create(mandates):
        coordinate = AcceptedProjectionCoordinate(
            instance_id="instance-a",
            repository_path="/accepted/ledger.git",
            git_object_format="sha1",
            git_oid="e" * 40,
            semantic_root=_digest("callback-semantic-root"),
            generation_root=_digest("callback-generation-root"),
            compiler=current_compiler_coordinate(),
        )
        sources = {
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)
            for mandate in mandates
        }
        connection = sqlite3.connect(":memory:")
        connections.append(connection)
        connection.executescript(schema_sql())
        insert_members(connection, sources, "sha1")
        insert_owners(
            connection,
            parsed=parse_static_owners(sources, accepted=coordinate),
            blobs=sources,
            codec=artifact_codec_for_compiler(coordinate.compiler),
        )
        blobs = {
            oid: (path, sources[path])
            for path, oid in connection.execute("SELECT path,git_blob_oid FROM members")
        }
        reads = []

        def read_blob(oid):
            path, content = blobs[oid]
            reads.append(path)
            return content

        typed = TypedStateReader(connection, coordinate, SimpleNamespace(read_blob=read_blob))
        return SimpleNamespace(typed=typed, accepted=coordinate, reads=reads)

    yield create
    for connection in connections:
        connection.close()


def _request(admission, mandate):
    return _effectful_request(
        "propose_change_set",
        admission=admission,
        item=_item(PATH),
        target_paths=(PATH,),
        mandate_digest=procedure_mandate_digest(mandate).tagged,
    )


def test_egress_reads_only_the_exact_mandate_source(tmp_path, projection_factory, record_property):
    admission = _admission(tmp_path)
    mandate = _runtime_mandate(admission, namespace=("subjects",))
    request = _request(admission, mandate)
    counts = []
    for unrelated in (0, 500):
        projection = projection_factory(
            [mandate]
            + [
                mandate.model_copy(
                    update={
                        "identity": ArtifactIdentity(kind="ProcedureMandate", name=f"other-{n}")
                    }
                )
                for n in range(unrelated)
            ]
        )
        steps = 0

        def count():
            nonlocal steps
            steps += 1
            return 0

        projection.typed.connection.set_progress_handler(count, 1)
        try:
            assert (
                require_procedure_mandate_at_head(
                    request, admission=admission, projection=projection
                )
                == mandate
            )
        finally:
            projection.typed.connection.set_progress_handler(None, 0)
        assert projection.reads == [procedure_mandate_path(mandate.identity.name)]
        assert projection.typed.work["members_read"] == 1
        counts.append(steps)
    record_property("exact_mandate_sql_steps", counts)
    assert counts[1] <= counts[0] * 1.1 + 20, counts


@pytest.mark.parametrize("change", ["retired", "replaced", "absent", "procedure", "expired"])
def test_egress_rechecks_current_exact_contract(tmp_path, projection_factory, change):
    admission = _admission(tmp_path)
    mandate = _runtime_mandate(admission, namespace=("subjects",))
    bound = mandate
    if change == "retired":
        mandate = mandate.model_copy(
            update={"lifecycle": mandate.lifecycle.model_copy(update={"state": "retired"})}
        )
        bound = mandate  # Its exact retired digest must reach the mandate law.
    elif change == "replaced":
        mandate = mandate.model_copy(update={"namespace": ("claims",)})
    elif change == "procedure":
        mandate = mandate.model_copy(
            update={
                "procedure": mandate.procedure.model_copy(
                    update={"artifact_digest": _digest("other")}
                )
            }
        )
        bound = mandate
    elif change == "expired":
        mandate = mandate.model_copy(update={"expires_at": NOW - timedelta(days=1)})
        bound = mandate
    projection = projection_factory([] if change == "absent" else [mandate])
    with pytest.raises(TerminalAuthorityRefusal) as caught:
        require_procedure_mandate_at_head(
            _request(admission, bound), admission=admission, projection=projection
        )
    assert caught.value.codes == (
        "procedure_mandate_expired" if change == "expired" else "procedure_mandate_superseded",
    )
    assert caught.value.repair_kind == "author_successor"
    assert len(projection.reads) == (0 if change in {"absent", "replaced"} else 1)


def test_egress_refuses_source_that_differs_from_selected_digest(tmp_path, projection_factory):
    admission = _admission(tmp_path)
    mandate = _runtime_mandate(admission, namespace=("subjects",))
    projection = projection_factory([mandate])
    projection.typed.source = lambda identity: mandate.model_copy(update={"namespace": ("claims",)})
    with pytest.raises(ProjectionIntegrityError, match="exact digest"):
        require_procedure_mandate_at_head(
            _request(admission, mandate), admission=admission, projection=projection
        )


@pytest.mark.parametrize("present", [True, False])
def test_proposal_callback_binds_its_coordinate_without_reading_the_head_tree(
    tmp_path, projection_factory, present
):
    admission = _admission(tmp_path)
    mandate = _runtime_mandate(admission, namespace=("subjects",))
    projection = projection_factory([mandate] if present else [])
    request = _request(admission, mandate)
    assert projection.accepted.git_oid != request.accepted_coordinate.git_oid
    bound = []

    @contextmanager
    def bind(coordinate):
        assert coordinate is projection.accepted
        bound.append(coordinate)
        yield projection

    class NoTreeReads(dict):
        def items(self):
            pytest.fail("mandate authorization enumerated the head tree")

    class Authorized(Exception):
        pass

    def submit(**kwargs):
        kwargs["authorize"](projection.accepted, NoTreeReads())
        raise Authorized

    adapter = ProposalTerminalAdapter(service=SimpleNamespace(submit=submit), bind_projection=bind)
    with pytest.raises(Authorized if present else TerminalAuthorityRefusal) as caught:
        adapter.deliver(
            request=request,
            admission=admission,
            candidate_tree={},
            changed_paths=(PATH,),
            accepted_mandates={request.procedure_mandate_digest: mandate},
        )
    assert bound == [projection.accepted]
    if not present:
        assert caught.value.codes == ("procedure_mandate_superseded",)
