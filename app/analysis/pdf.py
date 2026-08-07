import hashlib
import logging
import re
import time
from pathlib import Path

import fitz

from app.analysis.common import AnalysisDraft
from app.analysis.counting import count_units, tokenize_counting_units
from app.analysis.languages import detect_ocr_language
from app.analysis.ocr import OcrEngine, OcrResult, recognize_many
from app.analysis.sections import contains_search_report, detect_heading
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentAnalysisWarning

logger = logging.getLogger("patent_service")

_ABSTRACT_MARKER = re.compile(
    r"(?:^|\n)\s*(?:\(?57\)?\s*)?(?:abstract|zusammenfassung|abr[eé]g[eé]|"
    r"resumen|resumo|реферат|摘要|要約|초록|ملخص)\s*[:：]?\s*",
    re.IGNORECASE,
)
_PATENT_HEADER = re.compile(
    r"^(?:PCT[/ -][A-Z]{2}[ /-]?\d|[A-Z]{2}[ /-]?\d)[A-Z0-9/ .-]{4,}$",
    re.IGNORECASE,
)
_PAGE_LABEL = re.compile(
    r"^(?:"
    r"-?\s*\d+\s*-?|"
    r"(?:第\s*)?\d+\s*(?:/\s*\d+)?\s*(?:页|頁)|"
    r"pages?\s*\d+(?:\s*of\s*\d+)?|"
    r"\d+\s*/\s*\d+\s*pages?"
    r")$",
    re.IGNORECASE,
)


