"""The end-to-end skeleton: real KEV and EPSS bytes through web.fetch to Claims.

The gate every later slice keeps green (D-R27). Recorded REAL bytes (the CISA
KEV catalog, the FIRST EPSS API) are replayed through the real ``web.fetch``
adapter of the providers checkout, bound to a Line's slot:

    Line -> call (web.fetch, replay) -> Capture (bytes == the origin body)
         -> extract (JSON records) -> claims -> absence (inverse, mirror)
         -> propose (Mandates propose-only) -> run result (completeness)

Every written value carries an evidence receipt naming the Capture digest, the
extract spec digest and the record locator. The second half of the file holds
the runtime guard (c): admitting a Capture never parses its content.

Fixtures are in ``fixtures/`` (``exchanges.json`` names each one's origin).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput, SubjectInput
from cruxible_client.contracts.captures import (
    capture_contract_path,
    parse_capture_envelope,
    render_capture_contract,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_evidence import CaptureBasis, ExtractedBasis
from cruxible_client.contracts.claim_types import ClaimInverse, claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
)
from cruxible_client.contracts.query.authored import QuerySpecInput
from cruxible_client.contracts.shapes import contract_digest, parse_contract, parse_type
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from tests.core_support._identity_support import accept_tree
from tests.core_support._light_capture_support import JSON_DECODING, http_contract
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.support.capabilities import accept_head_capabilities
from tests.support.claim_types import claim_type as build_claim_type
from tests.support.mandates import procedure_mandate
from tests.support.operation_world import (
    NOW,
    Invoker,
    Operator,
    actor,
    author,
    author_line,
    author_procedure,
    build_files,
)
from tests.support.procedures import boundary_contract, contract_files, procedure_limits
from tests.support.procedures import procedure_spec as build_spec
from tests.support.provider_checkout import require_provider_checkout

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXCHANGES = {
    item["id"]: item for item in json.loads((FIXTURES / "exchanges.json").read_text())["exchanges"]
}
KEV_URL = EXCHANGES["kev-catalog"]["url"]
EPSS_URL = EXCHANGES["epss-api"]["url"]

VULN = "security.vulnerability"
CATALOG = "security.catalog"
LISTED_IN = VULN + ".listed_in"
DATE_ADDED = VULN + ".kev_date_added"
SCORE = VULN + ".epss_score"
PERCENTILE = VULN + ".epss_percentile"
SCORE_DATE = VULN + ".epss_date"
KEV_KIND = "test.kev-catalog"
EPSS_KIND = "test.epss-scores"
#: The derived kind a derive's narrowed view of the catalog is taken under (D-R37).
KEV_VIEW_KIND = "test.kev-view"
WATCHED = (
    "CVE-2021-44228",
    "CVE-2023-4863",
    "CVE-2014-0160",
    "CVE-2017-0144",
    "CVE-2019-0708",
    "CVE-2023-44487",
    "CVE-2022-22965",
    "CVE-2021-26855",
    "CVE-2018-13379",
    # Not in the catalog: an expected Subject the complete read proves unlisted.
    "CVE-2024-3094",
)
DELISTED = "CVE-2018-13379"
UNLISTED = "CVE-2024-3094"

KEV_ROW = boundary_contract("test.kev-row", {"date_added": "date"})
EPSS_ROW = boundary_contract(
    "test.epss-row",
    {"score": "decimal{0..1}", "percentile": "decimal{0..1}", "date": "date"},
)
WATCH_ROW = boundary_contract(
    "test.watched-row", {"subject_kind": "string", "subject_id": "string", "cve_id": "string"}
)
ACCESS = BodyAccessContext(principal_id="skeleton", can_read_body=True)


# -- the real web.fetch, replaying recorded exchanges -------------------------------


def _web_fetch() -> Any:
    """The providers checkout's own ``web.fetch`` adapter (never a copy of it)."""

    checkout = require_provider_checkout()  # the skeleton runs the real web.fetch
    package = checkout / "packages/cruxible-provider-web/src/cruxible_provider_web"
    if not package.is_dir():
        pytest.fail(f"{checkout} carries no cruxible-provider-web package")
    if "cruxible_provider_web" not in sys.modules:
        # Loaded from the checkout by location; the import path is left alone.
        spec = importlib.util.spec_from_file_location(
            "cruxible_provider_web",
            package / "__init__.py",
            submodule_search_locations=[str(package)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["cruxible_provider_web"] = module
        spec.loader.exec_module(module)
    from cruxible_provider_web.fetch import WebFetch

    return WebFetch


class Replay:
    """Answers web.fetch's real client from recorded exchanges, by URL."""

    def __init__(self, **routes: str) -> None:
        self.routes = dict(routes)
        self.requests: list[str] = []

    def body(self, exchange_id: str) -> bytes:
        return (FIXTURES / EXCHANGES[exchange_id]["body"]).read_bytes()

    def client_factory(self) -> Callable[..., Any]:
        import httpx
        from cruxible_provider_web.http import RecordingClient

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            answer = self.routes[str(request.url)]
            if answer.startswith("redirect:"):
                return httpx.Response(302, headers={"location": answer.removeprefix("redirect:")})
            if answer.startswith("bytes:"):
                return httpx.Response(
                    200,
                    headers={"content-type": "application/octet-stream"},
                    content=base64.b64decode(answer.removeprefix("bytes:")),
                )
            exchange = EXCHANGES[answer]
            return httpx.Response(
                exchange["status"], headers=exchange["headers"], content=self.body(answer)
            )

        def factory(recorder: Any, *, url: str, timeout_seconds: float) -> Any:
            del url
            return RecordingClient(
                recorder, timeout_seconds=timeout_seconds, transport=httpx.MockTransport(handle)
            )

        return factory

    def invoker(self) -> Invoker:
        web_fetch = _web_fetch()
        from cruxible_provider_runtime.egress import EgressRecorder
        from cruxible_provider_runtime.protocol import Budgets
        from cruxible_provider_runtime.provider_api import ProviderRunContext

        def output(context: Any) -> dict[str, Any]:
            result = web_fetch(client_factory=self.client_factory())(
                ProviderRunContext(
                    run_id=context.run_id,
                    input=dict(context.input),
                    operation="web.fetch",
                    operation_signature_digest="sha256:" + "9" * 64,
                    build_digest="sha256:" + "b" * 64,
                    implementation_digest="sha256:" + "a" * 64,
                    declared_endpoints=("dynamic:target-from-run-input",),
                    budgets=Budgets(wall_clock_seconds=30, output_bytes=4_000_000),
                    coordinates={},
                    capture_contract=None,
                    secrets={},
                    egress=EgressRecorder(),
                )
            )
            assert result.status == "ok", result
            return dict(result.output)

        return Invoker(output=output)


# -- the world ------------------------------------------------------------------------


def _claim_type(predicate: str, value: str, **changes: Any) -> Any:
    from cruxible_client.contracts.shapes import EnumType

    typed = parse_type(value)
    schema: dict[str, Any] = {"type": "string"}
    if isinstance(typed, EnumType):
        schema["enum"] = list(typed.members)
    return build_claim_type(
        **{
            "predicate": predicate,
            "allowed_subject_kinds": [VULN],
            "value": typed,
            "value_type": schema,
            "permitted_roles": ["observation"],
            "default_role": "observation",
            "revision_evidence": "replace",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="captured",
                        claim_roles=("observation",),
                        capture_contracts=(EPSS_KIND, KEV_KIND, KEV_VIEW_KIND),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            ),
            **changes,
        }
    )


def _key_type(predicate: str, kind: str) -> Any:
    return _claim_type(
        predicate,
        "string",
        allowed_subject_kinds=[kind],
        evidence_requirement="none",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=()),
    )


