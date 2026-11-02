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
                        capture_contracts=(EPSS_KIND, KEV_KIND),
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
        slots={
            "fetch": {
                "in": "web.fetch.request@1",
                "out": "web.fetch.result@1",
                "effect": "external_read",
            }
        },
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


def skeleton_world(tmp_path: Path) -> Any:
    from tests.core_support._support import initialize_local

    checkout = require_provider_checkout()  # the skeleton runs the real web.fetch
    instance, _owner = initialize_local(tmp_path)
    members, build = _web_provider_files(checkout)
    kinds = (
        http_contract(name=KEV_KIND, decoding=JSON_DECODING),
        http_contract(name=EPSS_KIND, decoding=JSON_DECODING),
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
