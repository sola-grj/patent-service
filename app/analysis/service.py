import asyncio
import logging
import tempfile
import time
from pathlib import Path

from app.analysis.common import AnalysisDraft
from app.analysis.counting import text_five_grams
from app.analysis.ocr import AutoOcrEngine, OcrEngine
from app.analysis.pdf import PdfPatentParser
from app.analysis.structured import StructuredPatentParser
from app.analysis.uploads import StoredUpload, convert_doc_to_docx
from app.analysis.word import WordPatentParser
from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisAggregate,
    PatentAnalysisResponse,
    PatentAnalysisWarning,
    PatentFileAnalysis,
    PatentLookupRequest,
    PatentReference,
    PatentSource,
)
from app.services.patent_lookup import PatentLookupService
from app.utils.patent_numbers import normalize_patent_number

logger = logging.getLogger("patent_service")


class PatentAnalysisService:
    def __init__(
        self,
        *,
        settings: Settings,
        lookup_service: PatentLookupService,
        epo_publication_server_client: EpoPublicationServerClient,
        epo_ops_client: EpoOpsClient | None = None,
        ocr: OcrEngine | None = None,
    ) -> None:
        self._settings = settings
        self._lookup_service = lookup_service
        self._epo_publication_server_client = epo_publication_server_client
        self._epo_ops_client = epo_ops_client
        engine = ocr or AutoOcrEngine(settings)
        self._pdf_parser = PdfPatentParser(settings, engine)
        self._word_parser = WordPatentParser(settings, engine)
        self._structured_parser = StructuredPatentParser(settings, engine)

    def analyze_uploads(self, uploads: list[StoredUpload]) -> PatentAnalysisResponse:
        drafts = [self._analyze_upload(upload) for upload in uploads]
        return _build_response(input_mode="upload", drafts=drafts)

    async def analyze_patent(self, patent_number: str) -> PatentAnalysisResponse:
        started_at = time.monotonic()
        reference = normalize_patent_number(patent_number)
        logger.info(
            "patent analysis routed patent_number=%s normalized_number=%s source=%s step=source_route",
            patent_number,
            reference.normalized_number,
            reference.source.value,
        )
        if reference.source is PatentSource.WIPO:
            logger.info(
                "patent analysis step patent_number=%s source=wipo step=official_lookup service=PATENTSCOPE mode=%s action=fetch_metadata_and_pamphlet_zip",
                reference.normalized_number,
                self._settings.wipo_lookup_mode,
            )
            lookup_started_at = time.monotonic()
            response = await self._lookup_service.lookup_patent(
                PatentLookupRequest(
                    patent_number=patent_number, include_original_file=True
                )
            )
            archive = response.raw_source_refs.get("original_archive", {})
            archive_path = Path(str(archive.get("storage_path") or ""))
            if not archive_path.is_file():
                raise PatentServiceError(
                    code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                    status_code=404,
                    message="WIPO did not provide a locally available publication archive.",
                    source="wipo",
                    details={"normalized_number": reference.normalized_number},
                )
            logger.info(
                "patent analysis step patent_number=%s source=wipo step=pamphlet_zip action=download_complete lookup_mode=%s filename=%s bytes=%s elapsed_ms=%s",
                reference.normalized_number,
                response.raw_source_refs.get("lookup_mode", "unknown"),
                archive.get("filename") or archive_path.name,
                archive_path.stat().st_size,
                int((time.monotonic() - lookup_started_at) * 1000),
            )
            logger.info(
                "patent analysis step patent_number=%s source=wipo step=zip_parse action=start parser=structured_xml_with_ocr_fallback",
                reference.normalized_number,
            )
            draft = await asyncio.to_thread(
                self._structured_parser.parse,
                archive_path,
                source="wipo",
                filename=archive.get("filename") or f"{reference.normalized_number}.zip",
            )
        else:
            draft = await self._analyze_ep_publication(reference)
        _ensure_official_core_sections(draft, source=reference.source.value)
        result = _build_response(
            input_mode="patent_number",
            drafts=[draft],
            patent_number=reference.normalized_number,
        )
        _log_analysis_result(
            result,
            source=reference.source.value,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
        )
        return result

    def _analyze_upload(self, upload: StoredUpload) -> AnalysisDraft:
        started_at = time.monotonic()
        parser_name = {
            "pdf": "pdf_text_with_ocr_fallback",
            "docx": "word_xml_with_embedded_image_ocr",
            "doc": "libreoffice_conversion_then_word_xml_with_embedded_image_ocr",
        }.get(upload.file_type, "unknown")
        logger.info(
            "uploaded document analysis step document=%s file_type=%s step=parser_route parser=%s action=start",
            upload.filename,
            upload.file_type,
            parser_name,
        )
        try:
            if upload.file_type == "pdf":
                draft = self._pdf_parser.parse(upload.path, filename=upload.filename)
            elif upload.file_type == "docx":
                draft = self._word_parser.parse(
                    upload.path, filename=upload.filename, original_type="docx"
                )
            else:
                logger.info(
                    "uploaded document analysis step document=%s file_type=doc step=libreoffice_conversion action=start",
                    upload.filename,
                )
                converted = convert_doc_to_docx(upload.path, self._settings)
                logger.info(
                    "uploaded document analysis step document=%s file_type=doc step=libreoffice_conversion action=complete",
                    upload.filename,
                )
                draft = self._word_parser.parse(
                    converted,
                    filename=upload.filename,
                    original_type="doc",
                    original_sha256=_sha256(upload.path),
                )
            result = draft.to_result()
            logger.info(
                "uploaded document analysis completed document=%s file_type=%s status=%s total_words=%s elapsed_ms=%s",
                upload.filename,
                upload.file_type,
                result.status,
                result.total_words,
                int((time.monotonic() - started_at) * 1000),
            )
            return draft
        except PatentServiceError as exc:
            logger.warning(
                "uploaded document analysis failed document=%s file_type=%s error_code=%s message=%s elapsed_ms=%s",
                upload.filename,
                upload.file_type,
                exc.code,
                exc.message,
                int((time.monotonic() - started_at) * 1000),
            )
            return _failed_draft(upload.filename, upload.file_type, exc)
        except Exception as exc:  # defensive boundary for independent file results
            logger.exception(
                "uploaded document analysis failed document=%s file_type=%s error=%s elapsed_ms=%s",
                upload.filename,
                upload.file_type,
                exc,
                int((time.monotonic() - started_at) * 1000),
            )
            error = PatentServiceError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                status_code=422,
                message="The uploaded document could not be analyzed.",
                details={"filename": upload.filename, "error": str(exc)},
            )
            return _failed_draft(upload.filename, upload.file_type, error)

    async def _analyze_ep_publication(self, reference) -> AnalysisDraft:
        attempted: list[str] = []
        last_error: PatentServiceError | None = None
        for kind_code in _ep_application_kinds(reference.kind_code):
            attempted.append(kind_code)
            candidate = _ep_reference_with_kind(reference, kind_code)
            logger.info(
                "patent analysis step patent_number=%s source=epo step=application_publication_resolution action=try kind_code=%s",
                reference.normalized_number,
                kind_code,
            )
            ops_draft = await self._analyze_epo_ops(candidate)
            archive_started_at = time.monotonic()
            archive_url = self._epo_publication_server_client.build_archive_download_url(
                country_code=candidate.country_code,
                doc_number=candidate.doc_number,
                kind_code=kind_code,
            )
            logger.info(
                "patent analysis step patent_number=%s source=epo step=publication_zip service=EPO_Publication_Server action=download_start kind_code=%s url=%s",
                candidate.normalized_number,
                kind_code,
                archive_url,
            )
            try:
                payload = await self._epo_publication_server_client.download_archive(
                    country_code=candidate.country_code,
                    doc_number=candidate.doc_number,
                    kind_code=kind_code,
                )
            except PatentServiceError as exc:
                if exc.code is ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE:
                    logger.info(
                        "patent analysis step patent_number=%s source=epo step=publication_zip service=EPO_Publication_Server action=not_found kind_code=%s",
                        candidate.normalized_number,
                        kind_code,
                    )
                    last_error = exc
                    continue
                raise
            logger.info(
                "patent analysis step patent_number=%s source=epo step=publication_zip service=EPO_Publication_Server action=download_complete kind_code=%s bytes=%s elapsed_ms=%s",
                candidate.normalized_number,
                kind_code,
                len(payload),
                int((time.monotonic() - archive_started_at) * 1000),
            )
            if len(payload) > self._settings.analysis_max_docx_uncompressed_bytes:
                raise PatentServiceError(
                    code=ErrorCode.UPLOAD_TOO_LARGE,
                    status_code=422,
                    message="The EPO archive exceeds the configured analysis limit.",
                    source="epo",
                    details={"size": len(payload)},
                )
            with tempfile.TemporaryDirectory(prefix="patent-epo-analysis-") as directory:
                path = Path(directory) / f"EP{candidate.doc_number}{kind_code}.zip"
                await asyncio.to_thread(path.write_bytes, payload)
                logger.info(
                    "patent analysis step patent_number=%s source=epo step=zip_parse action=start parser=structured_xml_with_ocr_fallback filename=%s",
                    candidate.normalized_number,
                    path.name,
                )
                archive_draft = await asyncio.to_thread(
                    self._structured_parser.parse,
                    path,
                    source="epo",
                    filename=path.name,
                )
                merged = _merge_epo_official_drafts(ops_draft, archive_draft)
                logger.info(
                    "patent analysis step patent_number=%s source=epo step=official_source_merge action=complete text_source_priority=OPS drawing_source=Publication_Server_ZIP",
                    candidate.normalized_number,
                )
                return merged
        raise PatentServiceError(
            code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
            status_code=404,
            message="No corresponding EPO A1/A2 application publication archive was found.",
            source="epo",
            details={
                "normalized_number": reference.normalized_number,
                "attempted_kind_codes": attempted,
                "last_error": last_error.message if last_error else "",
            },
        )

    async def _analyze_epo_ops(
        self, reference: PatentReference
    ) -> AnalysisDraft | None:
        if self._epo_ops_client is None:
            logger.info(
                "patent analysis step patent_number=%s source=epo step=ops action=skip reason=client_unavailable",
                reference.normalized_number,
            )
            return None
        if isinstance(self._epo_ops_client, EpoOpsClient) and not self._settings.epo_ops_configured:
            logger.info(
                "patent analysis step patent_number=%s source=epo step=ops action=skip reason=credentials_not_configured",
                reference.normalized_number,
            )
            return None
        ops_started_at = time.monotonic()
        logger.info(
            "patent analysis step patent_number=%s source=epo step=ops service=EPO_OPS action=fetch_start constituents=biblio,description,claims",
            reference.normalized_number,
        )
        draft = AnalysisDraft(
            filename=f"EP{reference.doc_number}{reference.kind_code or ''}.zip",
            file_type="epo_zip",
        )
        biblio, description, claims = await asyncio.gather(
            _optional_epo_xml(
                self._epo_ops_client.fetch_bibliographic_data,
                reference,
                constituent="biblio",
            ),
            _optional_epo_xml(
                self._epo_ops_client.fetch_description_data,
                reference,
                constituent="description",
            ),
            _optional_epo_xml(
                self._epo_ops_client.fetch_claims_data,
                reference,
                constituent="claims",
            ),
        )
        publication_language: str | None = None
        if biblio:
            try:
                basic_info, refs = self._epo_ops_client.parse_bibliographic_data(
                    biblio
                )
                publication_language = refs.get("publication_language")
                draft.add_text(
                    "abstract",
                    basic_info.abstract,
                    method="epo_ops",
                    confidence="high",
                )
            except PatentServiceError:
                pass
        if description:
            try:
                content, _ = self._epo_ops_client.parse_description_data(
                    description, preferred_language=publication_language
                )
                draft.add_text(
                    "description",
                    content.text,
                    method="epo_ops",
                    confidence="high",
                )
            except PatentServiceError:
                pass
        if claims:
            try:
                content, _ = self._epo_ops_client.parse_claims_data(
                    claims, preferred_language=publication_language
                )
                draft.add_text(
                    "claims",
                    " ".join(content.claim_texts),
                    method="epo_ops",
                    confidence="high",
                )
            except PatentServiceError:
                pass
        available = [
            name for name in ("abstract", "description", "claims")
            if draft.parts[name].text.strip()
        ]
        missing = [
            name for name in ("abstract", "description", "claims")
            if not draft.parts[name].text.strip()
        ]
        logger.info(
            "patent analysis step patent_number=%s source=epo step=ops service=EPO_OPS action=fetch_complete available=%s missing=%s elapsed_ms=%s",
            reference.normalized_number,
            ",".join(available) or "none",
            ",".join(missing) or "none",
            int((time.monotonic() - ops_started_at) * 1000),
        )
        return draft if available else None


