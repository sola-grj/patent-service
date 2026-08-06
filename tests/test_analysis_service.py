import asyncio
import logging
import zipfile
from pathlib import Path

import httpx
import pytest
from app.analysis.common import AnalysisDraft
from app.analysis.artifacts import AnalysisArtifactStore
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
    PatentReference,
    PatentSource,
)
from app.utils.patent_numbers import normalize_patent_number


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


class WorkspaceWipoLookup:
    def __init__(self) -> None:
        self.workspace: Path | None = None

    async def lookup_patent_full(self, request, *, storage_dir: Path):
        self.workspace = storage_dir
        archive_path = storage_dir / "WO2026044310A1_PAMPH.zip"
        pdf_path = storage_dir / "WO2026044310A1.pdf"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "wo-published-application.xml",
                "<wo-published-application>"
                "<abstract><p>small cover</p></abstract>"
                "<description><p>detailed body</p></description>"
                "<claims><claim>one claim</claim></claims>"
                "</wo-published-application>",
            )
        pdf_path.write_bytes(b"%PDF-prepared")
        return PatentLookupResponse(
            source=PatentSource.WIPO,
            normalized_number="WO2026044310A1",
            display_number="WO/2026/044310",
            basic_info=PatentBasicInfo(),
            original_file=PatentOriginalFile(
                available=True,
                content_type="application/pdf",
                filename=pdf_path.name,
                storage_path=str(pdf_path),
            ),
            raw_source_refs={
                "original_archive": {
                    "filename": archive_path.name,
                    "storage_path": str(archive_path),
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


def test_wipo_analysis_promotes_pdf_and_removes_source_workspace(tmp_path: Path):
    lookup = WorkspaceWipoLookup()
    store = AnalysisArtifactStore(
        Settings(analysis_artifact_dir=str(tmp_path / "artifacts"))
    )
    service = PatentAnalysisService(
        settings=Settings(analysis_artifact_dir=str(tmp_path / "artifacts")),
        lookup_service=lookup,
        epo_publication_server_client=EpoPublicationServerClient(
            "https://example.test"
        ),
        artifact_store=store,
        ocr=EmptyOcr(),
    )

    response = asyncio.run(service.analyze_patent("WO2026044310A1"))

    assert response.artifact is not None
    assert store.read_bytes(response.artifact) == b"%PDF-prepared"
    assert response.source_document is not None
    assert response.source_document.strategy == "generated_cache"
    assert response.source_document.normalized_number == "WO2026044310A1"
    assert lookup.workspace is not None
    assert not lookup.workspace.exists()


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
    assert response.artifact is None
    assert response.source_document is not None
    assert response.source_document.strategy == "external_url"
    assert response.source_document.normalized_number == "EP1234567A1"
    assert response.source_document.kind_code == "A1"
    assert response.source_document.upstream_url == (
        "https://data.example/patents/EP1234567NWA1/document.pdf"
    )
    assert response.aggregate.total_words == 6


def test_epo_application_number_resolves_before_archive_download(tmp_path: Path):
    xml = (
        b"<ep-patent-document><abstract lang='en'><p>small cover</p></abstract>"
        b"<description><p>detailed body</p></description>"
        b"<claims><claim>one claim</claim></claims></ep-patent-document>"
    )
    archive_file = tmp_path / "application-package.zip"
    with zipfile.ZipFile(archive_file, "w") as archive:
        archive.writestr("EP.xml", xml)
    package = archive_file.read_bytes()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.endswith("document.pdf"):
            return httpx.Response(404)
        return httpx.Response(200, content=package)

    class ApplicationResolver:
        def __init__(self) -> None:
            self.requested: list[PatentReference] = []

        async def resolve_ep_publication_reference(
            self, reference: PatentReference
        ) -> PatentReference:
            self.requested.append(reference)
            return normalize_patent_number("EP4686382A1")

    resolver = ApplicationResolver()
    service = PatentAnalysisService(
        settings=Settings(),
        lookup_service=resolver,
        epo_publication_server_client=EpoPublicationServerClient(
            "https://data.example/patents",
            transport=httpx.MockTransport(handler),
        ),
        ocr=EmptyOcr(),
    )

    response = asyncio.run(service.analyze_patent("EP25188322.9"))

    assert resolver.requested[0].reference_type == "application"
    assert requested[0].endswith("/EP4686382NWA1/document.zip")
    assert all("EP25188322NW" not in url for url in requested)
    assert response.patent_number == "EP25188322.9"
    assert response.files[0].filename == "EP4686382A1.zip"
    assert response.source_document is not None
    assert response.source_document.normalized_number == "EP4686382A1"
    assert response.source_document.upstream_url == (
        "https://data.example/patents/EP4686382NWA1/document.pdf"
    )
    assert response.aggregate.total_words == 6


def test_epo_pdf_proxy_rejects_untrusted_url():
    client = EpoPublicationServerClient("https://data.example/patents")

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(
            client.download_pdf_url(
                "https://attacker.example/patents/EP1234567NWA1/document.pdf",
                max_bytes=1024,
            )
        )

    assert excinfo.value.code is ErrorCode.SOURCE_ACCESS_DENIED


def test_epo_pdf_proxy_rejects_non_pdf_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not a PDF</html>",
            headers={"content-type": "text/html"},
        )

    client = EpoPublicationServerClient(
        "https://data.example/patents",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(
            client.download_pdf_url(
                "https://data.example/patents/EP1234567NWA1/document.pdf",
                max_bytes=1024,
            )
        )

    assert excinfo.value.code is ErrorCode.UPSTREAM_RESPONSE_INVALID


def test_epo_pdf_proxy_rejects_redirect_to_untrusted_host():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            302,
            headers={
                "location": (
                    "https://attacker.example/patents/EP1234567NWA1/document.pdf"
                )
            },
        )

    client = EpoPublicationServerClient(
        "https://data.example/patents",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(
            client.download_pdf_url(
                "https://data.example/patents/EP1234567NWA1/document.pdf",
                max_bytes=1024,
            )
        )

    assert excinfo.value.code is ErrorCode.SOURCE_ACCESS_DENIED
    assert seen == [
        "https://data.example/patents/EP1234567NWA1/document.pdf"
    ]


def test_epo_pdf_proxy_rejects_oversized_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-too-large",
            headers={"content-type": "application/pdf"},
        )

    client = EpoPublicationServerClient(
        "https://data.example/patents",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(
            client.download_pdf_url(
                "https://data.example/patents/EP1234567NWA1/document.pdf",
                max_bytes=5,
            )
        )

    assert excinfo.value.code is ErrorCode.UPSTREAM_RESPONSE_INVALID


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
