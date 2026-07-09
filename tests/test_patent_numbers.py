import asyncio

import pytest

from app.errors import PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
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

    async def fetch_images_metadata(self, reference: PatentReference) -> str:
        return "<unused />"

    def parse_bibliographic_data(self, xml_text: str):
        return (
            PatentBasicInfo(title="EP result"),
            {"publication_reference": {"country": "EP", "doc_number": "1234567", "kind": "A1"}},
        )

    def parse_original_file_availability(self, xml_text: str):
        return (
            PatentOriginalFile(available=True, content_type="application/pdf", filename="EP1234567A1.pdf"),
            {"publication_reference": {"country": "EP", "doc_number": "1234567", "kind": "A1"}},
        )

    def build_biblio_path(self, reference: PatentReference) -> str:
        return "/published-data/publication/epodoc/example/biblio"

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
    assert ep_response.original_file.download_url.endswith("/EP/1234567/A1.pdf")
    assert wo_response.source is PatentSource.WIPO
