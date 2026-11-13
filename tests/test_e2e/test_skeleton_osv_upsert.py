"""An OSV-shaped single Procedure: fetch-list -> derive -> extract -> claims (V1-V5).

One fetch returns OSV advisories as a list of Captures; a pure derive over
them and the accepted components emits one derived Capture: the purls it
``queried`` (a header) and one record per (component, CVE) match. Claims
write ``security.component.affected_by``, whose Subject-valued value creates
each vulnerability the instance does not hold yet (``create: if_absent``).
``expect`` is the header's ``queried``: mirror retires a CVE a checked
component no longer matches, and a component the provider could not check
(a distro package, K-B1-6) is never retired, only left as it is.

A second Line runs on that derived Capture's landing (Q3): its Trigger
substitutes the Capture, so it extracts and writes Claims without fetching.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.authoring.inputs import (
    LineInput,
    QueryDefinitionInput,
    SubjectInput,
    TriggerInput,
)
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.query.authored import QuerySpecInput
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from tests.core_support._identity_support import accept_tree
from tests.core_support._light_capture_support import JSON_DECODING, http_contract
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.support.capabilities import ensure_capabilities
from tests.support.layouts import record_set, with_layout
from tests.support.operation_world import NOW, Invoker, author, build_files
from tests.support.procedures import boundary_contract, contract_files, procedure_limits
from tests.support.procedures import procedure_spec as build_spec
from tests.support.provider_checkout import require_provider_checkout
from tests.test_e2e.test_skeleton_capture_lists import _wire, install
from tests.test_e2e.test_skeleton_kev_epss import (
    VULN,
    _claim_type,
    _contract_digest_in,
    _key_type,
    activate,
    proposed,
    run,
    skeleton_world,
)

COMPONENT = "security.component"
PURL = COMPONENT + ".purl"
AFFECTED = COMPONENT + ".affected_by"
ADVISORIES_KIND = "test.osv-advisories"
MATCHES_KIND = "test.osv-matches"
PROVIDER = "test-osv"
ORIGIN = "https://osv.test"
LOGICAL_SOURCE = "osv.dev.querybatch"
LINE = "test.osv-line"
LANDED_LINE = "test.osv-landed-line"
TRIGGER = "test.osv-matches-landed"

COMPONENT_ROW = boundary_contract("test.component-row", {"purl": "string"})
OSV_REQUEST = boundary_contract("test.osv.request", {"feed": "string"})
OSV_RESULT = boundary_contract("test.osv.result", {"pages": "list<capture<*>>{max_items: 3}"})
OSV_DERIVE = boundary_contract(
    "test.osv-derive.request",
    {
        "sources": "list<capture<*>>{max_items: 3}",
        "components": "list<test.component-row@1>{max_items: 100}",
    },
    COMPONENT_ROW,
)
MATCH_ROW = boundary_contract("test.osv-match", {"summary": "string{max_len: 256}?"})

A = "pkg:pypi/a@1.0"
B = "pkg:pypi/b@2.0"
DISTRO = "pkg:deb/debian/c@3.0"
X, Y, Z, W = "CVE-2026-1001", "CVE-2026-1002", "CVE-2026-1003", "CVE-2026-1004"


def _osv_build(checkout: Path) -> Any:
    """A core test-double provider: the advisory fetch and the pure match derive."""

    from cruxible_client.contracts.provider_operations import ProviderOperation
    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
    )
    from cruxible_client.contracts.shapes import contract_digest
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

    digests = {
        (item.ref.name, item.ref.version): contract_digest(item).tagged
        for item in (OSV_REQUEST, OSV_RESULT, OSV_DERIVE)
    }
    digests[("web.fetch.result", 1)] = _contract_digest_in(checkout, "web.fetch.result")
    served = tuple(
        ProviderOperation.model_validate(
            {
                "name": name,
                "in": request,
                "out": result,
                "effect": effect,
                "refusals": {},
                "entrypoint": "demo.runtime:Osv",
            }
        )
        for name, request, result, effect in (
            ("fetch_advisories", "test.osv.request@1", "test.osv.result@1", "external_read"),
            ("derive_matches", "test.osv-derive.request@1", "web.fetch.result@1", "pure"),
        )
    )
    manifest = ProviderRuntimeManifest(
        provider_id=PROVIDER,
        distribution=ProviderDistributionRef(name=PROVIDER, version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Osv", backends=("local_env",), deterministic=True
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(schema_version=2, manifest=manifest, operations=served)
    return document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=PROVIDER,
            version="1.0.0",
            filename="test_osv-1.0.0-py3-none-any.whl",
            sha256="sha256:" + "e" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain=PROVIDER,
        operations=document.pinned_operations(lambda ref: digests.get((ref.name, ref.version))),
    )


def _matches_kind() -> Any:
    """The derived matches kind, declaring its ``matches`` record set and ``queried`` header."""

    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )

    kind = lower_capture_contract_input(
        CaptureContractInputRecord(
            name=MATCHES_KIND,
            shape="http_response",
            grade="derived",
            replay="attested",
            max_bytes=1_048_576,
            decoding=JSON_DECODING,
        ),
        named=True,
    )
    return with_layout(
        kind,
        {
            "matches": record_set(
                at="/matches/*",
                row=MATCH_ROW.ref.spelling,
                key={
                    "purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"},
                    "advisory": {"at": "cve_id", "type": f"key_part<{VULN}:cve_id>"},
                },
                fields={"summary": {"at": "summary"}},
                count_at="/count",
            )
        },
        {"queried": {"at": "/queried", "type": "list<string{max_len: 256}>{max_items: 100}"}},
    )


def osv_world(tmp_path: Path) -> Any:
    """The KEV skeleton's vulnerabilities, plus components and the OSV provider."""

    instance = skeleton_world(tmp_path)
    affected = _claim_type(
        AFFECTED,
        f"subject<{VULN}>",
        allowed_subject_kinds=[COMPONENT],
        cardinality="many",
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="captured",
                    claim_roles=("observation",),
                    capture_contracts=(MATCHES_KIND,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )
    accept_tree(
        instance,
        {
            **contract_files(COMPONENT_ROW, OSV_REQUEST, OSV_RESULT, OSV_DERIVE, MATCH_ROW),
            **{
                capture_contract_path(kind.identity.name): render_capture_contract(kind)
                for kind in (
                    http_contract(name=ADVISORIES_KIND, decoding=JSON_DECODING),
                    _matches_kind(),
                )
            },
            subject_kind_path(COMPONENT): render_subject_kind(
                keyed_subject_kind(COMPONENT, (PURL,))
            ),
            **{
                claim_type_path(t.predicate): render_claim_type(t)
                for t in (affected, _key_type(PURL, COMPONENT))
            },
        },
        name="osv-vocabulary",
    )
    ensure_capabilities(instance, *OSV_CAPABILITIES, name="osv-capabilities")
    accept_tree(instance, build_files(_osv_build(require_provider_checkout())), name="osv")
    for index, purl in enumerate((A, B, DISTRO)):
        author(
            instance,
            SubjectInput(
                kind="subject", subject_kind=COMPONENT, key={"purl": purl}, rationale="In SBOM."
            ),
            name=f"component-{index}",
        )
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": "test.components",
                    "from": {"c": COMPONENT},
                    "row": COMPONENT_ROW.ref.spelling,
                    "fields": ["c.purl"],
                }
            ),
        ),
        name="components-query",
    )
    return instance


