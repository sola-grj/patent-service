from pathlib import Path

import fitz
from fastapi.testclient import TestClient

from app.api.routes import get_analysis_service, get_ocr_engine
from app.main import app
from app.models.patents import PatentAnalysisResponse


class StubAnalysisService:
    async def analyze_patent(self, patent_number: str, *, cancellation=None):
        return PatentAnalysisResponse(
            input_mode="patent_number", status="success", patent_number=patent_number
        )

    def analyze_uploads(self, uploads, *, cancellation=None):
        return PatentAnalysisResponse(input_mode="upload", status="success")


def test_ocr_engine_is_process_scoped():
    get_ocr_engine.cache_clear()

    first = get_ocr_engine()
    second = get_ocr_engine()

    assert first is second


def test_analyze_requires_exactly_one_input_mode():
    app.dependency_overrides[get_analysis_service] = lambda: StubAnalysisService()
    client = TestClient(app)

    missing = client.post("/api/patents/analyze")
    both = client.post(
        "/api/patents/analyze",
        data={"patent_number": "EP1234567A1"},
        files={"files": ("patent.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )

    app.dependency_overrides.clear()
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "ambiguous_analysis_input"
    assert both.status_code == 422
    assert both.json()["error"]["code"] == "ambiguous_analysis_input"


def test_analyze_rejects_unsupported_and_disguised_uploads():
    app.dependency_overrides[get_analysis_service] = lambda: StubAnalysisService()
    client = TestClient(app)

    unsupported = client.post(
        "/api/patents/analyze",
        files={"files": ("notes.txt", b"hello", "text/plain")},
    )
    disguised = client.post(
        "/api/patents/analyze",
        files={"files": ("patent.pdf", b"not a pdf", "application/pdf")},
    )

    app.dependency_overrides.clear()
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "unsupported_file_type"
    assert disguised.status_code == 422
    assert disguised.json()["error"]["code"] == "file_signature_mismatch"


def test_analyze_accepts_valid_pdf_upload(tmp_path: Path):
    path = tmp_path / "valid.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "ABSTRACT example")
    document.save(path)
    document.close()
    app.dependency_overrides[get_analysis_service] = lambda: StubAnalysisService()
    client = TestClient(app)

    with path.open("rb") as stream:
        response = client.post(
            "/api/patents/analyze",
            files={"files": ("valid.pdf", stream, "application/pdf")},
        )

    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["input_mode"] == "upload"