class PdfPatentParser:
    """Conservative five-part parser for public patent PDF files."""

    def __init__(self, settings: Settings, ocr: OcrEngine) -> None:
        self._settings = settings
        self._ocr = ocr

    def parse(self, path: Path, *, filename: str) -> AnalysisDraft:
        started_at = time.monotonic()
        logger.info(
            "uploaded document analysis step document=%s file_type=pdf step=pdf_open action=start",
            filename,
        )
        draft = AnalysisDraft(
            filename=filename,
            file_type="pdf",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        try:
            document = fitz.open(path)
        except (fitz.FileDataError, RuntimeError, ValueError) as exc:
            raise PatentServiceError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                status_code=422,
                message="The PDF document could not be opened.",
                details={"filename": filename, "error": str(exc)},
            ) from exc

        try:
            if document.page_count > self._settings.analysis_max_pdf_pages:
                raise PatentServiceError(
                    code=ErrorCode.UPLOAD_TOO_LARGE,
                    status_code=422,
                    message="The PDF exceeds the configured page limit.",
                    details={
                        "filename": filename,
                        "page_count": document.page_count,
                        "max_pages": self._settings.analysis_max_pdf_pages,
                    },
                )
            self._parse_pages(document, draft)
        finally:
            document.close()
        result = draft.to_result()
        logger.info(
            "uploaded document analysis step document=%s file_type=pdf step=parse action=complete total_words=%s methods=%s elapsed_ms=%s",
            filename,
            result.total_words,
            ",".join(
                f"{name}:{part.method}"
                for name, part in draft.parts.items()
                if part.status != "missing"
            ) or "none",
            int((time.monotonic() - started_at) * 1000),
        )
        return draft

    def _parse_pages(self, document: fitz.Document, draft: AnalysisDraft) -> None:
        current_part: str | None = None
        detected_sections = False
        abstract_found = False
        seen_images: set[str] = set()
        page_texts = [_page_text(page) for page in document]
        sample_text = "\n".join(page_texts[:20])
        document_language = detect_ocr_language(
            sample_text, default=self._settings.ocr_default_language
        )
        unreliable_pages = [
            index + 1
            for index, text in enumerate(page_texts)
            if not _text_layer_is_reliable(text)
        ]
        logger.info(
            "uploaded PDF step document=%s step=text_layer_check pages=%s reliable_pages=%s ocr_fallback_pages=%s language=%s",
            draft.filename,
            len(page_texts),
            len(page_texts) - len(unreliable_pages),
            len(unreliable_pages),
            document_language,
        )
        page_ocr_results = self._ocr_unreliable_pages(
            document,
            page_texts,
            language=document_language,
            document_name=draft.filename,
        )

        for page_number, page in enumerate(document):
            text = page_texts[page_number]
            original_text = text
            method = "pdf_text"
            confidence = "high"
            full_page_ocr = False
            if not _text_layer_is_reliable(text):
                result = page_ocr_results[page_number]
                full_page_ocr = bool(result.text.strip())
                if full_page_ocr:
                    text = result.text
                    method = result.provider or "ocr"
                    confidence = _ocr_confidence(result)
                elif original_text.strip():
                    text = original_text
                    method = "pdf_text_low_quality"
                    confidence = "low"
                for warning in result.warnings:
                    draft.warnings.append(
                        PatentAnalysisWarning(
                            code="ocr_failed",
                            message=f"Page {page_number + 1}: {warning}",
                            filename=draft.filename,
                        )
                    )

            if not text.strip() or contains_search_report(text):
                continue

            if page_number == 0:
                cover_abstract, cover_drawing = _extract_cover_parts(text)
                if cover_abstract:
                    draft.add_text(
                        "abstract",
                        cover_abstract,
                        method=method,
                        confidence=confidence,
                    )
                    abstract_found = True
                    detected_sections = True
                    current_part = "abstract"
                    if cover_drawing:
                        draft.add_text(
                            "abstract_drawing",
                            cover_drawing,
                            method=method,
                            confidence="medium",
                            is_drawing=True,
                        )
                    elif method == "pdf_text":
                        self._ocr_abstract_images(
                            document,
                            page,
                            draft,
                            language=document_language,
                            seen_images=seen_images,
                        )
                    continue

            lines = [line.strip() for line in text.splitlines() if line.strip()]
            segments: list[tuple[str | None, str]] = []
            buffer: list[str] = []
            segment_part = current_part
            for line in lines:
                heading = detect_heading(line)
                if heading:
                    if buffer:
                        segments.append((segment_part, " ".join(buffer)))
                        buffer = []
                    segment_part = heading
                    current_part = heading
                    detected_sections = True
                elif not _is_repeated_header(line):
                    buffer.append(line)
            if buffer:
                segments.append((segment_part, " ".join(buffer)))

            for part, segment_text in segments:
                if not segment_text.strip():
                    continue
                target = part if detected_sections and part else "unclassified"
                status = "found" if target != "unclassified" else "unclassified"
                draft.add_text(
                    target,
                    segment_text,
                    method=method,
                    confidence=confidence if target != "unclassified" else "low",
                    status=status,
                    is_drawing=target in {"abstract_drawing", "description_drawings"},
                )
                if target == "abstract":
                    abstract_found = True

            if not full_page_ocr:
                self._ocr_embedded_drawings(
                    document,
                    page,
                    draft,
                    page_text=text,
                    page_parts={part for part, _ in segments if part},
                    current_part=current_part,
                    language=document_language,
                    seen_images=seen_images,
                )

        if draft.parts["unclassified"].status == "unclassified":
            draft.warnings.append(
                PatentAnalysisWarning(
                    code="section_detection_incomplete",
                    message=(
                        "Some PDF text could not be assigned to a patent section "
                        "with sufficient confidence."
                    ),
                    filename=draft.filename,
                )
            )

    def _ocr_unreliable_pages(
        self,
        document: fitz.Document,
        page_texts: list[str],
        *,
        language: str,
        document_name: str,
    ) -> dict[int, OcrResult]:
        page_numbers = [
            index
            for index, text in enumerate(page_texts)
            if not _text_layer_is_reliable(text)
        ]
        if not page_numbers:
            logger.info(
                "uploaded PDF step document=%s step=full_page_ocr action=skip reason=reliable_text_layer pages=%s",
                document_name,
                len(page_texts),
            )
            return {}
        results: dict[int, OcrResult] = {}
        batch_size = max(1, self._settings.ocr_batch_size)
        for offset in range(0, len(page_numbers), batch_size):
            batch_numbers = page_numbers[offset : offset + batch_size]
            batch_started_at = time.monotonic()
            logger.info(
                "uploaded PDF step document=%s step=full_page_ocr action=batch_start batch=%s pages=%s page_numbers=%s language=%s",
                document_name,
                offset // batch_size + 1,
                len(batch_numbers),
                ",".join(str(number + 1) for number in batch_numbers),
                language,
            )
            payloads: list[bytes] = []
            valid_numbers: list[int] = []
            for page_number in batch_numbers:
                rendered = self._render_page(document[page_number])
                if isinstance(rendered, OcrResult):
                    results[page_number] = rendered
                else:
                    payloads.append(rendered)
                    valid_numbers.append(page_number)
            batch_results = recognize_many(
                self._ocr,
                payloads,
                sparse=False,
                language=language,
            )
            results.update(zip(valid_numbers, batch_results))
            logger.info(
                "uploaded PDF step document=%s step=full_page_ocr action=batch_complete batch=%s rendered=%s recognized=%s elapsed_ms=%s",
                document_name,
                offset // batch_size + 1,
                len(valid_numbers),
                sum(bool(result.text.strip()) for result in batch_results),
                int((time.monotonic() - batch_started_at) * 1000),
            )
        return results

    def _ocr_page(
        self,
        page: fitz.Page,
        *,
        sparse: bool,
        language: str | None = None,
    ) -> OcrResult:
        rendered = self._render_page(page)
        if isinstance(rendered, OcrResult):
            return rendered
        return self._ocr.recognize(
            rendered, sparse=sparse, language=language
        )

    def _render_page(self, page: fitz.Page) -> bytes | OcrResult:
        scale = 300 / 72
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        if pixmap.width * pixmap.height > self._settings.analysis_max_image_pixels:
            return OcrResult(
                text="",
                confidence=None,
                language="",
                warnings=["Rendered page exceeds the configured OCR pixel limit."],
            )
        return pixmap.tobytes("png")

    def _ocr_abstract_images(
        self,
        document: fitz.Document,
        page: fitz.Page,
        draft: AnalysisDraft,
        *,
        language: str,
        seen_images: set[str],
    ) -> None:
        page_area = max(page.rect.width * page.rect.height, 1)
        seen: set[int] = set()
        for image in page.get_images(full=True):
            xref = image[0]
            if xref in seen:
                continue
            seen.add(xref)
            rectangles = page.get_image_rects(xref)
            largest_area = max(
                (rect.width * rect.height for rect in rectangles), default=0
            )
            if largest_area < page_area * 0.03 or largest_area > page_area * 0.85:
                continue
            extracted = document.extract_image(xref)
            digest = hashlib.sha256(extracted["image"]).hexdigest()
            if digest in seen_images:
                continue
            seen_images.add(digest)
            width = int(extracted.get("width") or 0)
            height = int(extracted.get("height") or 0)
            if not width or not height or width * height > self._settings.analysis_max_image_pixels:
                continue
            result = self._ocr.recognize(
                extracted["image"], sparse=True, language=language
            )
            if result.text.strip():
                draft.add_text(
                    "abstract_drawing",
                    result.text,
                    method=result.provider or "ocr",
                    confidence=_ocr_confidence(result),
                    is_drawing=True,
                )
            for warning in result.warnings:
                draft.mark_error("abstract_drawing", f"Cover image {xref}: {warning}")

    def _ocr_embedded_drawings(
        self,
        document: fitz.Document,
        page: fitz.Page,
        draft: AnalysisDraft,
        *,
        page_text: str,
        page_parts: set[str],
        current_part: str | None,
        language: str,
        seen_images: set[str],
    ) -> None:
        candidate_part = (
            "abstract_drawing"
            if page_parts.intersection({"abstract", "abstract_drawing"})
            or current_part in {"abstract", "abstract_drawing"}
            else "description_drawings"
            if page_parts.intersection({"description", "description_drawings"})
            or current_part in {"description", "description_drawings"}
            else None
        )
        if candidate_part is None:
            return
        page_area = max(page.rect.width * page.rect.height, 1)
        seen_xrefs: set[int] = set()
        for image in page.get_images(full=True):
            xref = image[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            rectangles = page.get_image_rects(xref)
            largest_area = max(
                (rect.width * rect.height for rect in rectangles), default=0
            )
            if largest_area < page_area * 0.03 or largest_area > page_area * 0.85:
                continue
            extracted = document.extract_image(xref)
            payload = extracted.get("image", b"")
            digest = hashlib.sha256(payload).hexdigest()
            if not payload or digest in seen_images:
                continue
            seen_images.add(digest)
            width = int(extracted.get("width") or 0)
            height = int(extracted.get("height") or 0)
            if not width or not height or width * height > self._settings.analysis_max_image_pixels:
                continue
            result = self._ocr.recognize(
                payload, sparse=True, language=language
            )
            image_text = " ".join(result.text.split())
            if image_text and not _ocr_text_duplicates_page(image_text, page_text):
                if candidate_part == "abstract_drawing" or _looks_like_drawing_ocr(
                    image_text
                ):
                    draft.add_text(
                        candidate_part,
                        image_text,
                        method=result.provider or "ocr",
                        confidence=_ocr_confidence(result),
                        is_drawing=True,
                    )
            for warning in result.warnings:
                draft.mark_error(candidate_part, f"Embedded image {xref}: {warning}")


def _page_text(page: fitz.Page) -> str:
    lines: list[str] = []
    top = page.rect.height * 0.04
    bottom = page.rect.height * 0.96
    for block in page.get_text("blocks"):
        y0, y1, text = float(block[1]), float(block[3]), str(block[4])
        if y1 <= top or y0 >= bottom:
            continue
        lines.extend(line.strip() for line in text.splitlines() if line.strip())
    return "\n".join(lines)


def _text_layer_is_reliable(text: str) -> bool:
    normalized = "".join(character for character in text if not character.isspace())
    if count_units(text) < 2 or not normalized:
        return False
    suspicious = sum(
        text.count(marker)
        for marker in ("\ufffd", "Ã", "Â", "â€", "□", "�")
    )
    if suspicious / len(normalized) > 0.01:
        return False
    controls = sum(
        1
        for character in normalized
        if ord(character) < 32 and character not in "\t\r\n"
    )
    if controls:
        return False
    meaningful = sum(character.isalnum() for character in normalized)
    return meaningful / len(normalized) >= 0.25


def _extract_cover_parts(text: str) -> tuple[str, str]:
    matches = list(_ABSTRACT_MARKER.finditer(text))
    if not matches:
        return "", ""
    match = matches[0]
    abstract_end = matches[1].start() if len(matches) > 1 else len(text)
    remainder = text[match.end() : abstract_end]
    collected: list[str] = []
    for line in remainder.splitlines():
        if detect_heading(line) in {
            "abstract_drawing",
            "description",
            "claims",
            "description_drawings",
        }:
            break
        if line.strip() and not _is_repeated_header(line):
            collected.append(line.strip())
    drawing_region = text[: match.start()]
    title_markers = list(re.finditer(r"(?m)^\s*\(?54\)?\b.*$", drawing_region))
    if title_markers:
        drawing_region = drawing_region[title_markers[-1].end() :]
    drawing_lines = [
        line.strip()
        for line in drawing_region.splitlines()
        if _looks_like_figure_text(line)
    ]
    return " ".join(collected), " ".join(drawing_lines)


def _looks_like_figure_text(line: str) -> bool:
    normalized = " ".join(line.split())
    if not normalized or _is_repeated_header(normalized):
        return False
    units = re.findall(r"[A-Za-z]+|\d+(?:[./-]\d+)*", normalized)
    return bool(re.search(r"\d", normalized)) and (
        len(units) <= 4 or bool(re.search(r"\bfig(?:ure)?\.?\s*\d+", normalized, re.IGNORECASE))
    )


def _looks_like_drawing_ocr(text: str) -> bool:
    tokens = tokenize_counting_units(text)
    if not tokens or not re.search(r"\d", text):
        return False
    if re.search(r"\bfig(?:ure)?\.?\s*\d+", text, re.IGNORECASE):
        return True
    numeric_or_short = sum(
        token[0].isdigit() or len(token) <= 4 for token in tokens if token
    )
    return len(tokens) <= 30 and numeric_or_short / len(tokens) >= 0.5


def _ocr_text_duplicates_page(image_text: str, page_text: str) -> bool:
    image_tokens = {token.lower() for token in tokenize_counting_units(image_text)}
    page_tokens = {token.lower() for token in tokenize_counting_units(page_text)}
    if not image_tokens or not page_tokens:
        return False
    return len(image_tokens & page_tokens) / len(image_tokens) >= 0.8


def _is_repeated_header(line: str) -> bool:
    normalized = " ".join(line.split())
    return bool(_PATENT_HEADER.match(normalized)) or bool(_PAGE_LABEL.match(normalized))


def _ocr_confidence(result: OcrResult) -> str:
    if result.confidence is None:
        return "low"
    return "high" if result.confidence >= 80 else "medium" if result.confidence >= 50 else "low"
