import hashlib
import logging
import re
import time
import zipfile
from pathlib import Path, PurePosixPath

from defusedxml import ElementTree as ET

from app.analysis.common import AnalysisDraft
from app.analysis.languages import detect_ocr_language
from app.analysis.ocr import OcrEngine, OcrResult, recognize_many
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentAnalysisWarning

logger = logging.getLogger("patent_service")

_PAGE_ENTRY = re.compile(rb"<DP\b([^>]*)>", re.IGNORECASE)
_PAGE_IMAGE = re.compile(rb"\bIMA=([^\s>]+)", re.IGNORECASE)
_PAGE_FLAG = re.compile(rb"\b(AB|DE|CL|DR)=1\b", re.IGNORECASE)


class StructuredPatentParser:
    def __init__(self, settings: Settings, ocr: OcrEngine) -> None:
        self._settings = settings
        self._ocr = ocr

    def parse(self, path: Path, *, source: str, filename: str | None = None):
        started_at = time.monotonic()
        payload_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        draft = AnalysisDraft(
            filename=filename or path.name,
            file_type=f"{source}_zip",
            sha256=payload_hash,
        )
        try:
            with zipfile.ZipFile(path) as archive:
                names = _validate_archive(archive, self._settings)
                xml_name = _select_patent_xml(archive, names, source)
                logger.info(
                    "patent ZIP step document=%s source=%s step=archive_open action=complete entries=%s xml=%s",
                    draft.filename,
                    source,
                    len(names),
                    xml_name,
                )
                root = ET.fromstring(archive.read(xml_name))
                if source == "wipo":
                    self._parse_wipo(archive, names, root, draft)
                else:
                    self._parse_epo(archive, names, root, draft)
        except PatentServiceError:
            raise
        except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
            raise PatentServiceError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                status_code=422,
                message="The official patent package could not be parsed.",
                source=source,
                details={"filename": filename or path.name, "error": str(exc)},
            ) from exc
        logger.info(
            "patent ZIP step document=%s source=%s step=zip_parse action=complete methods=%s elapsed_ms=%s",
            draft.filename,
            source,
            ",".join(
                f"{name}:{part.method}"
                for name, part in draft.parts.items()
                if part.status != "missing"
            ) or "none",
            int((time.monotonic() - started_at) * 1000),
        )
        return draft

    def _parse_wipo(self, archive, names, root, draft: AnalysisDraft) -> None:
        page_flags = _wipo_page_flags(archive, names)
        abstract_nodes = _all_local(root, "abstract")
        publication_language_hint = (root.get("lang") or "").lower()
        selected_abstract = _select_language_node(
            abstract_nodes, preferred=publication_language_hint
        )
        selected_language_hint = (
            (selected_abstract.get("lang") or "").lower()
            if selected_abstract is not None
            else ""
        )
        selected_language = detect_ocr_language(
            _node_text(selected_abstract) if selected_abstract is not None else "",
            hint=selected_language_hint or None,
            default=self._settings.ocr_default_language,
        )
        if selected_abstract is not None:
            logger.info(
                "patent ZIP step document=%s source=wipo section=abstract decision=xml method=wipo_xml language=%s",
                draft.filename,
                selected_language,
            )
            draft.add_text(
                "abstract",
                _node_text(selected_abstract),
                method="wipo_xml",
                confidence="high",
            )
        elif page_flags.get("abstract"):
            logger.info(
                "patent ZIP step document=%s source=wipo section=abstract decision=ocr reason=xml_text_missing pages=%s language=%s",
                draft.filename,
                len(page_flags["abstract"]),
                selected_language,
            )
            self._ocr_references(
                archive,
                names,
                page_flags["abstract"],
                draft,
                "abstract",
                sparse=False,
                language=selected_language,
            )
        abstract_images = _references(
            _select_language_nodes(
                _all_local(root, "abstract-figure"), selected_language_hint
            )
        )
        self._ocr_references(
            archive,
            names,
            abstract_images,
            draft,
            "abstract_drawing",
            sparse=True,
            language=selected_language,
        )

        for element_name, part in (
            ("description", "description"),
            ("claims", "claims"),
        ):
            nodes = _all_local(root, element_name)
            text = " ".join(_node_text(node) for node in nodes).strip()
            if text:
                logger.info(
                    "patent ZIP step document=%s source=wipo section=%s decision=xml method=wipo_xml language=%s",
                    draft.filename,
                    part,
                    selected_language,
                )
                draft.add_text(
                    part, text, method="wipo_xml", confidence="high"
                )
            else:
                refs = _references(nodes) or page_flags.get(part, [])
                logger.info(
                    "patent ZIP step document=%s source=wipo section=%s decision=ocr reason=xml_text_missing pages=%s language=%s",
                    draft.filename,
                    part,
                    len(refs),
                    selected_language,
                )
                self._ocr_references(
                    archive,
                    names,
                    refs,
                    draft,
                    part,
                    sparse=False,
                    language=selected_language,
                )

        drawing_nodes = _all_local(root, "drawings")
        drawing_refs = _references(drawing_nodes) or page_flags.get(
            "description_drawings", []
        )
        self._ocr_references(
            archive,
            names,
            drawing_refs,
            draft,
            "description_drawings",
            sparse=True,
            language=selected_language,
        )

    def _parse_epo(self, archive, names, root, draft: AnalysisDraft) -> None:
        publication_language_hint = (root.get("lang") or "").lower()
        document_language = detect_ocr_language(
            "",
            hint=publication_language_hint or None,
            default=self._settings.ocr_default_language,
        )
        for element_name, part in (
            ("abstract", "abstract"),
            ("description", "description"),
            ("claims", "claims"),
        ):
            nodes = _all_local(root, element_name)
            selected = _select_language_node(
                nodes, preferred=publication_language_hint
            )
            text = _node_text(selected) if selected is not None else ""
            language = detect_ocr_language(
                text,
                hint=(selected.get("lang") if selected is not None else None),
                default=document_language,
            )
            if part == "abstract" or document_language == self._settings.ocr_default_language:
                document_language = language
            if text:
                logger.info(
                    "patent ZIP step document=%s source=epo section=%s decision=xml method=epo_xml language=%s",
                    draft.filename,
                    part,
                    language,
                )
                draft.add_text(part, text, method="epo_xml", confidence="high")
            elif nodes:
                references = _references(nodes)
                logger.info(
                    "patent ZIP step document=%s source=epo section=%s decision=ocr reason=xml_text_missing pages=%s language=%s",
                    draft.filename,
                    part,
                    len(references),
                    language,
                )
                self._ocr_references(
                    archive,
                    names,
                    references,
                    draft,
                    part,
                    sparse=False,
                    language=language,
                )

        abstract_nodes = _all_local(root, "abstract")
        selected_abstract = _select_language_node(
            abstract_nodes, preferred=publication_language_hint
        )
        self._ocr_references(
            archive,
            names,
            _references([selected_abstract] if selected_abstract is not None else []),
            draft,
            "abstract_drawing",
            sparse=True,
            language=document_language,
        )
        drawing_nodes = _all_local(root, "drawings")
        self._ocr_references(
            archive,
            names,
            _references(drawing_nodes),
            draft,
            "description_drawings",
            sparse=True,
            language=document_language,
        )

    def _ocr_references(
        self,
        archive: zipfile.ZipFile,
        names: dict[str, str],
        references: list[str],
        draft: AnalysisDraft,
        part: str,
        *,
        sparse: bool,
        language: str | None = None,
    ) -> None:
        source = draft.file_type.removesuffix("_zip")
        seen: set[str] = set()
        actual_names: list[str] = []
        payloads: list[bytes] = []
        for reference in references:
            safe = _safe_member_name(reference)
            actual = _resolve_member(names, safe)
            if not actual or actual in seen:
                continue
            seen.add(actual)
            actual_names.append(actual)
            payloads.append(archive.read(actual))
        if not payloads:
            logger.info(
                "patent OCR step document=%s source=%s section=%s action=skip reason=no_image_references",
                draft.filename,
                source,
                part,
            )
            return
        results: list[OcrResult] = []
        batch_size = max(1, self._settings.ocr_batch_size)
        started_at = time.monotonic()
        logger.info(
            "patent OCR step document=%s source=%s section=%s action=start pages=%s batch_size=%s language=%s sparse=%s",
            draft.filename,
            source,
            part,
            len(payloads),
            batch_size,
            language or "default",
            sparse,
        )
        for offset in range(0, len(payloads), batch_size):
            current_batch = payloads[offset : offset + batch_size]
            logger.info(
                "patent OCR batch document=%s source=%s section=%s action=start batch=%s pages=%s",
                draft.filename,
                source,
                part,
                offset // batch_size + 1,
                len(current_batch),
            )
            results.extend(
                recognize_many(
                    self._ocr,
                    current_batch,
                    sparse=sparse,
                    language=language,
                )
            )
        for actual, result in zip(actual_names, results):
            cleaned_text = _clean_ocr_text(result.text)
            if cleaned_text:
                draft.add_text(
                    part,
                    cleaned_text,
                    method=result.provider or "ocr",
                    confidence=_ocr_confidence(result),
                )
            for warning in result.warnings:
                draft.mark_error(part, f"{actual}: {warning}")
        if actual_names and draft.parts[part].status == "missing":
            draft.parts[part].status = "found"
            draft.parts[part].method = "ocr"
            draft.parts[part].confidence = "low"
        providers = sorted({result.provider or "ocr" for result in results})
        warning_count = sum(len(result.warnings) for result in results)
        logger.info(
            "patent OCR step document=%s source=%s section=%s action=complete pages=%s providers=%s warnings=%s extracted=%s elapsed_ms=%s",
            draft.filename,
            source,
            part,
            len(results),
            ",".join(providers) or "none",
            warning_count,
            sum(bool(result.text.strip()) for result in results),
            int((time.monotonic() - started_at) * 1000),
        )


