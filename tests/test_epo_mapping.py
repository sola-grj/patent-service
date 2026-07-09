from pathlib import Path

from app.clients.epo_ops import EpoOpsClient
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
    assert basic_info.ipc == ["A01B 1/00"]
    assert basic_info.cpc == ["B01D 53/00"]
    assert raw_refs["publication_reference"]["selected_number"] == "EP1234567A1"
    assert raw_refs["application_reference"]["selected_number"] == "EP2026000123"
    assert raw_refs["application_reference"]["selected_date"] == "20260115"


def test_parse_description_data():
    xml_text = (FIXTURES_DIR / "epo_description.xml").read_text(encoding="utf-8")

    content, raw_refs = EpoOpsClient.parse_description_data(xml_text)

    assert content.language == "EN"
    assert content.drawing_labels == [
        "FIG. 1 is a schematic view of the widget.",
        "FIG. 2 is another schematic view of the widget.",
    ]
    assert count_words(content.text) == 26
    assert raw_refs["selected_language"] == "EN"


def test_parse_claims_data():
    xml_text = (FIXTURES_DIR / "epo_claims.xml").read_text(encoding="utf-8")

    content, raw_refs = EpoOpsClient.parse_claims_data(xml_text)

    assert content.language == "EN"
    assert content.claims_count == 2
    assert count_words(" ".join(content.claim_texts)) == 14
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
