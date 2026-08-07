import asyncio
import logging
import tempfile
import time
import uuid
import hmac
from functools import lru_cache
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from urllib.parse import quote

from app.analysis.cancellation import AnalysisCancellation, AnalysisCancelled
from app.analysis.artifacts import AnalysisArtifactStore
from app.analysis.service import PatentAnalysisService
from app.analysis.ocr import AutoOcrEngine
from app.analysis.uploads import StoredUpload, validate_upload
from app.cache.supabase import SupabasePatentCache
from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.clients.wipo_patentscope_rest import WipoPatentScopeRestClient
from app.config import Settings, get_settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisResponse,
    PatentCacheAcceptedResponse,
    PatentCacheRequest,
    PatentLookupApiResponse,
    PatentLookupRequest,
    PatentReceiptVerificationRequest,
    PatentReceiptVerificationResponse,
    PatentSource,
)
from app.security.receipts import ReceiptSigner
from app.services.patent_cache import PatentCacheService
from app.services.patent_lookup import PatentLookupService
from app.utils.patent_numbers import normalize_patent_number

router = APIRouter()
logger = logging.getLogger("patent_service")


@lru_cache(maxsize=1)
def get_epo_ops_client() -> EpoOpsClient:
    return EpoOpsClient(get_settings())


@lru_cache(maxsize=1)
def get_supabase_cache() -> SupabasePatentCache:
    return SupabasePatentCache(get_settings())


@lru_cache(maxsize=1)
def get_wipo_rest_client() -> WipoPatentScopeRestClient:
    return WipoPatentScopeRestClient(get_settings())


@lru_cache(maxsize=1)
def get_receipt_signer() -> ReceiptSigner:
    return ReceiptSigner(get_settings())


@lru_cache(maxsize=1)
def get_artifact_store() -> AnalysisArtifactStore:
    return AnalysisArtifactStore(get_settings())


@lru_cache(maxsize=1)
def get_lookup_service() -> PatentLookupService:
    settings = get_settings()
    return PatentLookupService(
        settings=settings,
        epo_ops_client=get_epo_ops_client(),
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        wipo_rest_client=get_wipo_rest_client(),
        wipo_soap_client=WipoPatentScopeClient(settings),
        cache=get_supabase_cache(),
    )


@lru_cache(maxsize=1)
def get_ocr_engine() -> AutoOcrEngine:
    """Keep model sessions alive for the lifetime of the API process."""
    return AutoOcrEngine(get_settings())


@lru_cache(maxsize=1)
def get_analysis_service() -> PatentAnalysisService:
    settings = get_settings()
    return PatentAnalysisService(
        settings=settings,
        lookup_service=get_lookup_service(),
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        epo_ops_client=get_epo_ops_client(),
        ocr=get_ocr_engine(),
        artifact_store=get_artifact_store(),
    )


@lru_cache(maxsize=1)
def get_patent_cache_service() -> PatentCacheService:
    settings = get_settings()
    return PatentCacheService(
        cache=get_supabase_cache(),
        lookup_service=get_lookup_service(),
        artifact_store=get_artifact_store(),
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        original_file_max_bytes=settings.original_file_max_bytes,
    )


def _require_service_api_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.api_key
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:]
    if not expected or not hmac.compare_digest(supplied, expected):
        raise PatentServiceError(
            code=ErrorCode.SERVICE_AUTH_REQUIRED,
            status_code=401,
            message="A valid patent-service API key is required.",
            source="service",
        )


def normalize_receipt_number(value: str, source: PatentSource) -> str:
    return normalize_patent_number(
        value,
        source_override=source,
    ).normalized_number


@router.get("/health")
async def health(
    settings: Settings = Depends(get_settings),
    ocr: AutoOcrEngine = Depends(get_ocr_engine),
) -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "sources": {
            "epo_ops_configured": settings.epo_ops_configured,
            "wipo_rest_configured": settings.wipo_rest_configured,
            "wipo_soap_configured": settings.wipo_soap_configured,
        },
        "ocr": ocr.diagnostics(),
    }


@router.post("/patents/lookup", response_model=PatentLookupApiResponse)
async def lookup_patent(
    request: PatentLookupRequest,
    service: PatentLookupService = Depends(get_lookup_service),
    signer: ReceiptSigner = Depends(get_receipt_signer),
) -> PatentLookupApiResponse:
    started_at = time.monotonic()
    trace_id = uuid.uuid4().hex
    logger.info(
        "lookup request started trace_id=%s patent_number=%s",
        trace_id,
        request.patent_number,
    )
    try:
        response = await service.lookup_patent(request, trace_id=trace_id)
    except TypeError as exc:
        if "trace_id" not in str(exc):
            raise
        response = await service.lookup_patent(request)
    response = response.model_copy(
        update={"lookup_receipt": signer.sign_lookup(response)}
    )
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "lookup request finished trace_id=%s patent_number=%s source=%s normalized_number=%s data_origin=%s elapsed_ms=%s",
        trace_id,
        request.patent_number,
        response.source,
        response.normalized_number,
        response.data_origin,
        elapsed_ms,
    )
    return response