def _validate_archive(
    archive: zipfile.ZipFile, settings: Settings
) -> dict[str, str]:
    infos = archive.infolist()
    if len(infos) > settings.analysis_max_docx_entries:
        raise ValueError("archive contains too many entries")
    total = 0
    names: dict[str, str] = {}
    for info in infos:
        safe = _safe_member_name(info.filename, allow_directories=True)
        total += info.file_size
        if total > settings.analysis_max_docx_uncompressed_bytes:
            raise ValueError("archive expands beyond the configured limit")
        if (
            info.file_size > 10 * 1024 * 1024
            and info.file_size > max(info.compress_size, 1) * 1000
        ):
            raise ValueError("archive contains a suspicious compression ratio")
        names[safe.lower()] = info.filename
    return names


def _safe_member_name(value: str, *, allow_directories: bool = True) -> str:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("archive contains an unsafe member path")
    if not allow_directories and len(candidate.parts) != 1:
        raise ValueError("archive page reference contains an unsafe path")
    return candidate.as_posix()


def _select_patent_xml(
    archive: zipfile.ZipFile, names: dict[str, str], source: str
) -> str:
    preferred = (
        "wo-published-application.xml" if source == "wipo" else None
    )
    if preferred and preferred in names:
        return names[preferred]
    for actual in names.values():
        if not actual.lower().endswith(".xml"):
            continue
        try:
            root = ET.fromstring(archive.read(actual))
        except ET.ParseError:
            continue
        root_name = _local_name(root.tag)
        if source == "wipo" and root_name in {
            "wo-published-application",
            "wo-patent-document",
        }:
            return actual
        if source == "epo" and root_name == "ep-patent-document":
            return actual
    raise ValueError("archive does not contain a patent publication XML file")


