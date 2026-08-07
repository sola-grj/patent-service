import hashlib
import zipfile
from pathlib import Path, PurePosixPath

from defusedxml import ElementTree as ET

from app.analysis.common import AnalysisDraft
from app.analysis.languages import detect_ocr_language
from app.analysis.ocr import OcrEngine, OcrResult
from app.analysis.sections import detect_heading
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class WordPatentParser:
    def __init__(self, settings: Settings, ocr: OcrEngine) -> None:
        self._settings = settings
        self._ocr = ocr

    def parse(
        self,
        path: Path,
        *,
        filename: str,
        original_type: str = "docx",
        original_sha256: str | None = None,
    ) -> AnalysisDraft:
        draft = AnalysisDraft(
            filename=filename,
            file_type=original_type,
            sha256=original_sha256 or hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        try:
            with zipfile.ZipFile(path) as archive:
                names = _validate_docx_archive(archive, self._settings)
                relationships = _relationships(archive, names)
                root = ET.fromstring(archive.read(names["word/document.xml"]))
                body = root.find(f"{{{_W}}}body")
                if body is None:
                    raise ValueError("DOCX does not contain a document body")
                self._parse_body(archive, names, relationships, body, draft)
        except PatentServiceError:
            raise
        except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
            raise PatentServiceError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                status_code=422,
                message="The Word document could not be parsed.",
                details={"filename": filename, "error": str(exc)},
            ) from exc
        if all(part.status == "missing" for part in draft.parts.values()):
            draft.parts["unclassified"].status = "unclassified"
        return draft

    def _parse_body(
        self, archive, names, relationships, body, draft: AnalysisDraft
    ) -> None:
        current_part: str | None = None
        detected_sections = False
        seen_images: set[str] = set()
        document_language = detect_ocr_language(
            _visible_text(body), default=self._settings.ocr_default_language
        )
        for block in list(body):
            text = _visible_text(block)
            heading = detect_heading(text)
            if heading:
                current_part = heading
                detected_sections = True
                draft.add_text(
                    heading,
                    text,
                    method="docx_xml",
                    confidence="high",
                    is_drawing=heading in {"abstract_drawing", "description_drawings"},
                )
            elif text:
                target = current_part if detected_sections and current_part else "unclassified"
                status = "found" if target != "unclassified" else "unclassified"
                draft.add_text(
                    target,
                    text,
                    method="docx_xml",
                    confidence="high" if target != "unclassified" else "low",
                    status=status,
                    is_drawing=target in {"abstract_drawing", "description_drawings"},
                )
            for relationship_id in _image_relationship_ids(block):
                target_name = relationships.get(relationship_id)
                if not target_name:
                    continue
                actual = names.get(target_name.lower())
                if not actual:
                    continue
                payload = archive.read(actual)
                digest = hashlib.sha256(payload).hexdigest()
                if digest in seen_images:
                    continue
                seen_images.add(digest)
                image_part = (
                    "abstract_drawing"
                    if current_part in {"abstract", "abstract_drawing"}
                    else "description_drawings"
                    if current_part in {"description", "description_drawings"}
                    else "unclassified"
                )
                result = self._ocr.recognize(
                    payload, sparse=True, language=document_language
                )
                if result.text.strip():
                    draft.add_text(
                        image_part,
                        result.text,
                        method=result.provider or "ocr",
                        confidence=_ocr_confidence(result),
                        status="found" if image_part != "unclassified" else "unclassified",
                        is_drawing=True,
                    )
                for warning in result.warnings:
                    draft.mark_error(image_part, f"{actual}: {warning}")


def _validate_docx_archive(archive, settings: Settings) -> dict[str, str]:
    infos = archive.infolist()
    if len(infos) > settings.analysis_max_docx_entries:
        raise ValueError("DOCX contains too many entries")
    total = 0
    names: dict[str, str] = {}
    for info in infos:
        candidate = PurePosixPath(info.filename.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("DOCX contains an unsafe member path")
        total += info.file_size
        if total > settings.analysis_max_docx_uncompressed_bytes:
            raise ValueError("DOCX expands beyond the configured limit")
        if (
            info.file_size > 10 * 1024 * 1024
            and info.file_size > max(info.compress_size, 1) * 1000
        ):
            raise ValueError("DOCX contains a suspicious compression ratio")
        names[candidate.as_posix().lower()] = info.filename
    if "word/document.xml" not in names:
        raise ValueError("DOCX does not contain word/document.xml")
    return names


def _relationships(archive, names: dict[str, str]) -> dict[str, str]:
    rels_name = names.get("word/_rels/document.xml.rels")
    if not rels_name:
        return {}
    root = ET.fromstring(archive.read(rels_name))
    output: dict[str, str] = {}
    for node in root:
        if node.tag.rsplit("}", 1)[-1] != "Relationship":
            continue
        relationship_id = node.get("Id")
        target = node.get("Target")
        mode = node.get("TargetMode", "Internal")
        if not relationship_id or not target or mode.lower() == "external":
            continue
        candidate = PurePosixPath("word") / PurePosixPath(target.replace("\\", "/"))
        normalized_parts: list[str] = []
        for part in candidate.parts:
            if part == "..":
                if normalized_parts:
                    normalized_parts.pop()
            elif part not in {".", ""}:
                normalized_parts.append(part)
        output[relationship_id] = "/".join(normalized_parts)
    return output


def _visible_text(block) -> str:
    texts: list[str] = []

    def walk(node, deleted: bool = False) -> None:
        local = node.tag.rsplit("}", 1)[-1]
        deleted = deleted or local == "del"
        if local == "t" and not deleted and node.text:
            texts.append(node.text)
        elif local in {"tab", "br", "cr"} and not deleted:
            texts.append(" ")
        for child in node:
            walk(child, deleted)

    walk(block)
    return " ".join("".join(texts).split())


def _image_relationship_ids(block) -> list[str]:
    values: list[str] = []
    for node in block.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local not in {"blip", "imagedata"}:
            continue
        value = node.get(f"{{{_R}}}embed") or node.get(f"{{{_R}}}id")
        if value and value not in values:
            values.append(value)
    return values


def _ocr_confidence(result: OcrResult) -> str:
    if result.confidence is None:
        return "low"
    return "high" if result.confidence >= 80 else "medium" if result.confidence >= 50 else "low"