async def _optional_epo_xml(
    fetcher,
    reference: PatentReference,
    *,
    constituent: str,
) -> str | None:
    try:
        return await fetcher(reference)
    except PatentServiceError as exc:
        logger.warning(
            "patent analysis step patent_number=%s source=epo step=ops service=EPO_OPS action=constituent_unavailable constituent=%s error_code=%s message=%s",
            reference.normalized_number,
            constituent,
            exc.code,
            exc.message,
        )
        return None


def _log_analysis_result(
    result: PatentAnalysisResponse, *, source: str, elapsed_ms: int
) -> None:
    logger.info(
        "patent analysis completed patent_number=%s source=%s status=%s abstract_words=%s abstract_drawing_words=%s description_words=%s description_drawings_words=%s claims_words=%s unclassified_words=%s total_words=%s elapsed_ms=%s",
        result.patent_number,
        source,
        result.status,
        result.aggregate.abstract_words,
        result.aggregate.abstract_drawing_words,
        result.aggregate.description_words,
        result.aggregate.description_drawings_words,
        result.aggregate.claims_words,
        result.aggregate.unclassified_words,
        result.aggregate.total_words,
        elapsed_ms,
    )


def _ep_reference_with_kind(
    reference: PatentReference, kind_code: str
) -> PatentReference:
    normalized = f"EP{reference.doc_number}{kind_code}"
    return reference.model_copy(
        update={
            "kind_code": kind_code,
            "normalized_number": normalized,
            "display_number": normalized,
            "lookup_number": f"EP{reference.doc_number}.{kind_code}",
        }
    )