def _all_local(root, name: str) -> list:
    return [node for node in root.iter() if _local_name(node.tag) == name]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _select_language_node(nodes: list, *, preferred: str = ""):
    if preferred:
        for node in nodes:
            if (node.get("lang") or "").lower() == preferred:
                return node
    for preferred in ("en", "de", "fr"):
        for node in nodes:
            if (node.get("lang") or "").lower() == preferred:
                return node
    return nodes[0] if nodes else None


def _select_language_nodes(nodes: list, preferred: str) -> list:
    if not nodes:
        return []
    if preferred:
        matching = [
            node for node in nodes if (node.get("lang") or "").lower() == preferred
        ]
        if matching:
            return matching[:1]
    unqualified = [node for node in nodes if not node.get("lang")]
    return (unqualified or nodes)[:1]


def _node_text(node) -> str:
    return " ".join("".join(node.itertext()).split())


def _references(nodes: list) -> list[str]:
    references: list[str] = []
    for node in nodes:
        for child in node.iter():
            if _local_name(child.tag) not in {"img", "doc-page"}:
                continue
            value = child.get("file")
            if value and value not in references:
                references.append(value)
    return references


def _wipo_page_flags(
    archive: zipfile.ZipFile, names: dict[str, str]
) -> dict[str, list[str]]:
    output = {
        "abstract": [],
        "description": [],
        "claims": [],
        "description_drawings": [],
    }
    pag_name = _resolve_member(names, "pag.lst")
    if not pag_name:
        return output
    payload = archive.read(pag_name)
    for entry in _PAGE_ENTRY.findall(payload):
        image = _PAGE_IMAGE.search(entry)
        flag = _PAGE_FLAG.search(entry)
        if not image or not flag:
            continue
        name = image.group(1).decode("ascii", errors="ignore")
        part = {
            b"AB": "abstract",
            b"DE": "description",
            b"CL": "claims",
            b"DR": "description_drawings",
        }.get(flag.group(1).upper())
        if part:
            output[part].append(name)
    return output


def _resolve_member(names: dict[str, str], reference: str) -> str | None:
    normalized = reference.lower()
    direct = names.get(normalized)
    if direct:
        return direct
    basename = PurePosixPath(normalized).name
    matches = [actual for safe, actual in names.items() if PurePosixPath(safe).name == basename]
    return matches[0] if len(matches) == 1 else None


def _ocr_confidence(result: OcrResult) -> str:
    if result.confidence is None:
        return "low"
    if result.confidence >= 80:
        return "high"
    if result.confidence >= 50:
        return "medium"
    return "low"


def _clean_ocr_text(text: str) -> str:
    cleaned = re.sub(
        r"\b(?:WO\s*\d{4}\s*/?\s*\d{4,6}|EP\s*\d{4,12}\s*[A-Z]\d{1,2}|"
        r"PCT\s*/\s*[A-Z]{2}\s*\d{4}\s*/\s*\d{4,6})\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?m)^\s*\d+\s*/\s*\d+\s*$", " ", cleaned)
    cleaned = re.sub(r"(?m)^\s*-\s*\d+\s*-\s*$", " ", cleaned)
    cleaned = re.sub(r"(?m)^\s*(?:19|20)\d{2}\s+\d{2}\s+\d{2}\s*$", " ", cleaned)
    cleaned = re.sub(r"(?m)^\s*[A-Za-z]{1,3}/[A-Za-z]{1,3}\s*$", " ", cleaned)
    return " ".join(cleaned.split())
