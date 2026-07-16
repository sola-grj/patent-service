from fastapi.testclient import TestClient

from app.api.routes import get_lookup_service
from app.errors import ErrorCode, PatentServiceError
from app.main import app
from app.models.patents import (
    PatentBasicInfo,
    PatentDrawingsInfo,
    PatentLookupEpResponse,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentSource,
)


class StubLookupService:
    async def lookup_patent(self, request):
        return PatentLookupEpResponse(
            source=PatentSource.EPO,
            normalized_number="EP1234567A1",
            display_number="EP1234567A1",
            title="Example Widget",
            abstract="Example abstract text.",
            ipc=["A01B 1/00"],
            cpc=["B01D 53/00"],
            applicants=["Example Applicant GmbH"],
            inventors=["Jane Inventor"],
            language="EN",
            first_priority_date="20231013",
            international_filing_date="20241011",
            filing_deadline_30_months="20260413",
            filing_deadline_31_months="20260513",
            application_date="20260115",
            application_no="EP2026000123",
            publication_date="20260701",
            publication_no="EP1234567A1",
            abstract_words=3,
            description_words=8,
            claims_count=2,
            claims_words=9,
            total_pages=73,
            drawings=PatentDrawingsInfo(
                has_drawings=True,
                drawing_page_count=2,
                drawing_labels=["FIG. 1 is a view."],
            ),
            original_file_download_url=(
                "https://data.epo.org/publication-server/rest/v1.2/patents/"
                "EP1234567NWA1/document.pdf"
            ),
            warnings=[],
            raw_source_refs={"ops_biblio": {"endpoint": "/published-data/example"}},
        )


class StubWipoLookupService:
    async def lookup_patent(self, request):
        return PatentLookupResponse(
            source=PatentSource.WIPO,
            normalized_number="WO2026137030A1",
            display_number="WO/2026/137030",
            basic_info=PatentBasicInfo(
                title="WO result",
                abstract="Example abstract text.",
            ),
            original_file=PatentOriginalFile(
                available=request.include_original_file,
                filename="WO2026137030A1.zip" if request.include_original_file else "",
                storage_path="D:/tmp/wipo.zip" if request.include_original_file else "",
            ),
            raw_source_refs={"source": "stub"},
        )


class ErrorLookupService:
    async def lookup_patent(self, request):
        raise PatentServiceError(
            code=ErrorCode.SOURCE_ACCESS_NOT_CONFIGURED,
            status_code=501,
            message="WIPO programmatic retrieval is not implemented in this first cut.",
            source="wipo",
            details={"normalized_number": request.patent_number},
        )


def test_lookup_response_contract_for_ep():
    app.dependency_overrides[get_lookup_service] = lambda: StubLookupService()
    client = TestClient(app)

    response = client.post(
        "/api/patents/lookup",
        json={"patent_number": "EP1234567A1", "include_original_file": True},
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "epo"
    assert payload["publication_no"] == "EP1234567A1"
    assert payload["application_no"] == "EP2026000123"
    assert payload["language"] == "EN"
    assert payload["first_priority_date"] == "20231013"
    assert payload["international_filing_date"] == "20241011"
    assert payload["filing_deadline_30_months"] == "20260413"
    assert payload["filing_deadline_31_months"] == "20260513"
    assert payload["total_pages"] == 73
    assert payload["drawings"]["has_drawings"] is True
    assert payload["original_file_download_url"].endswith(
        "/EP1234567NWA1/document.pdf"
    )
    assert payload["warnings"] == []
    assert "raw_source_refs" in payload


def test_lookup_response_contract_for_wo():
    app.dependency_overrides[get_lookup_service] = lambda: StubWipoLookupService()
    client = TestClient(app)

    response = client.post(
        "/api/patents/lookup",
        json={"patent_number": "WO2026137030A1", "include_original_file": False},
    )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "wipo"
    assert payload["basic_info"]["title"] == "WO result"
    assert payload["application_date"] is None
    assert payload["application_no"] is None
    assert payload["publication_date"] is None
    assert payload["publication_no"] is None
    assert payload["description_words"] is None
    assert payload["claims_count"] is None
    assert payload["claims_words"] is None
    assert payload["drawings"] == {
        "has_drawings": False,
        "drawing_page_count": None,
        "drawing_labels": [],
    }
    assert payload["original_file"]["available"] is False


def test_lookup_error_contract():
    app.dependency_overrides[get_lookup_service] = lambda: ErrorLookupService()
    client = TestClient(app)

    response = client.post(
        "/api/patents/lookup",
        json={"patent_number": "WO2026137030A1", "include_original_file": False},
    )

    app.dependency_overrides.clear()

    assert response.status_code == 501
    payload = response.json()
    assert payload["error"]["code"] == "source_access_not_configured"
    assert payload["error"]["source"] == "wipo"


def test_health_endpoint_reports_source_configuration():
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "sources" in payload