def _web_provider_files(checkout: Path) -> tuple[dict[str, bytes], Any]:
    """The package's shipped Contracts and a build of its registration, as installed."""

    from cruxible_core.providers.package_metadata import read_package_registration

    root = checkout / "packages/cruxible-provider-web/src/cruxible_provider_web"
    members: dict[str, bytes] = {}
    digests: dict[tuple[str, int], str] = {}
    for path in sorted((root / "contracts").rglob("*.json")):
        member = "contracts/" + path.relative_to(root / "contracts").as_posix()
        contract = parse_contract(path.read_bytes(), path=member)
        members[member] = path.read_bytes()
        digests[(contract.name, contract.version)] = contract_digest(contract).tagged
    registration = read_package_registration(root)
    build = registration.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=registration.manifest.distribution.name,
            version=registration.manifest.distribution.version,
            filename="cruxible_provider_web-skeleton-py3-none-any.whl",
            sha256="sha256:" + "5e" * 32,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain="cruxible-provider-web",
        operations=registration.pinned_operations(lambda ref: digests.get((ref.name, ref.version))),
    )
    return members, build


def _fetch_step(alias: str, kind: str) -> dict[str, Any]:
    return {
        "call": alias,
        "slot": "fetch",
        "captures": {"capture": kind},
        "input": {
            "url": "$params.url",
            "logical_source": "$params.logical_source",
            "expected_format": "$params.expected_format",
            "max_bytes": 1_048_576,
        },
    }


def _spec(name: str, steps: list[dict[str, Any]]) -> Any:
    return build_spec(
        name,
        reference_text=True,
        input={},
        output={},
        # Typed exactly as web.fetch.request@1 types the fields they fill.
        params={
            "url": 'string{pattern: "^[Hh][Tt][Tt][Pp][Ss]?://\\\\S+$"}',
            "logical_source": 'string{pattern: "^[a-z][a-z0-9_.-]{0,255}$"}',
            "expected_format": "enum[auto, bytes, csv, html, json, text]",
        },
        slots={"fetch": {"capability": "web.fetch@1"}},
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=1_048_576, read_rows=500),
        steps=steps,
    )


def kev_spec() -> Any:
    return _spec(
        "test.kev-skeleton",
        [
            _fetch_step("catalog", KEV_KIND),
            {"read": "watched", "query": "test.watched-vulnerabilities"},
            {
                "extract": "entries",
                "capture": "$steps.catalog.capture",
                "records": "/vulnerabilities/*",
                "row": KEV_ROW.ref.spelling,
                "key": {"cve_id": {"at": "cveID", "type": f"key_part<{VULN}:cve_id>"}},
                "duplicates": "refuse_key",
                "fields": {"date_added": {"at": "dateAdded", "parse": "date"}},
                "header": {"catalog_version": {"at": "/catalogVersion"}},
            },
            {
                "id": "write",
                "for_each": "$steps.entries.records",
                "subject": {
                    "kind": VULN,
                    "key": {"cve_id": "$item.key.cve_id"},
                    "create": "never",
                    "within": "$steps.watched.rows",
                },
                "expect": {"$ref": "step", "step": "watched", "path": ["rows"]},
                "claims": [
                    {
                        "type": LISTED_IN,
                        "value": {"kind": CATALOG, "key": {"catalog_id": "cisa-kev"}},
                        "when_absent": "inverse",
                        "rationale": "The CISA KEV catalog lists this vulnerability.",
                    },
                    {
                        "type": DATE_ADDED,
                        "value": "$item.fields.date_added",
                        "when_absent": "mirror",
                    },
                ],
            },
        ],
    )


def epss_spec() -> Any:
    return _spec(
        "test.epss-skeleton",
        [
            _fetch_step("scores_api", EPSS_KIND),
            {"read": "watched", "query": "test.watched-vulnerabilities"},
            {
                "extract": "scores",
                "capture": "$steps.scores_api.capture",
                "records": "/data/*",
                "row": EPSS_ROW.ref.spelling,
                "key": {"cve_id": {"at": "cve", "type": f"key_part<{VULN}:cve_id>"}},
                "fields": {
                    "score": {"at": "epss", "parse": "decimal"},
                    "percentile": {"at": "percentile", "parse": "decimal"},
                    "date": {"at": "date", "parse": "date"},
                },
            },
            {
                "id": "write",
                "for_each": "$steps.scores.records",
                "subject": {
                    "kind": VULN,
                    "key": {"cve_id": "$item.key.cve_id"},
                    "create": "never",
                    "within": "$steps.watched.rows",
                },
                "claims": [
                    {"type": SCORE, "value": "$item.fields.score"},
                    {"type": PERCENTILE, "value": "$item.fields.percentile"},
                    {"type": SCORE_DATE, "value": "$item.fields.date"},
                ],
            },
        ],
    )


