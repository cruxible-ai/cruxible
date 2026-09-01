from __future__ import annotations

import httpx

from cruxible_client import CruxibleClient


def test_client_lists_policies_in_force_from_the_single_read_route() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "tag": "playbill-policy-in-force-list-v1",
                "coordinate": {
                    "tag": "playbill-accepted-coordinate-v1",
                    "git_oid": "1" * 40,
                    "semantic_root": "sha256:" + "2" * 64,
                    "generation_root": "sha256:" + "3" * 64,
                    "compiler_digest": "sha256:" + "4" * 64,
                },
                "policies": [
                    {
                        "tag": "playbill-policy-in-force-v1",
                        "placement": "standalone",
                        "policy_kind": "approval_policy",
                        "declaring_artifact_identity": "ApprovalPolicy:instance",
                        "declaring_artifact_kind": "ApprovalPolicy",
                        "declaring_artifact_digest": "sha256:" + "5" * 64,
                        "path": "governance/approval-policy.json",
                        "field_path": "/",
                        "policy": {
                            "tag": "playbill-approval-policy-v1",
                            "mode": "self_approval_allowed",
                        },
                    }
                ],
            },
        )

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )

    result = client.list_playbill_policies_in_force("inst_policy")

    assert result.policies[0].policy_kind == "approval_policy"
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/api/v1/inst_policy/playbill/policies"
