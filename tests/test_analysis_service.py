import asyncio
import logging
import zipfile
from pathlib import Path

import httpx
from app.analysis.common import AnalysisDraft
from app.analysis.ocr import OcrResult
from app.analysis.service import PatentAnalysisService, _build_response
from app.clients.epo_ops import EpoClaimsContent, EpoDescriptionContent
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentSource,
)


class EmptyOcr:
    def recognize(
        self, image_bytes: bytes, *, sparse: bool = False, language: str | None = None
    ) -> OcrResult:
        return OcrResult()


class FailingOcr:
    def recognize(
        self, image_bytes: bytes, *, sparse: bool = False, language: str | None = None
    ) -> OcrResult:
        return OcrResult(warnings=["OCR backend unavailable"])


class FakeEpoOps:
    def __init__(self) -> None:
        self.requested: list[tuple[str, str]] = []

    async def fetch_bibliographic_data(self, reference):
        self.requested.append(("biblio", reference.kind_code))
        return "biblio"

    async def fetch_description_data(self, reference):
        self.requested.append(("description", reference.kind_code))
        return "description"

    async def fetch_claims_data(self, reference):
        self.requested.append(("claims", reference.kind_code))
        return "claims"

    @staticmethod
    def parse_bibliographic_data(_payload):
        return PatentBasicInfo(abstract="OPS preferred abstract"), {}

    @staticmethod
    def parse_description_data(_payload, **_kwargs):
        return EpoDescriptionContent(
            text="OPS preferred detailed description",
            language="en",
            paragraphs=[],
            drawing_labels=[],
        ), {}

    @staticmethod
    def parse_claims_data(_payload, **_kwargs):
        return EpoClaimsContent(
            language="en",
            claim_texts=["OPS preferred claim"],
            claims_count=1,
        ), {}


class WipoArchiveLookup:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def lookup_patent(self, request):
        return PatentLookupResponse(
            source=PatentSource.WIPO,
            normalized_number="WO2026044310A1",
            display_number="WO/2026/044310",
            basic_info=PatentBasicInfo(),
            original_file=PatentOriginalFile(available=True),
            raw_source_refs={
                "original_archive": {
                    "filename": "WO2026044310A1.zip",
                    "storage_path": str(self.path),
                }
            },
        )


def test_multi_file_aggregate_keeps_exact_duplicates_and_warns():
    left = AnalysisDraft(filename="one.docx", file_type="docx", sha256="same")
    right = AnalysisDraft(filename="two.docx", file_type="docx", sha256="same")
    for draft in (left, right):
        draft.add_text(
            "unclassified",
            "one two three four five six",
            method="docx_xml",
            confidence="low",
            status="unclassified",
        )

    response = _build_response(input_mode="upload", drafts=[left, right])

    assert response.aggregate.total_words == 12
    assert response.status == "partial"
    assert response.warnings[0].code == "possible_duplicate_content"
    assert response.warnings[0].details["exact"] is True


def test_wipo_patent_mode_uses_preserved_official_archive(
    tmp_path: Path, caplog
):
    archive_path = tmp_path / "WO.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "wo-published-application.xml",
            "<wo-published-application><abstract lang='en'><p>small cover</p></abstract>"
            "<description><p>detailed body</p></description>"
            "<claims><claim>one claim</claim></claims></wo-published-application>",
        )
    service = PatentAnalysisService(
        settings=Settings(),
        lookup_service=WipoArchiveLookup(archive_path),
        epo_publication_server_client=EpoPublicationServerClient("https://example.test"),
        ocr=EmptyOcr(),
    )

    with caplog.at_level(logging.INFO, logger="patent_service"):
        response = asyncio.run(service.analyze_patent("WO2026044310A1"))

    assert response.input_mode == "patent_number"
    assert response.patent_number == "WO2026044310A1"
    assert response.files[0].file_type == "wipo_zip"
    assert response.aggregate.abstract_words == 2
    assert response.aggregate.description_words == 2
    assert response.aggregate.claims_words == 2
    assert "source=wipo step=official_lookup service=PATENTSCOPE" in caplog.text
    assert "source=wipo step=pamphlet_zip action=download_complete" in caplog.text
    assert "source=wipo section=description decision=xml method=wipo_xml" in caplog.text
    assert "patent analysis completed patent_number=WO2026044310A1" in caplog.text