def _install(
    instance: Any,
    spec: Any,
    *,
    line: str,
    parameters: Mapping[str, Any],
    origins: tuple[str, ...] | None = None,
    authority: str = "propose",
) -> None:
    author_procedure(instance, spec, name=line + "-procedure")
    author_line(
        instance,
        name=line,
        procedure=spec.procedure,
        parameters=dict(parameters),
        max_authority=authority,
        acquisition=None if origins is None else {"catalog.capture": {"origins": list(origins)}},
        label=line,
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(line)
    accepted = parse_line_spec(tree[path], path=path)
    # The standing propose grant; a settle grant is authored on top (see below).
    mandate = procedure_mandate(
        line,
        namespace=("claims", "subjects"),
        line=ArtifactPin(
            role="line", target=accepted.identity, artifact_digest=line_spec_digest(accepted).tagged
        ),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
        name=line + "-mandate",
    )


def _derived_kind() -> Any:
    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )

    return lower_capture_contract_input(
        CaptureContractInputRecord(
            name=KEV_VIEW_KIND,
            shape="http_response",
            grade="derived",
            replay="attested",
            max_bytes=1_048_576,
            decoding=JSON_DECODING,
        ),
        named=True,
    )


def _kev_kind(access: str) -> Any:
    kind = http_contract(name=KEV_KIND, decoding=JSON_DECODING)
    return kind if access == "open" else kind.model_copy(update={"body_access": access})


def skeleton_world(tmp_path: Path, *, kev_access: str = "open") -> Any:
    from tests.core_support._support import initialize_local

    checkout = require_provider_checkout()  # the skeleton runs the real web.fetch
    instance, _owner = initialize_local(tmp_path)
    members, build = _web_provider_files(checkout)
    kinds = (
        _kev_kind(kev_access),
        http_contract(name=EPSS_KIND, decoding=JSON_DECODING),
        _derived_kind(),
    )
    inverse = ClaimInverse(key={"kind": "object"}, evidence_rules=("captured",))
    types = (
        _claim_type(
            LISTED_IN,
            f"subject<{CATALOG}>",
            cardinality="many",
            inverse=inverse,
            resolution_policy=ClaimResolutionPolicy(
                cardinality="many", eligible_verdicts=("supported",), selector="all"
            ),
        ),
        _claim_type(DATE_ADDED, "date"),
        _claim_type(SCORE, "decimal{0..1}"),
        _claim_type(PERCENTILE, "decimal{0..1}"),
        _claim_type(SCORE_DATE, "date"),
        _key_type(VULN + ".cve_id", VULN),
        _key_type(CATALOG + ".catalog_id", CATALOG),
    )
    accept_tree(instance, members, name="web-contracts")
    accept_head_capabilities(
        instance,
        ("web.fetch", "web.fetch.request@1", "web.fetch.result@1", "external_read"),
        (
            "web.fetch.pages",
            "web.fetch.pages.request@1",
            "web.fetch.pages.result@1",
            "external_read",
        ),
        ("workspace.file", "workspace.file.request@1", "workspace.file.result@1", "pure"),
        name="web-capabilities",
    )
    accept_tree(instance, build_files(build), name="web-build")
    accept_tree(
        instance,
        {
            **{capture_contract_path(k.identity.name): render_capture_contract(k) for k in kinds},
            **contract_files(KEV_ROW, EPSS_ROW, WATCH_ROW),
            subject_kind_path(VULN): render_subject_kind(
                keyed_subject_kind(VULN, (VULN + ".cve_id",))
            ),
            subject_kind_path(CATALOG): render_subject_kind(
                keyed_subject_kind(CATALOG, (CATALOG + ".catalog_id",))
            ),
            **{claim_type_path(t.predicate): render_claim_type(t) for t in types},
        },
        name="skeleton-vocabulary",
    )
    subjects = [(CATALOG, {"catalog_id": "cisa-kev"})] + [(VULN, {"cve_id": c}) for c in WATCHED]
    for index, (kind, key) in enumerate(subjects):
        author(
            instance,
            SubjectInput(kind="subject", subject_kind=kind, key=key, rationale="Watched."),
            name=f"subject-{index}",
        )
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": "test.watched-vulnerabilities",
                    "from": {"v": VULN},
                    "row": WATCH_ROW.ref.spelling,
                    "fields": ["v.subject_kind", "v.subject_id", "v.cve_id"],
                }
            ),
        ),
        name="watched-query",
    )
    return instance


def run(instance: Any, replay: Replay, *, line: str, minute: int = 0) -> Any:
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_completeness_transfer import _Clock

    return service_run_playbill_line(
        instance,
        path_identity_digest=line,
        request=LineRunRequest(line=line),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW + timedelta(minutes=minute)),
        provider_runtime_operator=Operator(replay.invoker()),  # type: ignore[arg-type]
    )


def proposed(instance: Any, state: Any) -> list[Any]:
    from tests.test_procedures.test_claims_terminal import candidate_claims

    return candidate_claims(instance, state)


def activate(instance: Any, state: Any) -> None:
    from tests.test_procedures.test_claims_terminal import activate as activate_proposal

    activate_proposal(instance, state)


def _by(claims: list[Any]) -> dict[tuple[str, str], Any]:
    return {
        (
            c.statement.subject.artifact_path.rsplit("/", 1)[-1].removesuffix(".json"),
            c.statement.predicate,
        ): c
        for c in claims
    }


def _cve(cve: str) -> str:
    return cve.lower()


def _envelope(instance: Any, capture_digest: str) -> Any:
    return parse_capture_envelope(instance.body_store().read(capture_digest, access=ACCESS))


def _assert_payload_capture(instance: Any, capture_digest: str, origin: bytes, url: str) -> None:
    """R1: the Capture's bytes are the origin body; its metadata is the coordinate."""

    envelope = _envelope(instance, capture_digest)
    assert envelope.commitment.digest == "sha256:" + hashlib.sha256(origin).hexdigest()
    assert instance.body_store().read(envelope.commitment.digest, access=ACCESS) == origin
    coordinate = envelope.source.coordinate
    assert coordinate["url"] == url and coordinate["final_url"] == url
    assert coordinate["status"] == 200
    assert coordinate["content_type"].startswith("application/json")
    assert envelope.source.coordinate_type == "http-response-v1"


