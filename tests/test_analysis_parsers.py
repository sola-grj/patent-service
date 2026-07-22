import zipfile
import io
from pathlib import Path

import fitz
import pytest
from PIL import Image

from app.analysis.ocr import OcrResult, RapidOcrEngine
from app.analysis.pdf import PdfPatentParser, _text_layer_is_reliable
from app.analysis.structured import StructuredPatentParser, _clean_ocr_text
from app.analysis.word import WordPatentParser
from app.config import Settings


class FakeOcr:
    def __init__(self) -> None:
        self.languages: list[str | None] = []

    def recognize(
        self,
        image_bytes: bytes,
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> OcrResult:
        self.languages.append(language)
        values = {
            b"ABSTRACT_IMAGE": "10 valve",
            b"DRAWING_IMAGE": "20 inlet",
            b"DOCX_IMAGE": "30 outlet",
            b"DESCRIPTION_PAGE": "fallback description",
            b"CLAIMS_PAGE": "fallback claim",
        }
        return OcrResult(
            text=values.get(image_bytes, "ABSTRACT scanned text"),
            confidence=85,
            language="eng",
        )


class BatchOcr(FakeOcr):
    def __init__(self) -> None:
        super().__init__()
        self.batch_sizes: list[int] = []

    def recognize_many(self, images, *, sparse=False, language=None):
        self.batch_sizes.append(len(images))
        return [
            self.recognize(image, sparse=sparse, language=language)
            for image in images
        ]


def test_structured_ocr_cleanup_removes_headers_and_footers_but_keeps_figure_numbers():
    text = (
        "WO 2026/044310\nPCT/AT2025/060321\n- 5 -\n"
        "Fig. 2 3 7 10\n2025 08 14\nBa/Ec"
    )

    assert _clean_ocr_text(text) == "Fig. 2 3 7 10"


def test_wipo_package_uses_xml_and_pag_list_page_classification(tmp_path: Path):
    archive_path = tmp_path / "WO.zip"
    xml = b"""<wo-published-application>
      <abstract lang="en"><p>compact cover</p><abstract-figure><img file="ab.bin"/></abstract-figure></abstract>
      <description><doc-page file="de.bin"/></description>
      <claims><doc-page file="cl.bin"/></claims>
      <drawings><doc-page file="dr.bin"/></drawings>
    </wo-published-application>"""
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("wo-published-application.xml", xml)
        archive.writestr("Pag.lst", b"<DP IMA=de.bin DE=1>\n<DP IMA=cl.bin CL=1>\n<DP IMA=dr.bin DR=1>")
        archive.writestr("ab.bin", b"ABSTRACT_IMAGE")
        archive.writestr("de.bin", b"DESCRIPTION_PAGE")
        archive.writestr("cl.bin", b"CLAIMS_PAGE")
        archive.writestr("dr.bin", b"DRAWING_IMAGE")

    result = StructuredPatentParser(Settings(), FakeOcr()).parse(
        archive_path, source="wipo"
    ).to_result()

    assert result.parts.abstract.word_count == 2
    assert result.parts.abstract_drawing.word_count == 2
    assert result.parts.description.word_count == 2
    assert result.parts.claims.word_count == 2
    assert result.parts.description_drawings.word_count == 2
    assert result.total_words == 10


def test_wipo_scan_pages_are_submitted_as_a_batch(tmp_path: Path):
    archive_path = tmp_path / "WO-batch.zip"
    xml = b"""<wo-published-application lang="en">
      <abstract lang="en"><p>compact cover</p></abstract>
      <description><doc-page file="de1.bin"/><doc-page file="de2.bin"/><doc-page file="de3.bin"/></description>
      <claims><p>one claim</p></claims>
    </wo-published-application>"""
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("wo-published-application.xml", xml)
        for name in ("de1.bin", "de2.bin", "de3.bin"):
            archive.writestr(name, b"DESCRIPTION_PAGE")

    ocr = BatchOcr()
    result = StructuredPatentParser(Settings(ocr_batch_size=4), ocr).parse(
        archive_path, source="wipo"
    ).to_result()

    assert ocr.batch_sizes == [3]
    assert result.parts.description.word_count == 6


def test_real_wipo_pamphlet_extracts_description_and_claims_with_packaged_ocr():
    archive_path = (
        Path(__file__).parents[1]
        / "artifacts"
        / "wipo-originals"
        / "WO2026044310A1_PAMPH.zip"
    )

    engine = RapidOcrEngine(Settings(ocr_backend="rapidocr"))
    if not engine.is_available():
        pytest.skip("RapidOCR runtime is not installed")
    result = StructuredPatentParser(Settings(), engine).parse(
        archive_path, source="wipo"
    ).to_result()

    assert result.parts.abstract.word_count == 87
    assert 1250 <= result.parts.description.word_count <= 1320
    assert 250 <= result.parts.claims.word_count <= 290
    assert result.parts.description.method == "rapidocr"
    assert result.parts.claims.method == "rapidocr"
    assert result.total_words >= 1650


def test_epo_package_does_not_treat_description_inline_image_as_drawing(tmp_path: Path):
    archive_path = tmp_path / "EP.zip"
    xml = b"""<ep-patent-document>
      <abstract lang="en"><p>compact cover</p><img file="ab.bin"/></abstract>
      <description lang="en"><p>detailed body</p><img file="formula.bin"/></description>
      <claims lang="en"><claim><claim-text>1. A cover.</claim-text></claim></claims>
      <drawings><img file="dr.bin"/></drawings>
    </ep-patent-document>"""
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("EP.xml", xml)
        archive.writestr("ab.bin", b"ABSTRACT_IMAGE")
        archive.writestr("formula.bin", b"SHOULD_NOT_BE_OCR")
        archive.writestr("dr.bin", b"DRAWING_IMAGE")

    result = StructuredPatentParser(Settings(), FakeOcr()).parse(
        archive_path, source="epo"
    ).to_result()

    assert result.parts.abstract_drawing.word_count == 2
    assert result.parts.description_drawings.word_count == 2
    assert result.parts.description.word_count == 2
    assert result.total_words == 11


def test_pdf_text_layer_is_split_without_full_page_ocr(tmp_path: Path):
    path = tmp_path / "patent.pdf"
    document = fitz.open()
    for text in (
        "ABSTRACT\nsmall locking cover",
        "DESCRIPTION\nA detailed technical body",
        "CLAIMS\n1. A locking cover",
        "DRAWINGS\n10 inlet",
    ):
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()

    result = PdfPatentParser(Settings(), FakeOcr()).parse(
        path, filename=path.name
    ).to_result()

    assert result.parts.abstract.method == "pdf_text"
    assert result.parts.abstract.word_count == 3
    assert result.parts.description.word_count == 4
    assert result.parts.claims.word_count == 4
    assert result.parts.description_drawings.word_count == 2
    assert result.drawing_ocr_words == 2


def test_pdf_cover_excludes_bibliographic_text_and_second_language_abstract(tmp_path: Path):
    path = tmp_path / "cover.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Applicant Example Corp\n(54) Title: COVER\n3 7 10\nFig. 2\n"
        "(57) Abstract: first language only\n(57) Zusammenfassung: zweite sprache",
    )
    document.save(path)
    document.close()

    result = PdfPatentParser(Settings(), FakeOcr()).parse(
        path, filename=path.name
    ).to_result()

    assert result.parts.abstract.word_count == 3
    assert result.parts.abstract_drawing.word_count == 5
    assert result.parts.unclassified.word_count == 0
    assert result.total_words == 8


