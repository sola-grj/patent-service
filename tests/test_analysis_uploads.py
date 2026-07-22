import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.analysis import uploads as upload_module
from app.analysis.ocr import OcrResult
from app.analysis.service import PatentAnalysisService
from app.analysis.uploads import StoredUpload, convert_doc_to_docx
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError


class EmptyOcr:
    def recognize(
        self, image_bytes: bytes, *, sparse: bool = False, language: str | None = None
    ) -> OcrResult:
        return OcrResult()


def test_doc_conversion_reports_missing_libreoffice(monkeypatch, tmp_path: Path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1"))
    monkeypatch.setattr(upload_module, "_libreoffice_command", lambda settings: None)

    with pytest.raises(PatentServiceError) as captured:
        convert_doc_to_docx(source, Settings())

    assert captured.value.code is ErrorCode.DOCUMENT_CONVERSION_UNAVAILABLE


def test_doc_conversion_reports_timeout(monkeypatch, tmp_path: Path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1"))
    monkeypatch.setattr(upload_module, "_libreoffice_command", lambda settings: "soffice")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=1)

    monkeypatch.setattr(upload_module.subprocess, "run", timeout)
    with pytest.raises(PatentServiceError) as captured:
        convert_doc_to_docx(source, Settings(analysis_timeout_seconds=1))

    assert captured.value.code is ErrorCode.DOCUMENT_CONVERSION_FAILED


def test_doc_conversion_success_uses_generated_docx(monkeypatch, tmp_path: Path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1"))
    monkeypatch.setattr(upload_module, "_libreoffice_command", lambda settings: "soffice")

    def success(command, **kwargs):
        output_dir = Path(command[command.index("--outdir") + 1])
        converted = output_dir / "legacy.docx"
        with zipfile.ZipFile(converted, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(upload_module.subprocess, "run", success)

    converted = convert_doc_to_docx(source, Settings())

    assert converted.name == "legacy.docx"
    assert converted.is_file()


def test_one_parse_failure_returns_partial_multi_file_response(tmp_path: Path):
    broken_pdf = tmp_path / "broken.pdf"
    broken_pdf.write_bytes(b"%PDF-1.4\ninvalid")
    word_path = tmp_path / "notes.docx"
    with zipfile.ZipFile(word_path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
            "<w:body><w:p><w:r><w:t>valid notes</w:t></w:r></w:p></w:body></w:document>",
        )
    service = PatentAnalysisService(
        settings=Settings(),
        lookup_service=None,
        epo_publication_server_client=EpoPublicationServerClient("https://example.test"),
        ocr=EmptyOcr(),
    )

    response = service.analyze_uploads(
        [
            StoredUpload(broken_pdf, "broken.pdf", "pdf", "application/pdf"),
            StoredUpload(
                word_path,
                "notes.docx",
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ]
    )

    assert response.status == "partial"
    assert response.files[0].status == "failed"
    assert response.files[1].status == "success"
    assert response.aggregate.total_words == 2
