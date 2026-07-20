import asyncio
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from app.clients.wipo_patentscope_rest import (
    WipoPatentScopeRestClient,
    parse_published_application_xml,
    to_wipo_rest_number,
)
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentReference, PatentSource


IASR = {
    "wo-bibliographic-data": {
        "application-reference": {
            "document-id": {
                "country": "IB",
                "doc-number": "PCT/IB2025/000001",
                "date": "20250102",
            }
        },
        "wo-application-info": {
            "date-of-earliest-priority": {"date": "20240103"}
        },
        "invention-title": [
            {"lang": "FR", "content": ["TITRE"]},
            {"lang": "EN", "content": ["REST TITLE"]},
        ],
        "parties": {
            "applicants": {
                "applicant": [
                    {"addressbook": [{"orgname": "IASR Applicant Ltd"}]}
                ]
            }
        },
    },
    "abstract": [{"lang": "EN", "p": [{"content": ["IASR abstract."]}]}],
}

PAMPHLET_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<wo-patent-document>
  <bibliographic-data>
    <publication-reference><document-id><country>WO</country><doc-number>2025078629</doc-number><kind>A1</kind><date>20250417</date></document-id></publication-reference>
    <application-reference><document-id><country>PCT</country><doc-number>EP2024078738</doc-number><date>20241011</date></document-id></application-reference>
    <invention-title lang="fr">TITRE FR</invention-title>
    <invention-title lang="en">CAPSULE SYSTEM</invention-title>
    <classifications-ipcr>
      <classification-ipcr><text>A61K 9/48 2006.01</text></classification-ipcr>
    </classifications-ipcr>
    <parties>
      <applicants><applicant><addressbook><orgname>ENTEROTARGET APS</orgname></addressbook></applicant></applicants>
      <inventors><inventor><addressbook><first-name>Jorgen</first-name><last-name>OLSEN</last-name></addressbook></inventor></inventors>
      <agents><agent><addressbook><name>Jane Agent</name><orgname>HOEIBERG P/S</orgname><address><address-1>Adelgade 12</address-1><city>Copenhagen</city><country>DK</country></address></addressbook></agent></agents>
    </parties>
  </bibliographic-data>
  <abstract lang="en"><p>A compact capsule system.</p></abstract>
  <description><p>FIG. 1 shows the capsule.</p><p>Detailed description text.</p></description>
  <claims><claim><claim-text>A capsule.</claim-text></claim><claim><claim-text>The capsule of claim 1.</claim-text></claim></claims>
  <drawings><figure><img file="000001.tif"/></figure></drawings>