@router.post("/patents/analyze", response_model=PatentAnalysisResponse)
async def analyze_patent(
    http_request: Request,
    patent_number: str | None = Form(default=None),
    source: PatentSource | None = Form(default=None),
    files: list[UploadFile] | None = File(default=None),
    settings: Settings = Depends(get_settings),
    service: PatentAnalysisService = Depends(get_analysis_service),
    signer: ReceiptSigner = Depends(get_receipt_signer),
) -> PatentAnalysisResponse:
    started_at = time.monotonic()
    analysis_id = uuid.uuid4().hex[:12]
    normalized_input = (patent_number or "").strip()
    uploaded_files = [item for item in (files or []) if item.filename]
    cancellation = AnalysisCancellation()
    if bool(normalized_input) == bool(uploaded_files):
        raise PatentServiceError(
            code=ErrorCode.AMBIGUOUS_ANALYSIS_INPUT,
            status_code=422,
            message="Provide either patent_number or one or more files, but not both.",
        )
    try:
        if normalized_input:
            logger.info(
                "analysis request started analysis_id=%s input_mode=patent_number patent_number=%s timeout_seconds=%s",
                analysis_id,
                normalized_input,
                settings.analysis_timeout_seconds,
            )
            result = await _run_monitored_analysis(
                http_request=http_request,
                cancellation=cancellation,
                analysis=service.analyze_patent(
                    normalized_input,
                    source=source,
                    cancellation=cancellation,
                ),
                timeout_seconds=settings.analysis_timeout_seconds,
            )
        else:
            logger.info(
                "analysis request started analysis_id=%s input_mode=upload files=%s filenames=%s timeout_seconds=%s",
                analysis_id,
                len(uploaded_files),
                ",".join(upload.filename or "unnamed" for upload in uploaded_files),
                settings.analysis_timeout_seconds,
            )
            result = await _run_monitored_analysis(
                http_request=http_request,
                cancellation=cancellation,
                analysis=_analyze_uploaded_files(
                    uploaded_files,
                    settings,
                    service,
                    cancellation,
                ),
                timeout_seconds=settings.analysis_timeout_seconds,
            )
        logger.info(
            "analysis request finished analysis_id=%s input_mode=%s patent_number=%s status=%s total_words=%s elapsed_ms=%s",
            analysis_id,
            result.input_mode,
            result.patent_number or "",
            result.status,
            result.aggregate.total_words,
            int((time.monotonic() - started_at) * 1000),
        )
        return result.model_copy(
            update={"analysis_receipt": signer.sign_analysis(result)}
        )
    except TimeoutError as exc:
        logger.error(
            "analysis request timed out analysis_id=%s patent_number=%s timeout_seconds=%s elapsed_ms=%s",
            analysis_id,
            normalized_input,
            settings.analysis_timeout_seconds,
            int((time.monotonic() - started_at) * 1000),
        )
        raise PatentServiceError(
            code=ErrorCode.ANALYSIS_TIMEOUT,
            status_code=504,
            message="Patent analysis exceeded the configured time limit.",
            details={"timeout_seconds": settings.analysis_timeout_seconds},
        ) from exc
    except AnalysisCancelled as exc:
        logger.info(
            "analysis request cancelled analysis_id=%s patent_number=%s elapsed_ms=%s",
            analysis_id,
            normalized_input,
            int((time.monotonic() - started_at) * 1000),
        )
        raise PatentServiceError(
            code=ErrorCode.ANALYSIS_CANCELLED,
            status_code=499,
            message="Patent analysis was cancelled because the client disconnected.",
            source="service",
        ) from exc
    finally:
        for upload in uploaded_files:
            await upload.close()


@router.post(
    "/patents/receipts/verify",
    response_model=PatentReceiptVerificationResponse,
)
async def verify_patent_receipts(
    request: PatentReceiptVerificationRequest,
    _: None = Depends(_require_service_api_key),
    signer: ReceiptSigner = Depends(get_receipt_signer),
) -> PatentReceiptVerificationResponse:
    lookup = signer.verify_lookup(request.lookup_receipt)
    analysis = signer.verify_analysis(request.analysis_receipt)
    if (
        not analysis.patent_number
        or (
            analysis.source_document
            and analysis.source_document.source != lookup.source
        )
        or normalize_receipt_number(analysis.patent_number, lookup.source)
        != normalize_receipt_number(lookup.normalized_number, lookup.source)
    ):
        raise PatentServiceError(
            code=ErrorCode.INVALID_RECEIPT,
            status_code=422,
            message="The lookup and analysis receipts refer to different patents.",
            source="service",
        )
    return PatentReceiptVerificationResponse(lookup=lookup, analysis=analysis)


