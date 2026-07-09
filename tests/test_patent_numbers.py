import asyncio

import pytest

from app.errors import ErrorCode
from app.clients.epo_ops import EpoClaimsContent, EpoDescriptionContent
from app.errors import PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentDrawingsInfo,
    PatentLookupEpResponse,
    PatentLookupRequest,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentReference,
    PatentSource,
)
from app.services.patent_lookup import PatentLookupService
from app.utils.patent_numbers import normalize_patent_number


def test_normalize_ep_publication_number():
    reference = normalize_patent_number("EP 1234567 A1")
    assert reference.source is PatentSource.EPO
    assert reference.normalized_number == "EP1234567A1"
    assert reference.lookup_number == "EP1234567.A1"


def test_normalize_wo_publication_number():
    reference = normalize_patent_number("WO/2026/137030")
    assert reference.source is PatentSource.WIPO
    assert reference.normalized_number == "WO2026137030"
    assert reference.display_number == "WO/2026/137030"


@pytest.mark.parametrize(
    "value",
    ["", "123456", "US1234567A1", "WO/26/137030", "EPABC"],
)
def test_invalid_patent_numbers_raise(value: str):
    with pytest.raises(PatentServiceError):
        normalize_patent_number(value)


class StubEpoOpsClient:
    async def fetch_bibliographic_data(self, reference: PatentReference) -> str:
        return "<unused />"

    async def fetch_description_data(self, reference: PatentReference) -> str:
        return "<unused />"

    async def fetch_claims_data(self, reference: PatentReference) -> str:
        return "<unused />"

    async def fetch_images_metadata(self, reference: PatentReference) -> str:
        return "<unused />"

    def parse_bibliographic_data(self, xml_text: str):
        return (
            PatentBasicInfo(
                title="EP result",
                abstract="EP abstract",
                publication_date="20260701",
                application_number="EP2026000123",
                applicants=["Example Applicant"],
                inventors=["Example Inventor"],
            ),
            {
                "publication_reference": {
                    "country": "EP",
                    "doc_number": "1234567",
                    "kind": "A1",
                    "selected_date": "20260701",
                },
                "application_reference": {
                    "selected_number": "EP2026000123",
                    "selected_date": "20260115",
                },
            },
        )

    def parse_description_data(self, xml_text: str):
        return (
            EpoDescriptionContent(
                text="A sample description text.",
                language="EN",
                paragraphs=["A sample description text.", "FIG. 1 is a view."],
                drawing_labels=["FIG. 1 is a view."],
            ),
            {"selected_language": "EN"},
        )

    def parse_claims_data(self, xml_text: str):
        return (
            EpoClaimsContent(
                language="EN",
                claim_texts=["1. A sample claim.", "2. The claim of claim 1."],
                claims_count=2,
            ),
            {"selected_language": "EN"},
        )

    def parse_original_file_availability(self, xml_text: str):
        return (
            PatentOriginalFile(
                available=True,
                content_type="application/pdf",
                filename="EP1234567A1.pdf",
            ),
            {
                "publication_reference": {
                    "country": "EP",
                    "doc_number": "1234567",
                    "kind": "A1",
                },
                "has_drawings": True,
                "drawing_page_count": 3,
            },
        )

    def build_biblio_path(self, reference: PatentReference) -> str:
        return "/published-data/publication/epodoc/example/biblio"

    def build_description_path(self, reference: PatentReference) -> str:
        return "/published-data/publication/epodoc/example/description"

    def build_claims_path(self, reference: PatentReference) -> str:
        return "/published-data/publication/epodoc/example/claims"

    def build_images_path(self, reference: PatentReference) -> str:
        return "/published-data/publication/epodoc/example/images"


class StubPublicationServerClient:
    def build_pdf_download_url(self, *, country_code: str, doc_number: str, kind_code: str) -> str:
        return f"https://example.test/{country_code}/{doc_number}/{kind_code}.pdf"


class StubWipoClient:
    async def lookup_patent(self, reference: PatentReference, *, include_original_file: bool) -> PatentLookupResponse:
        return PatentLookupResponse(
            source=PatentSource.WIPO,
            normalized_number=reference.normalized_number,
            display_number=reference.display_number,
            basic_info=PatentBasicInfo(title="WO result"),
            original_file=PatentOriginalFile(available=include_original_file),
            raw_source_refs={"source": "stub"},
        )


class StubEpoOpsMissingTextClient(StubEpoOpsClient):
    async def fetch_description_data(self, reference: PatentReference) -> str:
        raise PatentServiceError(
            code=ErrorCode.SOURCE_NO_RESULT,
            status_code=404,
            message="missing description",
            source="epo",
        )

    async def fetch_claims_data(self, reference: PatentReference) -> str:
        raise PatentServiceError(
            code=ErrorCode.SOURCE_NO_RESULT,
            status_code=404,
            message="missing claims",
            source="epo",
        )

    def parse_original_file_availability(self, xml_text: str):
        return (
            PatentOriginalFile(
                available=True,
                content_type="application/pdf",
                filename="EP1234567A1.pdf",
            ),
            {
                "publication_reference": {
                    "country": "EP",
                    "doc_number": "1234567",
                    "kind": "A1",
                },
                "has_drawings": True,
                "drawing_page_count": 3,
            },
        )


def test_lookup_service_routes_ep_and_wo():
    service = PatentLookupService(
        epo_ops_client=StubEpoOpsClient(),
        epo_publication_server_client=StubPublicationServerClient(),
        wipo_client=StubWipoClient(),
    )

    ep_response = asyncio.run(
        service.lookup_patent(
            PatentLookupRequest(
                patent_number="EP1234567A1", include_original_file=True
            )
        )
    )
    wo_response = asyncio.run(
        service.lookup_patent(
            PatentLookupRequest(
                patent_number="WO2026137030A1", include_original_file=False
            )
        )
    )

    assert ep_response.source is PatentSource.EPO
    assert isinstance(ep_response, PatentLookupEpResponse)
    assert ep_response.publication_no == "EP1234567A1"
    assert ep_response.original_file_download_url.endswith("/EP/1234567/A1.pdf")
    assert ep_response.drawings == PatentDrawingsInfo(
        has_drawings=True,
        drawing_page_count=3,
        drawing_labels=["FIG. 1 is a view."],
    )
    assert wo_response.source is PatentSource.WIPO


def test_lookup_service_returns_warnings_when_description_or_claims_are_missing():
    service = PatentLookupService(
        epo_ops_client=StubEpoOpsMissingTextClient(),
        epo_publication_server_client=StubPublicationServerClient(),
        wipo_client=StubWipoClient(),
    )

    ep_response = asyncio.run(
        service.lookup_patent(
            PatentLookupRequest(
                patent_number="EP1234567A1", include_original_file=True
            )
        )
    )

    assert isinstance(ep_response, PatentLookupEpResponse)
    assert ep_response.description_words is None
    assert ep_response.claims_count is None
    assert ep_response.claims_words is None
    assert {warning.field for warning in ep_response.warnings} >= {
        "description_words",
        "claims_count",
        "claims_words",
    }