class Osv:
    """Answers the advisory fetch with one page, and the match derive over it.

    ``unmatched`` purls are ones the provider cannot check (K-B1-6): left out of
    ``queried``, with no matches.
    """

    def __init__(self, advisories: dict[str, list[str]], *, unmatched: tuple[str, ...] = ()):
        self.advisories = advisories
        self.unmatched = unmatched
        self.calls: list[str] = []

    def page(self) -> bytes:
        body = {
            "advisories": [
                {"id": cve, "affected": purls} for cve, purls in sorted(self.advisories.items())
            ]
        }
        return json.dumps(body, sort_keys=True).encode()

    def invoker(self) -> Invoker:
        def output(context: Any) -> dict[str, Any]:
            if "feed" in context.input:
                self.calls.append("fetch")
                return {"pages": [_wire(self.page(), f"{ORIGIN}/v1/query", source=LOGICAL_SOURCE)]}
            self.calls.append("derive")
            body = matches(context.input["sources"], context.input["components"], self.unmatched)
            wire = _wire(body, "https://derive.test/osv-matches", source="test.osv-matches")
            return {**wire, "tag": "playbill-provider-result-to-external-capture-v1"}

        return Invoker(output=output)


def matches(
    sources: list[dict[str, Any]], components: list[dict[str, Any]], unmatched: Any
) -> bytes:
    """The derive: the purls it checked, and one record per (purl, CVE) it matched."""

    queried = sorted(row["purl"] for row in components if row["purl"] not in unmatched)
    pairs = sorted(
        {
            (purl, advisory["id"])
            for source in sources
            for advisory in json.loads(base64.b64decode(source["content_base64"]))["advisories"]
            for purl in advisory["affected"]
            if purl in queried
        }
    )
    document = {
        "queried": queried,
        "matches": [{"purl": purl, "cve_id": cve} for purl, cve in pairs],
        "count": len(pairs),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


DERIVE_SLOT = {"capability": "test.osv-derive@1"}
#: The capability each slot names: ``(name, in, out, effect)``.
OSV_CAPABILITIES = (
    ("test.osv", "test.osv.request@1", "test.osv.result@1", "external_read"),
    ("test.osv-derive", "test.osv-derive.request@1", "web.fetch.result@1", "pure"),
)


def _extract_and_claims() -> list[dict[str, Any]]:
    return [
        {
            "extract": "matched",
            "capture": "$steps.matches.capture",
            "records": "matches",
            "duplicates": "refuse_key",
            "header": ["queried"],
        },
        {
            "id": "write",
            "for_each": "$steps.matched.records",
            "subject": {"kind": COMPONENT, "key": {"purl": "$item.key.purl"}, "create": "never"},
            "claims": [
                {
                    "type": AFFECTED,
                    "value": {
                        "kind": VULN,
                        "key": {"cve_id": "$item.key.advisory"},
                        "create": "if_absent",
                    },
                    "when_absent": "mirror",
                    "rationale": "OSV lists this vulnerability for the component.",
                }
            ],
            "expect": "$steps.matched.header.queried",
        },
    ]


def osv_spec() -> Any:
    """read -> fetch -> derive -> extract -> claims, one Procedure."""

    return build_spec(
        "test.osv",
        reference_text=True,
        input={},
        output={},
        slots={"osv": {"capability": "test.osv@1"}, "derive": DERIVE_SLOT},
        hard_caps=procedure_limits(provider_calls=2, capture_bytes=1_048_576, read_rows=500),
        steps=[
            {"read": "components", "query": "test.components"},
            {
                "call": "advisories",
                "slot": "osv",
                "captures": {"pages": ADVISORIES_KIND},
                "input": {"feed": "osv"},
            },
            {
                "call": "matches",
                "slot": "derive",
                "captures": {"capture": MATCHES_KIND},
                "input": {
                    "sources": "$steps.advisories.pages",
                    "components": "$steps.components.rows",
                },
            },
            *_extract_and_claims(),
        ],
    )


def _paths(instance: Any, predicate: str) -> dict[str, str]:
    """Each live Subject holding the key ``predicate``, by its key value."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    found: dict[str, str] = {}
    for path in tree:
        if path.startswith("claims/"):
            claim = parse_claim(tree[path], path=path)
            if claim.statement.predicate == predicate and claim.lifecycle.state == "live":
                found[str(claim.statement.object.value)] = claim.statement.subject.artifact_path
    return found


def affected(instance: Any) -> dict[str, set[str]]:
    """purl -> the CVEs its live affected_by Claims name."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    purls = {path: purl for purl, path in _paths(instance, PURL).items()}
    cves = {path: cve for cve, path in _paths(instance, VULN + ".cve_id").items()}
    found: dict[str, set[str]] = {purl: set() for purl in purls.values()}
    for path in tree:
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.statement.predicate != AFFECTED or claim.lifecycle.state != "live":
            continue
        found[purls[claim.statement.subject.artifact_path]].add(
            cves[claim.statement.object.address.artifact_path]
        )
    return found


def _changes(state: Any) -> list[str]:
    """The kinds of change the run proposed, from its served child receipts."""

    (egress,) = state.terminal_egress
    return sorted(child.change_kind for child in egress.children if child.change_kind)


def test_osv_single_procedure_creates_vulnerabilities_and_mirrors_by_queried(
    tmp_path: Path,
) -> None:
    instance = osv_world(tmp_path)
    install(instance, osv_spec(), line=LINE)
    cves_before = set(_paths(instance, VULN + ".cve_id"))

    first_source = Osv({X: [A], Y: [A, B], Z: [DISTRO]})
    first = run(instance, first_source, line=LINE)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    assert first_source.calls == ["fetch", "derive"]
    # Three new vulnerabilities, each created once though Y affects two components.
    assert _changes(first) == ["claim"] * 4 + ["subject"] * 3
    claims = proposed(instance, first)
    assert len([c for c in claims if c.statement.predicate == AFFECTED]) == 4
    activate(instance, first)
    assert set(_paths(instance, VULN + ".cve_id")) - cves_before == {X, Y, Z}
    assert affected(instance) == {A: {X, Y}, B: {Y}, DISTRO: {Z}}
    # Every Claim cites the derived Capture: one root per Claim.
    (derived,) = {c.backing.capture_digests for c in claims if c.statement.predicate == AFFECTED}
    assert len(derived) == 1

    # B no longer matches Y; A gains a new W; the distro package goes unchecked.
    second_source = Osv({X: [A], Y: [A], W: [A]}, unmatched=(DISTRO,))
    second = run(instance, second_source, line=LINE, minute=1)
    assert second.status == "succeeded", json.dumps(second.terminal.model_dump(mode="json"))
    # W created with its Claim; B's Y retired; A's X and Y re-observed; DISTRO untouched.
    assert _changes(second) == ["claim", "reobservation", "reobservation", "retirement", "subject"]
    activate(instance, second)
    assert affected(instance) == {A: {X, Y, W}, B: set(), DISTRO: {Z}}

    # The same evidence again changes nothing.
    third = run(instance, Osv({X: [A], Y: [A], W: [A]}, unmatched=(DISTRO,)), line=LINE, minute=2)
    assert third.status == "succeeded", third.terminal
    assert third.terminal_egress[0].proposal_id is None


# -- Q3: a capture_landing Trigger fires on a DERIVED Capture ------------------------


def landed_spec() -> Any:
    """The derive call is the Trigger's input: substituted, never run."""

    return build_spec(
        "test.osv-landed",
        reference_text=True,
        input={},
        output={},
        slots={"derive": DERIVE_SLOT},
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=1_048_576),
        steps=[
            {
                "call": "matches",
                "slot": "derive",
                "captures": {"capture": MATCHES_KIND},
                "input": {"sources": [], "components": []},
            },
            *_extract_and_claims(),
        ],
    )