</wo-patent-document>"""


def _reference() -> PatentReference:
    return PatentReference(
        source=PatentSource.WIPO,
        normalized_number="WO2025078629A1",
        display_number="WO/2025/078629",
        country_code="WO",
        doc_number="2025078629",
        kind_code="A1",
        lookup_number="WO2025078629A1",
    )


def _settings() -> Settings:
    return Settings(
        wipo_patentscope_username="rest-user",
        wipo_patentscope_password="rest-pass",
    )


def _zip_payload(name: str = "wo-published-application.xml") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, PAMPHLET_XML)
    return output.getvalue()


def test_rest_number_uses_two_digit_year_and_drops_kind_code():
    assert to_wipo_rest_number(_reference()) == "WO25078629"


def test_parse_pamphlet_fills_missing_fields_and_metrics():
    info, metrics = parse_published_application_xml(PAMPHLET_XML)

    assert info.title == "CAPSULE SYSTEM"
    assert info.abstract == "A compact capsule system."
    assert info.publication_date == "20250417"
    assert info.application_number == "PCT/EP2024078738"
    assert info.ipc == ["A61K 9/48 2006.01"]
    assert info.applicants == ["ENTEROTARGET APS"]
    assert info.inventors == ["Jorgen OLSEN"]
    assert info.representatives[0].name == "Jane Agent"
    assert info.representatives[0].organization == "HOEIBERG P/S"
    assert info.representatives[0].country == "DK"
    assert metrics["claims_count"] == 2
    assert metrics["claims_words"] == 7
    assert metrics["drawings"].has_drawings is True
    assert metrics["drawings"].drawing_labels == ["FIG. 1 shows the capsule."]


def test_rest_lookup_uses_official_flow_without_downloading_zip(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/ia-status-report"):
            return httpx.Response(200, json=IASR)
        if path.endswith("/pct-publications/WO25078629"):
            return httpx.Response(
                200,
                json={
                    "availableDocuments": [
                        {
                            "docId": "id-pamph",
                            "docType": "PAMPH",
                            "minSpecCode": "pamph",
                        }
                    ]
                },
            )
        if path.endswith("/documents/id-pamph/pages"):
            return httpx.Response(
                200,
                json={"docId": "id-pamph", "content": ["wo-published-application.xml", "000001.tif"]},
            )
        if path.endswith("/documents/id-pamph/pages/wo-published-application.xml"):
            return httpx.Response(200, content=PAMPHLET_XML)
        raise AssertionError(path)

    client = WipoPatentScopeRestClient(
        _settings(), transport=httpx.MockTransport(handler), storage_dir=tmp_path
    )
    response = asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert response.basic_info.title == "CAPSULE SYSTEM"
    assert response.basic_info.representatives[0].organization == "HOEIBERG P/S"
    assert response.original_file.available is False
    assert response.raw_source_refs["lookup_mode"] == "rest"
    assert len(requests) == 4
    assert all(request.headers["Cookie"] == "OBBasicAuth=fromDialog" for request in requests)
    assert all(request.headers["Authorization"].startswith("Basic ") for request in requests)


def test_rest_lookup_downloads_official_zip_when_requested(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ia-status-report"):
            return httpx.Response(200, json=IASR)
        if path.endswith("/pct-publications/WO25078629"):
            return httpx.Response(200, json={"availableDocuments": [{"docId": "id-pamph", "docType": "PAMPH"}]})
        if path.endswith("/documents/id-pamph/pages"):
            return httpx.Response(200, json={"content": ["wo-published-application.xml"]})
        if path.endswith("/pages/wo-published-application.xml"):
            return httpx.Response(200, content=PAMPHLET_XML)
        if path.endswith("/documents/id-pamph"):
            return httpx.Response(
                200,
                content=_zip_payload(),
                headers={
                    "Content-Type": "application/zip",
                    "Content-Disposition": 'attachment; filename="WO2025078629_PAMPH.zip"',
                },
            )
        raise AssertionError(path)

    client = WipoPatentScopeRestClient(
        _settings(), transport=httpx.MockTransport(handler), storage_dir=tmp_path
    )
    response = asyncio.run(client.lookup_patent(_reference(), include_original_file=True))

    assert response.original_file.available is True
    assert response.original_file.content_type == "application/zip"
    with zipfile.ZipFile(response.original_file.storage_path) as archive:
        assert archive.namelist() == ["wo-published-application.xml"]


def test_rest_rejects_unsafe_zip_member(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ia-status-report"):
            return httpx.Response(200, json=IASR)
        if path.endswith("/pct-publications/WO25078629"):
            return httpx.Response(200, json={"availableDocuments": [{"docId": "id-pamph", "docType": "PAMPH"}]})
        if path.endswith("/documents/id-pamph/pages"):
            return httpx.Response(200, json={"content": ["wo-published-application.xml"]})
        if path.endswith("/pages/wo-published-application.xml"):
            return httpx.Response(200, content=PAMPHLET_XML)
        if path.endswith("/documents/id-pamph"):
            return httpx.Response(
                200,
                content=_zip_payload("../escape.xml"),
                headers={"Content-Type": "application/zip"},
            )
        raise AssertionError(path)

    client = WipoPatentScopeRestClient(
        _settings(), transport=httpx.MockTransport(handler), storage_dir=tmp_path
    )
    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(client.lookup_patent(_reference(), include_original_file=True))

    assert excinfo.value.code is ErrorCode.UPSTREAM_RESPONSE_INVALID


def test_rest_maps_rate_limit_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=b"<wo-response-error><wo-status-code>429</wo-status-code><wo-error-code>rate_limit_error</wo-error-code><wo-error-message>retry later</wo-error-message></wo-response-error>",
            headers={"X-RateLimit-Reset": "48"},
        )

    client = WipoPatentScopeRestClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert excinfo.value.code is ErrorCode.SOURCE_RATE_LIMIT
    assert excinfo.value.details["X-RateLimit-Reset"] == "48"