def _merge_epo_official_drafts(
    ops_draft: AnalysisDraft | None, archive_draft: AnalysisDraft
) -> AnalysisDraft:
    if ops_draft is None:
        return archive_draft
    for part in ("abstract", "description", "claims"):
        if ops_draft.parts[part].text.strip():
            archive_draft.parts[part] = ops_draft.parts[part]
    return archive_draft


def _ep_application_kinds(kind_code: str | None) -> list[str]:
    if kind_code in {"A1", "A2"}:
        return [kind_code]
    if kind_code in {"A3", "B1", "B2", "B3"}:
        return ["A1", "A2"]
    return ["A1", "A2"]


def _failed_draft(filename: str, file_type: str, exc: PatentServiceError) -> AnalysisDraft:
    draft = AnalysisDraft(filename=filename, file_type=file_type)
    draft.parts["unclassified"].status = "error"
    draft.warnings.append(
        PatentAnalysisWarning(
            code=str(exc.code),
            message=exc.message,
            filename=filename,
            details=exc.details,
        )
    )
    return draft


def _ensure_official_core_sections(draft: AnalysisDraft, *, source: str) -> None:
    incomplete = {
        name: (
            draft.parts[name].status
            if draft.parts[name].status in {"missing", "error"}
            else "empty"
        )
        for name in ("description", "claims")
        if draft.parts[name].status in {"missing", "error"}
        or not draft.parts[name].text.strip()
    }
    if not incomplete:
        return
    result = draft.to_result()
    has_ocr_error = any(status == "error" for status in incomplete.values())
    raise PatentServiceError(
        code=ErrorCode.OCR_FAILED if has_ocr_error else ErrorCode.SECTION_DETECTION_INCOMPLETE,
        status_code=503 if has_ocr_error else 422,
        message=(
            "OCR could not extract the official description and claims; no total word count is available."
            if has_ocr_error
            else "The official package does not expose all required description and claims sections."
        ),
        source=source,
        details={
            "filename": draft.filename,
            "incomplete_parts": incomplete,
            "partial_result": result.model_dump(mode="json"),
        },
    )