def _install_landed(instance: Any) -> None:
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        parse_line_spec,
    )
    from tests.support.mandates import procedure_mandate
    from tests.support.operation_world import author_procedure

    spec = landed_spec()
    author_procedure(instance, spec, name="landed-procedure")
    author(
        instance,
        LineInput.model_validate(
            {
                "kind": "line",
                "name": LANDED_LINE,
                "procedure_name": spec.procedure,
                "parameters": {},
                "max_authority": "propose",
                "trigger_input": "matches.capture",
                "acquisition": {"matches.capture": {}},
            }
        ),
        name="landed-line",
    )
    author(
        instance,
        TriggerInput.model_validate(
            {
                "kind": "trigger",
                "name": TRIGGER,
                "line_name": LANDED_LINE,
                "schedule": {"kind": "capture_landing", "capture_contract": MATCHES_KIND},
            }
        ),
        name="landed-trigger",
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(LANDED_LINE)
    line = parse_line_spec(tree[path], path=path)
    mandate = procedure_mandate(
        LANDED_LINE,
        namespace=("claims", "subjects"),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
        name="landed-mandate",
    )


def test_a_derived_captures_landing_triggers_a_line_that_writes_without_fetching(
    tmp_path: Path,
) -> None:
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        TriggerFire,
        service_run_playbill_line,
    )
    from tests.support.operation_world import actor
    from tests.test_procedures.test_completeness_transfer import _Clock

    instance = osv_world(tmp_path)
    install(instance, osv_spec(), line=LINE)
    _install_landed(instance)
    source = Osv({X: [A], Y: [A, B]})
    upstream = run(instance, source, line=LINE)
    assert upstream.status == "succeeded", upstream.terminal
    (call,) = (node for node in upstream.nodes if node.node == "matches")
    (derived,) = call.summary.capture_digests
    (event,) = [
        o.capture_event
        for o in upstream.outcomes
        if o.event_kind == "produced_capture" and o.node_id == call.node
    ]

    state = service_run_playbill_line(
        instance,
        path_identity_digest=LANDED_LINE,
        request=LineRunRequest(line=LANDED_LINE),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW + timedelta(minutes=1)),
        # No Provider runtime: the input is the landed derived Capture.
        trigger_fire=TriggerFire(trigger=TRIGGER, event=event),
    )
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert state.source_observations == ()
    assert source.calls == ["fetch", "derive"]  # the upstream run's only
    claims = [c for c in proposed(instance, state) if c.statement.predicate == AFFECTED]
    assert len(claims) == 3
    assert {c.backing.capture_digests for c in claims} == {(derived,)}
    activate(instance, state)
    assert affected(instance) == {A: {X, Y}, B: {Y}, DISTRO: set()}