def pinned_extract_digest(instance: Any, procedure: str, node_id: str) -> str:
    """The digest of the accepted Procedure's own extract node, as receipts cite it."""

    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path
    from cruxible_client.contracts.procedures.extract import extract_spec_digest
    from cruxible_client.contracts.procedures.models import ExtractNode

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path(procedure)
    accepted = parse_procedure(tree[path], path=path)
    (node,) = (
        item
        for item in accepted.definition.nodes
        if isinstance(item, ExtractNode) and item.node_id == node_id
    )
    return extract_spec_digest(node)


def _extracted(claim: Any, capture_digest: str, spec_digest: str) -> ExtractedBasis:
    receipt = claim.backing.procedure_evidence
    assert receipt.law == "claim-evidence-v2"
    value = receipt.value
    assert isinstance(value, ExtractedBasis), value
    assert value.capture_digest == capture_digest
    assert value.extract_spec_digest == spec_digest
    assert value.record_locator
    return value


# -- the skeleton ---------------------------------------------------------------------


#: The KEV Line's settle grant (B4c): per ClaimType and change kind, the bases
#: it settles on. listed_in creates on the record (extracted) or on the complete
#: read's absence (an inverse for an expected, unlisted CVE), and revises to its
#: inverse on absence; the mirrored date creates extracted and retires on absence.
KEV_SETTLE_SCOPE: dict[str, dict[str, Any]] = {
    LISTED_IN: {
        "create": {"basis": ["extracted", "absence"]},
        "revise": {"basis": ["absence"]},
    },
    DATE_ADDED: {"create": {"basis": ["extracted"]}, "retire": {"basis": ["absence"]}},
}


def grant_settle(instance: Any, *, line: str, scope: Mapping[str, Mapping[str, Any]]) -> None:
    from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
    from cruxible_client.contracts.authoring.models import MandateScopeAuthoring

    author(
        instance,
        ProcedureMandateInput(
            kind="procedure_mandate",
            name=line + "-settle",
            line_name=line,
            grants="settle",
            namespace=("claims",),
            valid_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=30),
            subjects="expected",
            scope=tuple(
                MandateScopeAuthoring.model_validate({"claim_type": t, "changes": changes})
                for t, changes in scope.items()
            ),
        ),
        name=line + "-settle-grant",
    )


def _summaries(state: Any, kind: str) -> list[Any]:
    return [node.summary for node in state.nodes if node.summary and node.summary.kind == kind]


def test_kev_and_epss_flow_from_recorded_bytes_to_claims(tmp_path: Path) -> None:
    instance = skeleton_world(tmp_path)
    kev = {"url": KEV_URL, "logical_source": "cisa.kev.catalog", "expected_format": "json"}
    epss = {"url": EPSS_URL, "logical_source": "first.epss", "expected_format": "json"}
    _install(
        instance,
        kev_spec(),
        line="test.kev-line",
        parameters=kev,
        origins=("https://www.cisa.gov",),
        authority="settle",
    )
    grant_settle(instance, line="test.kev-line", scope=KEV_SETTLE_SCOPE)
    # EPSS stays propose-only: its changes are outside every settle grant.
    _install(instance, epss_spec(), line="test.epss-line", parameters=epss)

    # 1. KEV: one Capture of the catalog; listed_in for the nine listed watched
    #    CVEs, an inverse for the one the complete read proves unlisted.
    replay = Replay(**{KEV_URL: "kev-catalog"})
    first = run(instance, replay, line="test.kev-line")
    assert first.status == "succeeded", first.terminal
    assert replay.requests == [KEV_URL]
    assert first.completeness is None  # complete: nothing observed incomplete
    claims = _by(proposed(instance, first))
    listed = [cve for cve in WATCHED if cve != UNLISTED]
    assert set(claims) == {
        *((_cve(c), LISTED_IN) for c in WATCHED),
        *((_cve(c), DATE_ADDED) for c in listed),
    }
    capture = claims[(_cve(listed[0]), DATE_ADDED)].backing.procedure_evidence.value.capture_digest
    kev_spec_digest = pinned_extract_digest(instance, "test.kev-skeleton", "entries")
    _assert_payload_capture(instance, capture, replay.body("kev-catalog"), KEV_URL)
    catalog = json.loads(replay.body("kev-catalog"))
    locators = {
        r["cveID"]: f"/vulnerabilities/{i}" for i, r in enumerate(catalog["vulnerabilities"])
    }
    for cve in listed:
        date = claims[(_cve(cve), DATE_ADDED)]
        receipt = _extracted(date, capture, kev_spec_digest)
        assert receipt.record_locator == locators[cve]
        assert receipt.field_path.endswith("/dateAdded")
        record = catalog["vulnerabilities"][int(locators[cve].rsplit("/", 1)[1])]
        assert date.statement.object.value == record["dateAdded"]
        positive = claims[(_cve(cve), LISTED_IN)]
        assert positive.statement.object.kind == "subject"
        # A literal keeps the record's extracted lineage (D-R4).
        assert _extracted(positive, capture, kev_spec_digest).record_locator == locators[cve]
    unlisted = claims[(_cve(UNLISTED), LISTED_IN)]
    assert unlisted.statement.object.kind == "none"
    witnessed = unlisted.backing.procedure_evidence
    assert isinstance(witnessed.value, CaptureBasis) and witnessed.value.capture_digest == capture
    assert witnessed.absence is not None and witnessed.absence.capture == capture
    assert witnessed.absence.extract_spec == kev_spec_digest
    assert witnessed.absence.logical_source == "cisa.kev.catalog"
    # Every change is inside the grant: the run settles (all or nothing).
    (egress,) = first.terminal_egress
    assert egress.settle_outcome == "settled" and egress.outside_scope == ()
    # The run's node summaries count what each node did.
    (extracted,) = _summaries(first, "extract")
    assert (extracted.records, extracted.invalid_records, extracted.complete) == (12, 0, True)
    (written,) = _summaries(first, "claims")
    assert (written.written, written.inverse_written) == (18, 1)

    # 2. The catalog delists one CVE: its listed_in becomes the inverse and its
    #    mirrored date retires; the confirmed unlisted inverse writes nothing.
    replay = Replay(**{KEV_URL: "kev-catalog-delisted"})
    second = run(instance, replay, line="test.kev-line", minute=1)
    assert second.status == "succeeded", second.terminal
    changed = _by(proposed(instance, second))
    assert set(changed) == {(_cve(DELISTED), LISTED_IN), (_cve(DELISTED), DATE_ADDED)}
    assert changed[(_cve(DELISTED), LISTED_IN)].statement.object.kind == "none"
    retired = changed[(_cve(DELISTED), DATE_ADDED)]
    assert retired.lifecycle.state == "retired" and retired.retirement.absence is not None
    second_capture = changed[(_cve(DELISTED), LISTED_IN)].backing.procedure_evidence.absence.capture
    _assert_payload_capture(instance, second_capture, replay.body("kev-catalog-delisted"), KEV_URL)
    assert second.terminal_egress[0].settle_outcome == "settled"
    (delisting,) = _summaries(second, "claims")
    assert (delisting.retired, delisting.inverse_written) == (1, 1)
    assert delisting.re_observed >= 1  # the unchanged positives and the unlisted inverse

    # 3. EPSS: the API's JSON answer, verbatim, scores every watched CVE.
    replay = Replay(**{EPSS_URL: "epss-api"})
    third = run(instance, replay, line="test.epss-line", minute=2)
    assert third.status == "succeeded", third.terminal
    scores = _by(proposed(instance, third))
    assert set(scores) == {
        (_cve(c), predicate) for c in WATCHED for predicate in (SCORE, PERCENTILE, SCORE_DATE)
    }
    answer = json.loads(replay.body("epss-api"))
    epss_capture = scores[(_cve(WATCHED[0]), SCORE)].backing.procedure_evidence.value.capture_digest
    _assert_payload_capture(instance, epss_capture, replay.body("epss-api"), EPSS_URL)
    for index, record in enumerate(answer["data"]):
        score = scores[(_cve(record["cve"]), SCORE)]
        receipt = _extracted(
            score,
            epss_capture,
            pinned_extract_digest(instance, "test.epss-skeleton", "scores"),
        )
        assert receipt.record_locator == f"/data/{index}"
        assert receipt.field_path.endswith("/epss")
        assert float(score.statement.object.value) == float(record["epss"])
        assert scores[(_cve(record["cve"]), SCORE_DATE)].statement.object.value == record["date"]
    # No settle grant covers EPSS: its changes propose, and wait for review.
    (egress,) = third.terminal_egress
    assert egress.proposal_id is not None and egress.settle_outcome != "settled"
    activate(instance, third)


