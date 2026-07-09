import asyncio
import base64
from pathlib import Path

from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.config import Settings
from app.models.patents import PatentReference, PatentSource

WIPO_BIBLIO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<wo-patent-document country="WO" doc-number="2026137030" kind="A1" lang="en">
  <bibliographic-data country="WO" lang="en">
    <publication-reference>
      <document-id>
        <country>WO</country>
        <doc-number>2026137030</doc-number>
        <kind>A1</kind>
        <date>20260702</date>
      </document-id>
    </publication-reference>
    <application-reference appl-type="PCT">
      <document-id>
        <country>PCT</country>
        <doc-number>AT2025060458</doc-number>
        <date>20251219</date>
      </document-id>
    </application-reference>
    <classification-ipc>
      <main-classification>A47K 3/022</main-classification>
    </classification-ipc>
    <parties>
      <applicants>
        <applicant sequence="1">
          <addressbook>
            <orgname>OFNER, Daniela</orgname>
          </addressbook>
        </applicant>
      </applicants>
      <inventors>
        <inventor sequence="1">
          <addressbook>
            <first-name>Daniela</first-name>
            <last-name>OFNER</last-name>
          </addressbook>
        </inventor>
      </inventors>
    </parties>
    <invention-title>FOOT BATH UNIT</invention-title>
  </bibliographic-data>
  <abstract lang="en">
    <p>A compact foot bath unit.</p>
  </abstract>
</wo-patent-document>
"""

WIPO_DOCUMENTS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<available-documents>
  <document>
    <document-id>WO2026137030-PAMPH</document-id>
    <document-code>PAMPH</document-code>
    <mime-type>application/zip</mime-type>
    <file-name>WO2026137030A1_PAMPH.zip</file-name>
    <title>Published International Application</title>
  </document>
  <document>
    <document-id>WO2026137030-ISR</document-id>
    <document-code>ISR</document-code>
    <mime-type>application/pdf</mime-type>
    <file-name>WO2026137030A1_ISR.pdf</file-name>
    <title>International Search Report</title>
  </document>
</available-documents>
"""

WIPO_TOC_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<document-toc>
  <page-id>1</page-id>
  <page-id>2</page-id>
</document-toc>
"""


def _settings() -> Settings:
    return Settings(
        wipo_patentscope_service_url="https://example.test/servicesPatentScope?wsdl",
        wipo_patentscope_username="demo-user",
        wipo_patentscope_password="demo-pass",
    )


def _reference() -> PatentReference:
    return PatentReference(
        source=PatentSource.WIPO,
        normalized_number="WO2026137030A1",
        display_number="WO/2026/137030",
        country_code="WO",
        doc_number="2026137030",
        kind_code="A1",
        lookup_number="WO2026137030A1",
    )


class FakeWipoService:
    def getIASR(self, identifier: str) -> str:
        assert identifier in {
            "WO2026137030A1",
            "WO2026137030",
            "WO/2026/137030",
            "WO/2026/137030A1",
        }
        return WIPO_BIBLIO_XML

    def getAvailableDocuments(self, identifier: str) -> str:
        assert identifier in {
            "WO2026137030A1",
            "WO2026137030",
            "WO/2026/137030",
            "WO/2026/137030A1",
        }
        return WIPO_DOCUMENTS_XML

    def getDocumentTableOfContents(self, document_id: str) -> str:
        assert document_id == "WO2026137030-PAMPH"
        return WIPO_TOC_XML

    def getDocumentContent(self, document_id: str) -> dict[str, str]:
        assert document_id == "WO2026137030-PAMPH"
        return {
            "content": base64.b64encode(b"fake pamphlet payload").decode("ascii"),
            "mimeType": "application/zip",
            "fileName": "WO2026137030A1_PAMPH.zip",
        }


def test_parse_bibliographic_data():
    basic_info, raw_refs = WipoPatentScopeClient.parse_bibliographic_data(
        WIPO_BIBLIO_XML
    )

    assert basic_info.title == "FOOT BATH UNIT"
    assert basic_info.abstract == "A compact foot bath unit."
    assert basic_info.publication_date == "20260702"
    assert basic_info.application_number == "PCT/AT2025060458"
    assert basic_info.applicants == ["OFNER, Daniela"]
    assert basic_info.inventors == ["Daniela OFNER"]
    assert basic_info.ipc == ["A47K 3/022"]
    assert raw_refs["publication_reference"]["kind"] == "A1"


def test_parse_available_documents_prefers_pamph():
    selected, raw_refs = WipoPatentScopeClient.parse_available_documents(
        WIPO_DOCUMENTS_XML
    )

    assert selected is not None
    assert selected.document_code == "PAMPH"
    assert selected.document_id == "WO2026137030-PAMPH"
    assert raw_refs["selected_document_id"] == "WO2026137030-PAMPH"


def test_lookup_patent_materializes_original_file(tmp_path: Path):
    client = WipoPatentScopeClient(
        _settings(),
        service_factory=FakeWipoService,
        storage_dir=tmp_path,
    )

    response = asyncio.run(
        client.lookup_patent(_reference(), include_original_file=True)
    )

    assert response.source is PatentSource.WIPO
    assert response.basic_info.title == "FOOT BATH UNIT"
    assert response.original_file.available is True
    assert response.original_file.content_type == "application/zip"
    assert response.original_file.filename == "WO2026137030A1_PAMPH.zip"
    stored_path = Path(response.original_file.storage_path)
    assert stored_path.exists()
    assert stored_path.read_bytes() == b"fake pamphlet payload"
