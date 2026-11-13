"""The HTTP coordinate may name where its bytes came from, never a credential.

Core's ``http-response-v1`` coordinate carries ``url`` and ``final_url``
(D-R25; the acquisition origin gate reads both). Every other coordinate and
selector field stays locator-free, and a URL carrying userinfo or a
credential-bearing query parameter is refused, never rewritten.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.source_references import ExternalSourceReference


def _reference(coordinate: object, *, coordinate_type: str = "http-response-v1") -> object:
    return ExternalSourceReference(
        source_identity="cisa.kev.catalog",
        producer_binding_digest="sha256:" + "0" * 64,
        coordinate_type=coordinate_type,
        coordinate=coordinate,
        selector_type="whole-response-v1",
        selector={},
        replayability="attested_only",
    )


def _coordinate(**urls: str) -> dict[str, object]:
    return {
        "url": "https://www.cisa.gov/feeds/kev.json",
        "final_url": "https://www.cisa.gov/feeds/kev.json",
        "status": 200,
        **urls,
    }


@pytest.mark.parametrize("field", ["url", "final_url"])
@pytest.mark.parametrize(
    "value",
    [
        "https://api.first.org/data/v1/epss?cve=CVE-2021-44228&limit=10",
        "https://www.cisa.gov/",
        "http://mirror.example:8080/kev.json#latest",
    ],
)
def test_the_url_fields_may_name_a_credential_free_locator(field: str, value: str) -> None:
    reference = _reference(_coordinate(**{field: value}))
    assert reference.coordinate[field] == value  # recorded exactly, never rewritten


@pytest.mark.parametrize("field", ["url", "final_url"])
@pytest.mark.parametrize(
    "value",
    [
        "https://user:pass@api.example/data",
        "https://token@api.example/data",
        "https://api.example/data?api_key=SECRET",
        "https://api.example/data?token=SECRET",
        "https://api.example/data?cve=x&Access-Token=SECRET",
        "https://api.example/data?client_secret=SECRET",
        "https://api.example/data?password=",
        "/relative/path",
        "ftp://api.example/data",
    ],
)
def test_a_url_carrying_credentials_or_no_origin_is_refused(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=r"coordinate URL"):
        _reference(_coordinate(**{field: value}))


def test_a_paged_listing_url_is_checked_the_same_way() -> None:
    _reference(_coordinate(pages=[{"url": "https://api.example/list?page=2", "status": 200}]))
    with pytest.raises(ValidationError, match="credentials"):
        _reference(_coordinate(pages=[{"url": "https://api.example/list?token=x"}]))


def test_other_fields_and_other_coordinate_types_stay_locator_free() -> None:
    with pytest.raises(ValidationError, match="cannot carry locators"):
        _reference(_coordinate(content_type="https://not.a.type/"))
    with pytest.raises(ValidationError, match="cannot carry locators"):
        _reference({"url": "https://www.cisa.gov/"}, coordinate_type="database-snapshot-v1")
    with pytest.raises(ValidationError, match="cannot carry locators"):
        ExternalSourceReference(
            source_identity="cisa.kev.catalog",
            producer_binding_digest="sha256:" + "0" * 64,
            coordinate_type="http-response-v1",
            coordinate=_coordinate(),
            selector_type="whole-response-v1",
            selector={"url": "https://www.cisa.gov/"},
            replayability="attested_only",
        )
