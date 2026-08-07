import asyncio
import io
import time
from pathlib import Path

import fitz
import httpx
from PIL import Image

from app.clients.epo_ops import EpoOpsClient, _merge_ops_document_pages
from app.config import Settings
from app.models.patents import PatentSource
from app.utils.patent_numbers import normalize_patent_number
from app.utils.text_metrics import count_words

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_parse_bibliographic_data():
    xml_text = (FIXTURES_DIR / "epo_biblio.xml").read_text(encoding="utf-8")

    basic_info, raw_refs = EpoOpsClient.parse_bibliographic_data(xml_text)

    assert basic_info.title == "Example Widget"
    assert basic_info.abstract == "Example abstract text."
    assert basic_info.publication_date == "20260701"
    assert basic_info.application_number == "EP2026000123"
    assert basic_info.applicants == ["Example Applicant GmbH"]
    assert basic_info.inventors == ["Jane Inventor"]
    assert basic_info.representatives[0].name == "Example Patent Attorneys GmbH"
    assert basic_info.representatives[0].country == "DE"
    assert basic_info.ipc == ["A01B 1/00"]
    assert basic_info.cpc == ["B01D 53/00"]
    assert raw_refs["publication_reference"]["selected_number"] == "EP1234567A1"
    assert raw_refs["application_reference"]["selected_number"] == "EP2026000123"
    assert raw_refs["application_reference"]["selected_date"] == "20260115"
    assert raw_refs["title_language"] == "EN"
    assert raw_refs["first_priority_date"] == "20231013"
    assert raw_refs["priority_data"][0].model_dump() == {
        "number": "PA202300999",
        "date": "20231013",
        "country": "DK",
        "kind": "national",
    }
    assert raw_refs["publication_language"] == "EN"
    assert raw_refs["filing_language"] == "DE"
    assert raw_refs["designated_states"].countries == ["DE", "FR"]


def test_parse_family_international_filing_date():
    xml_text = (FIXTURES_DIR / "epo_family.xml").read_text(encoding="utf-8")

    filing_date, raw_refs = EpoOpsClient.parse_family_international_filing_date(
        xml_text
    )

    assert filing_date == "20241011"
    assert raw_refs["wo_members"] == [
        {
            "publication_number": "WO2025076543A1",
            "application_number": "DK2024050123W",
            "filing_date": "20241011",
        }
    ]
    assert raw_refs["family_publications"] == [
        {
            "number": "WO2025076543",
            "country": "WO",
            "doc_number": "2025076543",
            "kind": "A1",
            "date": "20250417",
        },
        {
            "number": "AT528631",
            "country": "AT",
            "doc_number": "528631",
            "kind": "A1",
            "date": "20260315",
        },
    ]


def test_parse_register_bibliographic_data():
    xml_text = (FIXTURES_DIR / "epo_register_biblio.xml").read_text(encoding="utf-8")

    refs = EpoOpsClient.parse_register_bibliographic_data(xml_text)

    assert refs["agents"][0].name == "Example EP Agent"
    assert refs["agents"][0].country == "NL"
    assert refs["publication_reference"]["selected_number"] == "EP1000000A1"
    assert refs["application_reference"]["selected_number"] == "EP25188322"
    assert refs["application_reference"]["selected_date"] == "20250709"
    assert refs["priority_data"][0].number == "19981010536"
    assert refs["publication_language"] == "EN"
    assert refs["filing_language"] == "NL"
    assert refs["designated_states"].regions == ["EP"]
    assert refs["designated_states"].countries == ["AT", "DE"]


def test_parse_description_data():
    xml_text = (FIXTURES_DIR / "epo_description.xml").read_text(encoding="utf-8")

    content, raw_refs = EpoOpsClient.parse_description_data(xml_text)

    assert content.language == "EN"
    assert content.drawing_labels == [
        "FIG. 1 is a schematic view of the widget.",
        "FIG. 2 is another schematic view of the widget.",
    ]
    assert count_words(content.text) == 32
    assert raw_refs["selected_language"] == "EN"


def test_parse_claims_data():
    xml_text = (FIXTURES_DIR / "epo_claims.xml").read_text(encoding="utf-8")

    content, raw_refs = EpoOpsClient.parse_claims_data(xml_text)

    assert content.language == "EN"
    assert content.claims_count == 2
    assert count_words(" ".join(content.claim_texts)) == 17
    assert raw_refs["claim_text_count"] == 3


def test_parse_original_file_availability():
    xml_text = (FIXTURES_DIR / "epo_images.xml").read_text(encoding="utf-8")

    original_file, raw_refs = EpoOpsClient.parse_original_file_availability(xml_text)

    assert original_file.available is True
    assert original_file.content_type == "application/pdf"
    assert original_file.filename == "EP1234567A1.pdf"
    assert raw_refs["document_instance_link"] == "EP/1234567/A1/fullimage"
    assert raw_refs["drawing_page_count"] == 7
    assert raw_refs["has_drawings"] is True
    assert raw_refs["page_count"] == 14


def test_national_bibliographic_candidates_are_ordered_and_canonicalized():
    client = EpoOpsClient(Settings())
    reference = normalize_patent_number(
        "US20210184727A1",
        source_override=PatentSource.EPO,
    )

    candidates = client.build_biblio_candidate_paths(reference)
    canonical = client.reference_from_bibliographic_data(
        reference,
        {
            "publication_reference": {
                "country": "US",
                "doc_number": "20210184727",
                "kind": "A1",
                "ids": {
                    "epodoc": {
                        "doc_number": "US2021184727",
                        "kind": "A1",
                    }
                },
            }
        },
    )

    assert candidates[0].endswith("/epodoc/US2021184727/biblio")
    assert candidates[1].endswith("/docdb/US.2021184727.A1/biblio")
    assert "q=pn%3DUS2021184727A1" in candidates[2]
    assert canonical.normalized_number == "US2021184727A1"
    assert canonical.lookup_number == "US2021184727"


def test_ops_page_download_retries_429_and_sends_page_range():
    seen_ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_ranges.append(request.headers.get("X-OPS-Range"))
        if len(seen_ranges) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"page")

    async def run() -> bytes:
        client = EpoOpsClient(Settings())
        await client._http_client.aclose()
        client._http_client = httpx.AsyncClient(
            base_url="https://ops.example",
            transport=httpx.MockTransport(handler),
        )
        client._access_token = "token"
        client._access_token_expires_at = time.time() + 60
        try:
            return await client._get_document_page(
                "/published-data/example/fullimage",
                page_number=7,
                accept="application/pdf",
            )
        finally:
            await client._http_client.aclose()

    assert asyncio.run(run()) == b"page"
    assert seen_ranges == ["7", "7"]


def test_merge_ops_pdf_and_tiff_pages(tmp_path: Path):
    pdf = fitz.open()
    pdf.new_page()
    pdf_page = pdf.tobytes()
    pdf.close()
    tiff_buffer = io.BytesIO()
    Image.new("RGB", (12, 12), "white").save(tiff_buffer, format="TIFF")

    pdf_output = tmp_path / "pdf-pages.pdf"
    tiff_output = tmp_path / "tiff-pages.pdf"
    _merge_ops_document_pages([pdf_page, pdf_page], "application/pdf", pdf_output)
    _merge_ops_document_pages(
        [tiff_buffer.getvalue()],
        "image/tiff",
        tiff_output,
    )

    with fitz.open(pdf_output) as merged_pdf:
        assert merged_pdf.page_count == 2
    with fitz.open(tiff_output) as merged_tiff:
        assert merged_tiff.page_count == 1