def test_the_origin_gate_reads_the_coordinate_final_url(tmp_path: Path) -> None:
    """C4: a redirect off the permitted origin refuses, read from the coordinate."""

    instance = skeleton_world(tmp_path)
    kev = {"url": KEV_URL, "logical_source": "cisa.kev.catalog", "expected_format": "json"}
    _install(
        instance,
        kev_spec(),
        line="test.kev-line",
        parameters=kev,
        origins=("https://www.cisa.gov",),
    )
    elsewhere = "https://mirror.example/known_exploited_vulnerabilities.json"
    replay = Replay(**{KEV_URL: f"redirect:{elsewhere}", elsewhere: "kev-catalog"})
    state = run(instance, replay, line="test.kev-line")
    assert replay.requests == [KEV_URL, elsewhere]
    assert state.status != "succeeded"
    assert state.terminal.code == "cruxible.acquisition.origin_ineligible"
    assert elsewhere in state.terminal.message and "coordinate" in state.terminal.message


# -- guard (c): Capture admission never parses content ---------------------------------


NOT_JSON = b"\x00\xffNOT-JSON-SKELETON-MARKER\x1f\x8b"


@pytest.fixture()
def decode_watch(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Record every attempt to parse the marked bytes, and whether extract had begun."""

    import csv
    import zlib

    import cruxible_core.procedures.extract as extract

    watch: dict[str, Any] = {"extracting": False, "early": [], "decoded": 0}

    def marked(value: object) -> bool:
        if isinstance(value, (bytes, bytearray)):
            return b"NOT-JSON-SKELETON-MARKER" in value
        return isinstance(value, str) and "NOT-JSON-SKELETON-MARKER" in value

    def guard(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if args and marked(args[0]) and not watch["extracting"]:
                watch["early"].append(name)
            return original(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(json, "loads", guard("json.loads", json.loads))
    monkeypatch.setattr(zlib, "decompress", guard("zlib.decompress", zlib.decompress))
    monkeypatch.setattr(csv, "reader", guard("csv.reader", csv.reader))
    original = extract._decoded_bytes

    def decoded(content: bytes, decoding: Any) -> bytes:
        watch["extracting"] = True
        watch["decoded"] += 1
        return original(content, decoding)

    monkeypatch.setattr(extract, "_decoded_bytes", decoded)
    yield watch


def test_c_admission_commits_any_bytes_and_only_extract_decodes(
    tmp_path: Path, decode_watch: dict[str, Any]
) -> None:
    """Arbitrary non-JSON bytes under a json CaptureKind commit untouched; the
    first and only decode is the extract node's, which refuses them."""

    instance = skeleton_world(tmp_path)
    params = {"url": KEV_URL, "logical_source": "cisa.kev.catalog", "expected_format": "bytes"}
    _install(instance, kev_spec(), line="test.kev-line", parameters=params)
    replay = Replay(**{KEV_URL: "bytes:" + base64.b64encode(NOT_JSON).decode("ascii")})
    state = run(instance, replay, line="test.kev-line")
    assert decode_watch["early"] == [], decode_watch
    assert decode_watch["decoded"] >= 1
    assert state.status != "succeeded"
    assert "decode_invalid" in json.dumps(state.terminal.model_dump(mode="json"))
    # The Capture was admitted and committed byte for byte before extract ran.
    from cruxible_client.contracts.canonical import CasDigest

    committed = CasDigest(hashlib.sha256(NOT_JSON).hexdigest()).tagged
    assert instance.body_store().read(committed, access=ACCESS) == NOT_JSON


def test_the_skeleton_cannot_skip_where_the_providers_checkout_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI sets CRUXIBLE_PROVIDERS_REQUIRED: an absent checkout fails, never skips."""

    monkeypatch.delenv("CRUXIBLE_PROVIDERS_CHECKOUT", raising=False)
    monkeypatch.setenv("CRUXIBLE_PROVIDERS_REQUIRED", "1")
    with pytest.raises(pytest.fail.Exception):
        require_provider_checkout()
    monkeypatch.delenv("CRUXIBLE_PROVIDERS_REQUIRED")
    with pytest.raises(pytest.skip.Exception):
        require_provider_checkout()


# -- the template's derive stage (D-R37): fetch -> derive -> extract -> claims ---------


DERIVE_REQUEST = boundary_contract(
    "test.derive.request",
    {"source": "capture<*>", "watched": "list<test.watched-row@1>{max_items: 100}"},
    WATCH_ROW,
)
DERIVE_PROVIDER = "test-derive"
VIEW_URL = "https://derive.test/kev-view"


def _contract_digest_in(checkout: Path, name: str) -> str:
    root = checkout / "packages/cruxible-provider-web/src/cruxible_provider_web/contracts"
    (path,) = sorted((root / name).glob("*.json"))
    member = "contracts/" + path.relative_to(root).as_posix()
    return contract_digest(parse_contract(path.read_bytes(), path=member)).tagged


def _derive_build(checkout: Path, *, effect: str = "pure") -> Any:
    """A core test-double provider: one pure ``derive`` returning Capture wire."""

    from cruxible_client.contracts.provider_operations import ProviderOperation
    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderRuntimeManifest,
    )
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

    digests = {
        ("test.derive.request", 1): contract_digest(DERIVE_REQUEST).tagged,
        ("web.fetch.result", 1): _contract_digest_in(checkout, "web.fetch.result"),
    }
    served = (
        ProviderOperation.model_validate(
            {
                "name": "derive",
                "in": "test.derive.request@1",
                "out": "web.fetch.result@1",
                "effect": effect,
                "refusals": {},
                "entrypoint": "demo.runtime:Derive",
            }
        ),
    )
    manifest = ProviderRuntimeManifest(
        provider_id=DERIVE_PROVIDER,
        distribution=ProviderDistributionRef(name=DERIVE_PROVIDER, version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Derive", backends=("local_env",), deterministic=True
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(schema_version=2, manifest=manifest, operations=served)
    return document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=DERIVE_PROVIDER,
            version="1.0.0",
            filename="test_derive-1.0.0-py3-none-any.whl",
            sha256="sha256:" + "d" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain=DERIVE_PROVIDER,
        operations=document.pinned_operations(lambda ref: digests.get((ref.name, ref.version))),
    )


def derive_capability_name(effect: str = "pure") -> str:
    """The derive slot's capability: one name per effect (a capability's effect is fixed)."""

    return "test.derive" if effect == "pure" else "test.derive-" + effect.replace("_", "-")


def derive_world(tmp_path: Path, *, kev_access: str = "open", effect: str = "pure") -> Any:
    instance = skeleton_world(tmp_path, kev_access=kev_access)
    accept_tree(instance, contract_files(DERIVE_REQUEST), name="derive-contracts")
    accept_head_capabilities(
        instance,
        (derive_capability_name(effect), "test.derive.request@1", "web.fetch.result@1", effect),
        name="derive-capabilities",
    )
    build = _derive_build(require_provider_checkout(), effect=effect)
    accept_tree(instance, build_files(build), name="derive")
    return instance


def kev_view(source: bytes, watched: list[Mapping[str, Any]]) -> bytes:
    """The derive: the catalog narrowed to the watched CVEs, as canonical JSON."""

    wanted = {row["cve_id"] for row in watched}
    rows = [
        {"cveID": item["cveID"], "dateAdded": item["dateAdded"]}
        for item in json.loads(source)["vulnerabilities"]
        if item["cveID"] in wanted
    ]
    document = {"vulnerabilities": sorted(rows, key=lambda row: row["cveID"])}
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class DeriveReplay(Replay):
    """web.fetch from recorded exchanges, and the pure derive double beside it."""

    def __init__(self, instance: Any, **routes: str) -> None:
        super().__init__(**routes)
        self.instance = instance
        self.derive_inputs: list[dict[str, Any]] = []

    def invoker(self) -> Invoker:
        fetch = super().invoker().output

        def output(context: Any) -> dict[str, Any]:
            if "url" in context.input:
                return fetch(context)
            self.derive_inputs.append(dict(context.input))
            # The derive reads the Capture's exact bytes from the wire core passed.
            source = context.input["source"]
            body = kev_view(base64.b64decode(source["content_base64"]), context.input["watched"])
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            named = source["bytes_digest"]
            return {
                "tag": "playbill-provider-result-to-external-capture-v1",
                "source_identity": "test.kev-view",
                "coordinate_type": "http-response-v1",
                "coordinate": {
                    "url": VIEW_URL,
                    "final_url": VIEW_URL,
                    "content_type": "application/json",
                    "request_digest": "sha256:" + hashlib.sha256(named.encode()).hexdigest(),
                    "source": "packaged-recording",
                    "status": 200,
                },
                "selector_type": "whole-response-v1",
                "selector": {},
                "replayability": "attested_only",
                "content_base64": base64.b64encode(body).decode("ascii"),
                "content_encoding": "base64",
                "byte_length": len(body),
                "bytes_digest": digest,
                "observed_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        return Invoker(output=output)


def _derive_step(alias: str, source: str) -> dict[str, Any]:
    return {
        "call": alias,
        "slot": "derive",
        "captures": {"capture": KEV_VIEW_KIND},
        "input": {"source": source, "watched": "$steps.watched.rows"},
    }


def derive_spec(*, chain: bool = False, mix: bool = False, effect: str = "pure") -> Any:
    """fetch -> derive (-> derive) -> extract -> claims, with absence over the watched CVEs.

    ``mix`` cites a second, unrelated fetched Capture in each Claim's rationale.
    """

    view = "chained" if chain else "view"
    steps: list[dict[str, Any]] = [
        _fetch_step("catalog", KEV_KIND),
        {"read": "watched", "query": "test.watched-vulnerabilities"},
        _derive_step("view", "$steps.catalog.capture"),
    ]
    if chain:
        steps.append(_derive_step("chained", "$steps.view.capture"))
    if mix:
        steps.append({**_fetch_step("other", KEV_KIND), "input": _fetch_step("x", "")["input"]})
    rationale = "The derived KEV view lists it" + (" ({$steps.other.capture})." if mix else ".")
    steps += [
        {
            "extract": "entries",
            "capture": f"$steps.{view}.capture",
            "records": "/vulnerabilities/*",
            "row": KEV_ROW.ref.spelling,
            "key": {"cve_id": {"at": "cveID", "type": f"key_part<{VULN}:cve_id>"}},
            "duplicates": "refuse_key",
            "fields": {"date_added": {"at": "dateAdded", "parse": "date"}},
        },
        {
            "id": "write",
            "for_each": "$steps.entries.records",
            "subject": {
                "kind": VULN,
                "key": {"cve_id": "$item.key.cve_id"},
                "create": "never",
                "within": "$steps.watched.rows",
            },
            "expect": {"$ref": "step", "step": "watched", "path": ["rows"]},
            "claims": [
                {
                    "type": LISTED_IN,
                    "value": {"kind": CATALOG, "key": {"catalog_id": "cisa-kev"}},
                    "when_absent": "inverse",
                    "rationale": rationale,
                },
                {"type": DATE_ADDED, "value": "$item.fields.date_added", "when_absent": "mirror"},
            ],
        },
    ]
    return build_spec(
        "test.kev-derive" + ("-chain" if chain else "") + ("-mix" if mix else ""),
        reference_text=True,
        input={},
        output={},
        params={
            "url": 'string{pattern: "^[Hh][Tt][Tt][Pp][Ss]?://\\\\S+$"}',
            "logical_source": 'string{pattern: "^[a-z][a-z0-9_.-]{0,255}$"}',
            "expected_format": "enum[auto, bytes, csv, html, json, text]",
        },
        slots={
            "fetch": {"capability": "web.fetch@1"},
            "derive": {"capability": f"{derive_capability_name(effect)}@1"},
        },
        hard_caps=procedure_limits(provider_calls=4, capture_bytes=1_048_576, read_rows=500),
        steps=steps,
    )


KEV_PARAMS = {"url": KEV_URL, "logical_source": "cisa.kev.catalog", "expected_format": "json"}


def _derive_run(instance: Any, *, line: str, minute: int = 0) -> tuple[Any, DeriveReplay]:
    replay = DeriveReplay(instance, **{KEV_URL: "kev-catalog"})
    return run(instance, replay, line=line, minute=minute), replay


def _manifest(instance: Any, capture: str) -> Any:
    from cruxible_client.contracts.captures import read_input_receipt_set_manifest

    envelope = _envelope(instance, capture)
    return read_input_receipt_set_manifest(
        instance.body_store(), envelope.input_receipt_set_manifest_digest
    )


def _accepted_node_input(instance: Any, procedure: str, node_id: str) -> Any:
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path(procedure)
    accepted = parse_procedure(tree[path], path=path)
    (node,) = (item for item in accepted.definition.nodes if item.node_id == node_id)
    return node.input


def test_a_derived_capture_records_its_inputs_and_is_each_claims_one_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cruxible_core.procedures.execution as execution
    from cruxible_client.contracts.captures import (
        derive_input_template_digest,
        read_derive_reducer,
        verify_capture,
    )
    from cruxible_core.service.evidence.body_access import capture_access_resolver

    committed: list[Any] = []
    digest_of = execution.run_value_digest

    def spy(domain: str, value: object) -> str:
        if domain == "provider-input" and isinstance(value, dict) and "watched" in value:
            committed.append(value)
        return digest_of(domain, value)

    monkeypatch.setattr(execution, "run_value_digest", spy)
    instance = derive_world(tmp_path)
    _install(instance, derive_spec(), line="test.kev-derive-line", parameters=KEV_PARAMS)
    first, replay = _derive_run(instance, line="test.kev-derive-line")
    assert first.status == "succeeded", first.terminal
    claims = _by(proposed(instance, first))
    listed = [cve for cve in WATCHED if cve != UNLISTED]
    assert set(claims) == {
        *((_cve(c), LISTED_IN) for c in WATCHED),
        *((_cve(c), DATE_ADDED) for c in listed),
    }
    # Each Claim cites the derived view: one root, basis extracted.
    date = claims[(_cve(listed[0]), DATE_ADDED)]
    root = date.backing.procedure_evidence.value.capture_digest
    spec_digest = pinned_extract_digest(instance, "test.kev-derive", "entries")
    for cve in listed:
        _extracted(claims[(_cve(cve), DATE_ADDED)], root, spec_digest)
    (derive_input,) = replay.derive_inputs
    view = _envelope(instance, root)
    assert instance.body_store().read(view.commitment.digest, access=ACCESS) == kev_view(
        replay.body("kev-catalog"), derive_input["watched"]
    )
    # Absence cites the root too; the root's manifest names its sources.
    unlisted = claims[(_cve(UNLISTED), LISTED_IN)].backing.procedure_evidence
    assert unlisted.absence is not None and unlisted.absence.capture == root
    manifest = _manifest(instance, root)
    (catalog,) = manifest.input_capture_digests
    _assert_payload_capture(instance, catalog, replay.body("kev-catalog"), KEV_URL)
    # The derive received the catalog Capture as its wire: the exact origin bytes
    # with the envelope's coordinate (b1); the run committed only its digest (b3).
    source = derive_input["source"]
    assert base64.b64decode(source["content_base64"]) == replay.body("kev-catalog")
    catalog_envelope = _envelope(instance, catalog)
    assert source["bytes_digest"] == catalog_envelope.commitment.digest
    assert source["coordinate"] == catalog_envelope.source.coordinate
    (digested,) = committed
    assert digested == {**derive_input, "source": catalog}
    assert manifest.input_receipt_digests and len(manifest.input_accepted_state_digests) == 1
    # The reducer: provider build, operation signature and implementation, and this
    # node's input template; never the Procedure digest.
    reducer = read_derive_reducer(instance.body_store(), view.reducer_digest)
    evidence = view.production_evidence
    assert reducer.provider_artifact_digest == evidence.provider_artifact_digest
    assert reducer.operation_signature_digest == evidence.operation_signature_digest
    assert reducer.implementation_digest == evidence.implementation_digest
    assert reducer.input_template_digest == derive_input_template_digest(
        _accepted_node_input(instance, "test.kev-derive", "view")
    )
    from cruxible_client.contracts.captures import parse_capture_contract

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = capture_contract_path(KEV_VIEW_KIND)
    contract = parse_capture_contract(tree[path], path=path)
    assert (
        verify_capture(
            root,
            store=instance.body_store(),
            contract=contract,
            producer_artifact_digests={
                view.producer.qualified: view.run_coordinate.executable_digest
            },
            producer_receipt_resolver=instance.capture_verification_context(),
        )
        == view
    )
    # The view is open, as its catalog is.
    resolver = capture_access_resolver(instance)
    assert not resolver.capture(root).restricted

    # Once accepted, a re-run over identical bytes re-observes: nothing new is written.
    activate(instance, first)
    second, _ = _derive_run(instance, line="test.kev-derive-line", minute=1)
    assert second.status == "succeeded", second.terminal
    (written,) = _summaries(second, "claims")
    assert written.written == 0 and written.re_observed >= len(listed)


def _get_card(instance: Any, ref: str) -> Any:
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    return service_playbill_get(instance, request=GetRequest(ref=ref), access=ACCESS).card


def test_get_on_a_derived_capture_serves_its_sources_reads_and_reducer(tmp_path: Path) -> None:
    instance = derive_world(tmp_path)
    _install(instance, derive_spec(), line="test.kev-derive-line", parameters=KEV_PARAMS)
    state, _ = _derive_run(instance, line="test.kev-derive-line")
    assert state.status == "succeeded", state.terminal
    claims = _by(proposed(instance, state))
    root = claims[(_cve(WATCHED[0]), DATE_ADDED)].backing.procedure_evidence.value.capture_digest
    manifest = _manifest(instance, root)
    card = _get_card(instance, "Capture:" + root)
    assert card.derivation is not None
    assert card.derivation.sources == manifest.input_capture_digests
    assert card.derivation.reads == manifest.input_accepted_state_digests
    assert card.derivation.reducer is not None
    assert card.derivation.reducer.digest == _envelope(instance, root).reducer_digest
    # Item 7: the reducer's build and operation are named beside their digests.
    assert card.derivation.reducer.operation == "derive"
    assert card.derivation.reducer.provider is not None
    assert card.derivation.reducer.provider.startswith(f"Provider:{DERIVE_PROVIDER}")
    (source,) = manifest.input_capture_digests
    assert _get_card(instance, "Capture:" + source).derivation is None


def test_a_derived_capture_inherits_its_restricted_inputs_body_access(tmp_path: Path) -> None:
    from cruxible_core.service.evidence.body_access import capture_access_resolver

    instance = derive_world(tmp_path, kev_access="restricted")
    _install(instance, derive_spec(), line="test.kev-derive-line", parameters=KEV_PARAMS)
    state, _ = _derive_run(instance, line="test.kev-derive-line")
    assert state.status == "succeeded", state.terminal
    claims = _by(proposed(instance, state))
    root = claims[(_cve(WATCHED[0]), DATE_ADDED)].backing.procedure_evidence.value.capture_digest
    (source,) = _manifest(instance, root).input_capture_digests
    resolver = capture_access_resolver(instance)
    view, catalog = resolver.capture(root), resolver.capture(source)
    assert catalog.restricted and catalog.kind is not None and catalog.kind.access == "restricted"
    # The view's own kind is open; it inherits its catalog's restriction.
    assert view.kind is not None and view.kind.access == "open"
    assert view.restricted and view.inherited_restricted
    # An unrecorded derivation fails closed.
    with instance.body_store().capture_envelopes._connection() as connection:  # noqa: SLF001
        connection.execute("DELETE FROM derivations")
    fresh = capture_access_resolver(instance).capture(root)
    assert fresh.restricted and fresh.inherited_restricted


def test_a_derive_chain_roots_each_claim_at_the_last_derive(tmp_path: Path) -> None:
    instance = derive_world(tmp_path)
    _install(instance, derive_spec(chain=True), line="test.kev-chain-line", parameters=KEV_PARAMS)
    state, replay = _derive_run(instance, line="test.kev-chain-line")
    assert state.status == "succeeded", state.terminal
    assert len(replay.derive_inputs) == 2
    claims = _by(proposed(instance, state))
    root = claims[(_cve(WATCHED[0]), DATE_ADDED)].backing.procedure_evidence.value.capture_digest
    first_view = _manifest(instance, root).input_capture_digests
    # The chained view records both the first view and the catalog it read.
    assert len(first_view) == 2
    (view,) = (item for item in first_view if _manifest_or_none(instance, item) is not None)
    assert set(_manifest(instance, view).input_capture_digests) < set(first_view)


def _manifest_or_none(instance: Any, capture: str) -> Any:
    envelope = _envelope(instance, capture)
    return (
        None if envelope.input_receipt_set_manifest_digest is None else _manifest(instance, capture)
    )


def test_an_unrelated_second_capture_in_a_claims_closure_still_refuses(tmp_path: Path) -> None:
    instance = derive_world(tmp_path)
    _install(instance, derive_spec(mix=True), line="test.kev-mix-line", parameters=KEV_PARAMS)
    state, _ = _derive_run(instance, line="test.kev-mix-line")
    assert state.status != "succeeded"
    assert state.terminal.code == "proposal_item_evidence_missing", state.terminal
    assert "root Capture" in state.terminal.message


def test_a_derive_must_declare_effect_pure(tmp_path: Path) -> None:
    """b5: a call producing a derived Capture from Capture bytes is pure computation."""

    instance = derive_world(tmp_path, effect="external_read")
    spec = derive_spec(effect="external_read")
    _install(instance, spec, line="test.kev-impure-line", parameters=KEV_PARAMS)
    state, replay = _derive_run(instance, line="test.kev-impure-line")
    assert state.status != "succeeded"
    assert state.terminal.code == "provider_effect_declaration_mismatch", state.terminal
    assert replay.derive_inputs == []


def test_a_derive_input_must_be_a_capture_this_run_fetched_or_admitted() -> None:
    """b2: another stored Capture is replay, refused before any byte is read."""

    from types import SimpleNamespace

    from cruxible_core.procedures.execution import ProcedureExecutor, _RunRefusal
    from cruxible_core.procedures.terminal_dependencies import DependencyEvidenceFactsV1

    foreign, exhaust = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    state = SimpleNamespace(
        facts={exhaust: DependencyEvidenceFactsV1(epistemic_grade="derived")},
    )
    for digest in (foreign, exhaust, 7):
        with pytest.raises(_RunRefusal) as refused:
            ProcedureExecutor._capture_input_wire(  # noqa: SLF001
                None,  # type: ignore[arg-type]
                digest,
                kind="*",
                state=state,  # type: ignore[arg-type]
                node_id="view",
            )
        assert refused.value.refusal.code == "contract_input_refused"
        assert "replay" in str(refused.value)
