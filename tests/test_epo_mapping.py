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