def _build_response(
    *,
    input_mode: str,
    drafts: list[AnalysisDraft],
    patent_number: str | None = None,
) -> PatentAnalysisResponse:
    results = [draft.to_result() for draft in drafts]
    warnings = [warning for result in results for warning in result.warnings]
    _append_duplicate_warnings(drafts, results, warnings)
    if all(result.status == "success" for result in results):
        status = "success"
    elif all(result.status == "failed" for result in results):
        status = "failed"
    else:
        status = "partial"
    aggregate = PatentAnalysisAggregate(
        abstract_words=sum(item.parts.abstract.word_count for item in results),
        abstract_drawing_words=sum(
            item.parts.abstract_drawing.word_count for item in results
        ),
        description_words=sum(item.parts.description.word_count for item in results),
        description_drawings_words=sum(
            item.parts.description_drawings.word_count for item in results
        ),
        claims_words=sum(item.parts.claims.word_count for item in results),
        unclassified_words=sum(item.parts.unclassified.word_count for item in results),
        total_words=sum(item.total_words for item in results),
    )
    return PatentAnalysisResponse(
        input_mode=input_mode,
        status=status,
        patent_number=patent_number,
        files=results,
        aggregate=aggregate,
        warnings=warnings,
    )


def _append_duplicate_warnings(
    drafts: list[AnalysisDraft],
    results: list[PatentFileAnalysis],
    warnings: list[PatentAnalysisWarning],
) -> None:
    fingerprints = [text_five_grams(draft.comparable_text) for draft in drafts]
    for left in range(len(results)):
        for right in range(left + 1, len(results)):
            exact = bool(results[left].sha256) and results[left].sha256 == results[right].sha256
            similarity = _jaccard(fingerprints[left], fingerprints[right])
            if not exact and not (
                len(fingerprints[left]) >= 20
                and len(fingerprints[right]) >= 20
                and similarity >= 0.8
            ):
                continue
            warning = PatentAnalysisWarning(
                code="possible_duplicate_content",
                message=(
                    "The files are byte-identical. Counts were retained for both files."
                    if exact
                    else "The files contain highly similar patent text. Counts were retained for both files."
                ),
                details={
                    "files": [results[left].filename, results[right].filename],
                    "similarity": round(similarity, 4),
                    "exact": exact,
                },
            )
            warnings.append(warning)
            results[left].warnings.append(warning.model_copy(update={"filename": results[left].filename}))
            results[right].warnings.append(warning.model_copy(update={"filename": results[right].filename}))
            if results[left].status == "success":
                results[left].status = "partial"
            if results[right].status == "success":
                results[right].status = "partial"


def _jaccard(
    left: set[tuple[str, ...]], right: set[tuple[str, ...]]
) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