def test_scanned_pdf_falls_back_to_ocr(tmp_path: Path):
    image_stream = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(image_stream, format="PNG")
    path = tmp_path / "scan.pdf"
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_image(page.rect, stream=image_stream.getvalue())
    document.save(path)
    document.close()

    result = PdfPatentParser(Settings(), FakeOcr()).parse(
        path, filename=path.name
    ).to_result()

    assert result.parts.abstract.method == "ocr"
    assert result.parts.abstract.word_count == 2


def test_pdf_rejects_garbled_but_nonempty_text_layer():
    assert not _text_layer_is_reliable("Ã¼ Ã¶ broken text")
    assert _text_layer_is_reliable("A reliable technical description")


def test_pdf_ocr_embedded_drawing_on_text_page(tmp_path: Path):
    image_stream = io.BytesIO()
    Image.new("RGB", (500, 300), "white").save(image_stream, format="PNG")
    path = tmp_path / "mixed.pdf"
    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "ABSTRACT\nsmall cover")
    second = document.new_page()
    second.insert_text((72, 72), "DESCRIPTION\ndetailed technical body")
    second.insert_image(fitz.Rect(72, 150, 500, 450), stream=image_stream.getvalue())
    document.save(path)
    document.close()

    class DrawingOcr(FakeOcr):
        def recognize(self, image_bytes, *, sparse=False, language=None):
            self.languages.append(language)
            return OcrResult(
                text="Fig. 2 10 valve", confidence=90, provider="rapidocr"
            )

    ocr = DrawingOcr()
    result = PdfPatentParser(Settings(), ocr).parse(
        path, filename=path.name
    ).to_result()

    assert result.parts.description.word_count == 3
    assert result.parts.description_drawings.word_count == 4
    assert result.parts.description_drawings.method == "rapidocr"
    assert ocr.languages == ["en"]