@router.post(
    "/patents/cache",
    response_model=PatentCacheAcceptedResponse,
    status_code=202,
)
async def cache_submitted_patent(
    request: PatentCacheRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_service_api_key),
    signer: ReceiptSigner = Depends(get_receipt_signer),
    service: PatentCacheService = Depends(get_patent_cache_service),
) -> PatentCacheAcceptedResponse:
    lookup = signer.verify_lookup(request.lookup_receipt)
    analysis = signer.verify_analysis(request.analysis_receipt)
    accepted = await service.prepare(
        request_id=request.request_id,
        lookup=lookup,
        analysis=analysis,
    )
    if accepted.status == "pending":
        background_tasks.add_task(
            service.process,
            request_id=request.request_id,
            patent_id=accepted.patent_id,
            lookup=lookup,
            analysis=analysis,
        )
    return accepted


@router.get("/patents/cache/requests/{request_id}/file")
async def download_cached_patent_file(
    request_id: str,
    _: None = Depends(_require_service_api_key),
    service: PatentCacheService = Depends(get_patent_cache_service),
) -> Response:
    content, filename, mime_type = await service.download_request_document(
        request_id
    )
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "private, no-store",
        },
    )


async def _analyze_uploaded_files(
    files: list[UploadFile],
    settings: Settings,
    service: PatentAnalysisService,
    cancellation: AnalysisCancellation,
) -> PatentAnalysisResponse:
    if len(files) > settings.analysis_max_files:
        raise PatentServiceError(
            code=ErrorCode.UPLOAD_TOO_LARGE,
            status_code=413,
            message="Too many files were uploaded.",
            details={"count": len(files), "max_files": settings.analysis_max_files},
        )
    with tempfile.TemporaryDirectory(prefix="patent-analysis-") as directory:
        stored: list[StoredUpload] = []
        total_size = 0
        for index, upload in enumerate(files):
            suffix = Path(upload.filename or "").suffix.lower()
            target = Path(directory) / f"{index}-{uuid.uuid4().hex}{suffix}"
            file_size = 0
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    file_size += len(chunk)
                    total_size += len(chunk)
                    if file_size > settings.analysis_max_file_bytes:
                        raise PatentServiceError(
                            code=ErrorCode.UPLOAD_TOO_LARGE,
                            status_code=413,
                            message="An uploaded file exceeds the configured size limit.",
                            details={
                                "filename": upload.filename,
                                "max_bytes": settings.analysis_max_file_bytes,
                            },
                        )
                    if total_size > settings.analysis_max_total_bytes:
                        raise PatentServiceError(
                            code=ErrorCode.UPLOAD_TOO_LARGE,
                            status_code=413,
                            message="The combined upload exceeds the configured size limit.",
                            details={"max_bytes": settings.analysis_max_total_bytes},
                        )
                    output.write(chunk)
            stored.append(
                validate_upload(
                    target,
                    filename=upload.filename or f"upload-{index}{suffix}",
                    content_type=upload.content_type or "application/octet-stream",
                    settings=settings,
                )
            )
            logger.info(
                "uploaded document received document=%s file_type=%s bytes=%s step=validation action=complete",
                stored[-1].filename,
                stored[-1].file_type,
                file_size,
            )
        return await asyncio.to_thread(
            service.analyze_uploads,
            stored,
            cancellation=cancellation,
        )


async def _run_monitored_analysis(
    *,
    http_request: Request,
    cancellation: AnalysisCancellation,
    analysis,
    timeout_seconds: float,
):
    analysis_task = asyncio.create_task(analysis)
    disconnect_task = asyncio.create_task(
        _wait_for_disconnect(http_request, cancellation)
    )
    done, _ = await asyncio.wait(
        {analysis_task, disconnect_task},
        timeout=timeout_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if analysis_task in done:
        disconnect_task.cancel()
        return await analysis_task

    timed_out = disconnect_task not in done
    cancellation.cancel()
    reason = "timeout" if timed_out else "client_disconnect"
    logger.info("analysis cancellation requested reason=%s", reason)
    try:
        await analysis_task
    except (AnalysisCancelled, PatentServiceError):
        pass
    finally:
        disconnect_task.cancel()
    if timed_out:
        raise TimeoutError
    raise AnalysisCancelled


async def _wait_for_disconnect(
    request: Request,
    cancellation: AnalysisCancellation,
) -> None:
    while not cancellation.cancelled:
        if await request.is_disconnected():
            cancellation.cancel()
            return
        await asyncio.sleep(0.2)


@router.get("/patents/files/{filename}", response_class=FileResponse)
async def download_patent_file(
    filename: str, settings: Settings = Depends(get_settings)
) -> FileResponse:
    if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=404, detail="Patent file not found.")
    storage_dir = Path(
        settings.wipo_storage_dir
        or Path(tempfile.gettempdir()) / "patent-service" / "wipo"
    ).resolve()
    file_path = (storage_dir / filename).resolve()
    if file_path.parent != storage_dir or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Patent file not found.")
    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=filename,
    )