def test_wipo_patent_mode_does_not_return_misleading_partial_total_when_core_ocr_fails(
    tmp_path: Path,
):
    archive_path = tmp_path / "WO-pages.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "wo-published-application.xml",
            "<wo-published-application><abstract lang='en'><p>small cover</p></abstract>"
            "<description><doc-page file='de.tif'/></description>"
            "<claims><doc-page file='cl.tif'/></claims></wo-published-application>",
        )
        archive.writestr("de.tif", b"description page")
        archive.writestr("cl.tif", b"claims page")
    service = PatentAnalysisService(
        settings=Settings(),
        lookup_service=WipoArchiveLookup(archive_path),
        epo_publication_server_client=EpoPublicationServerClient("https://example.test"),
        ocr=FailingOcr(),
    )

    try:
        asyncio.run(service.analyze_patent("WO2026044310A1"))
    except PatentServiceError as exc:
        assert exc.code is ErrorCode.OCR_FAILED
        assert exc.status_code == 503
        assert exc.details["partial_result"]["total_words"] == 2
        assert exc.details["incomplete_parts"] == {
            "description": "error",
            "claims": "error",
        }
    else:
        raise AssertionError("Expected OCR failure for missing core sections")


def test_epo_publication_server_downloads_document_zip():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"PK\x03\x04archive")

    client = EpoPublicationServerClient(
        "https://data.example/patents",
        transport=httpx.MockTransport(handler),
    )

    payload = asyncio.run(
        client.download_archive(
            country_code="EP", doc_number="1234567", kind_code="A1"
        )
    )

    assert payload.startswith(b"PK")
    assert seen == ["https://data.example/patents/EP1234567NWA1/document.zip"]


def test_epo_b_kind_resolves_to_a1_archive(tmp_path: Path):
    xml = (
        b"<ep-patent-document><abstract lang='en'><p>small cover</p></abstract>"
        b"<description><p>detailed body</p></description>"
        b"<claims><claim>one claim</claim></claims></ep-patent-document>"
    )
    archive_file = tmp_path / "package.zip"
    with zipfile.ZipFile(archive_file, "w") as archive:
        archive.writestr("EP.xml", xml)
    package = archive_file.read_bytes()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=package)

    publication_client = EpoPublicationServerClient(
        "https://data.example/patents", transport=httpx.MockTransport(handler)
    )
    service = PatentAnalysisService(
        settings=Settings(),
        lookup_service=None,
        epo_publication_server_client=publication_client,
        ocr=EmptyOcr(),
    )

    response = asyncio.run(service.analyze_patent("EP1234567B1"))

    assert requested[0].endswith("/EP1234567NWA1/document.zip")
    assert response.files[0].filename == "EP1234567A1.zip"
    assert response.aggregate.total_words == 6


def test_epo_analysis_prefers_ops_text_and_uses_archive_for_drawings(
    tmp_path: Path, caplog
):
    xml = b"""<ep-patent-document>
      <abstract lang='en'><p>archive abstract</p></abstract>
      <description lang='en'><p>archive description</p></description>
      <claims lang='en'><claim>archive claim</claim></claims>
      <drawings><img file='drawing.bin'/></drawings>
    </ep-patent-document>"""
    archive_file = tmp_path / "package.zip"
    with zipfile.ZipFile(archive_file, "w") as archive:
        archive.writestr("EP.xml", xml)
        archive.writestr("drawing.bin", b"drawing")
    package = archive_file.read_bytes()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=package)

    class DrawingOcr(EmptyOcr):
        def recognize(self, image_bytes, *, sparse=False, language=None):
            return OcrResult(
                text="10 valve", confidence=90, provider="rapidocr", language=language or ""
            )

    ops = FakeEpoOps()
    service = PatentAnalysisService(
        settings=Settings(),
        lookup_service=None,
        epo_publication_server_client=EpoPublicationServerClient(
            "https://data.example/patents", transport=httpx.MockTransport(handler)
        ),
        epo_ops_client=ops,
        ocr=DrawingOcr(),
    )

    with caplog.at_level(logging.INFO, logger="patent_service"):
        response = asyncio.run(service.analyze_patent("EP1234567A1"))
    result = response.files[0]

    assert result.parts.abstract.method == "epo_ops"
    assert result.parts.description.method == "epo_ops"
    assert result.parts.claims.method == "epo_ops"
    assert result.parts.description_drawings.method == "rapidocr"
    assert response.aggregate.total_words == 12
    assert ops.requested == [
        ("biblio", "A1"),
        ("description", "A1"),
        ("claims", "A1"),
    ]
    assert "source=epo step=ops service=EPO_OPS action=fetch_start" in caplog.text
    assert "source=epo step=publication_zip service=EPO_Publication_Server action=download_start" in caplog.text
    assert "source=epo section=description decision=xml method=epo_xml" in caplog.text
    assert "source=epo section=description_drawings action=start" in caplog.text
    assert "step=official_source_merge action=complete" in caplog.text