def test_pdf_does_not_ocr_full_page_scan_again_when_text_layer_is_reliable(
    tmp_path: Path,
):
    image_stream = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(image_stream, format="PNG")
    path = tmp_path / "hidden-text.pdf"
    document = fitz.open()
    cover = document.new_page(width=600, height=800)
    cover.insert_text((72, 72), "ABSTRACT\nsmall cover")
    page = document.new_page(width=600, height=800)
    page.insert_image(page.rect, stream=image_stream.getvalue())
    page.insert_text((72, 72), "DESCRIPTION\ndetailed technical body")
    document.save(path)
    document.close()

    ocr = FakeOcr()
    result = PdfPatentParser(Settings(), ocr).parse(
        path, filename=path.name
    ).to_result()

    assert result.parts.description.word_count == 3
    assert result.parts.description_drawings.word_count == 0
    assert ocr.languages == []


def test_docx_body_and_embedded_image_are_added_once(tmp_path: Path):
    path = tmp_path / "patent.docx"
    document_xml = b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <w:body>
        <w:p><w:r><w:t>ABSTRACT</w:t></w:r></w:p>
        <w:p><w:r><w:t>small cover</w:t></w:r><w:r><a:blip r:embed="rId1"/></w:r></w:p>
        <w:p><w:r><w:t>DESCRIPTION</w:t></w:r></w:p>
        <w:p><w:r><w:t>detailed body</w:t></w:r><w:r><a:blip r:embed="rId1"/></w:r></w:p>
        <w:p><w:r><w:t>CLAIMS</w:t></w:r></w:p>
        <w:p><w:r><w:t>one claim</w:t></w:r></w:p>
      </w:body>
    </w:document>"""
    rels = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="image" Target="media/image1.bin"/>
    </Relationships>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", rels)
        archive.writestr("word/media/image1.bin", b"DOCX_IMAGE")

    result = WordPatentParser(Settings(), FakeOcr()).parse(
        path, filename=path.name
    ).to_result()

    assert result.parts.abstract.word_count == 2
    assert result.parts.abstract_drawing.word_count == 2
    assert result.parts.description.word_count == 2
    assert result.parts.description_drawings.word_count == 0
    assert result.parts.claims.word_count == 2
    assert result.document_text_words == 6
    assert result.drawing_ocr_words == 2
    assert result.total_words == 8


def test_docx_without_section_headings_preserves_text_as_unclassified(tmp_path: Path):
    path = tmp_path / "notes.docx"
    document_xml = b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <w:body><w:p><w:r><w:t>miscellaneous patent notes</w:t></w:r>
      <w:r><a:blip r:embed="rId1"/></w:r></w:p></w:body>
    </w:document>"""
    rels = b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="image" Target="media/image1.bin"/>
    </Relationships>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", rels)
        archive.writestr("word/media/image1.bin", b"DOCX_IMAGE")

    result = WordPatentParser(Settings(), FakeOcr()).parse(
        path, filename=path.name
    ).to_result()

    assert result.parts.unclassified.status == "unclassified"
    assert result.parts.unclassified.word_count == 5
    assert result.document_text_words == 3
    assert result.drawing_ocr_words == 2
    assert result.total_words == 5
