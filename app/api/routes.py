import asyncio
import logging
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.analysis.service import PatentAnalysisService
from app.analysis.ocr import AutoOcrEngine
from app.analysis.uploads import StoredUpload, validate_upload
from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.clients.wipo_patentscope_rest import WipoPatentScopeRestClient
from app.config import Settings, get_settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisResponse,
    PatentLookupApiResponse,
    PatentLookupRequest,
)
from app.services.patent_lookup import PatentLookupService

router = APIRouter()
logger = logging.getLogger("patent_service")


def get_lookup_service(
    settings: Settings = Depends(get_settings),
) -> PatentLookupService:
    return PatentLookupService(
        settings=settings,
        epo_ops_client=EpoOpsClient(settings),
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        wipo_rest_client=WipoPatentScopeRestClient(settings),
        wipo_soap_client=WipoPatentScopeClient(settings),
    )


@lru_cache(maxsize=1)
def get_ocr_engine() -> AutoOcrEngine:
    """Keep model sessions alive for the lifetime of the API process."""
    return AutoOcrEngine(get_settings())


def get_analysis_service(
    settings: Settings = Depends(get_settings),
    lookup_service: PatentLookupService = Depends(get_lookup_service),
    ocr: AutoOcrEngine = Depends(get_ocr_engine),
) -> PatentAnalysisService:
    return PatentAnalysisService(
        settings=settings,
        lookup_service=lookup_service,
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url,
            timeout_seconds=settings.request_timeout_seconds,
        ),
        epo_ops_client=EpoOpsClient(settings),
        ocr=ocr,
    )


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
) -> PatentLookupApiResponse:
    started_at = time.monotonic()
    logger.info(
        "lookup request started patent_number=%s include_original_file=%s",
        request.patent_number,
        request.include_original_file,
    )
    response = await service.lookup_patent(request)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "lookup request finished patent_number=%s source=%s normalized_number=%s include_original_file=%s elapsed_ms=%s",
        request.patent_number,
        response.source,
        response.normalized_number,
        request.include_original_file,
        elapsed_ms,
    )
    return response


@router.post("/patents/analyze", response_model=PatentAnalysisResponse)
async def analyze_patent(
    patent_number: str | None = Form(default=None),
    files: list[UploadFile] | None = File(default=None),
    settings: Settings = Depends(get_settings),
    service: PatentAnalysisService = Depends(get_analysis_service),
) -> PatentAnalysisResponse:
    started_at = time.monotonic()
    analysis_id = uuid.uuid4().hex[:12]
    normalized_input = (patent_number or "").strip()
    uploaded_files = [item for item in (files or []) if item.filename]
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
            result = await asyncio.wait_for(
                service.analyze_patent(normalized_input),
                timeout=settings.analysis_timeout_seconds,
            )
        else:
            logger.info(
                "analysis request started analysis_id=%s input_mode=upload files=%s filenames=%s timeout_seconds=%s",
                analysis_id,
                len(uploaded_files),
                ",".join(upload.filename or "unnamed" for upload in uploaded_files),
                settings.analysis_timeout_seconds,
            )
            result = await _analyze_uploaded_files(uploaded_files, settings, service)
        logger.info(
            "analysis request finished analysis_id=%s input_mode=%s patent_number=%s status=%s total_words=%s elapsed_ms=%s",
            analysis_id,
            result.input_mode,
            result.patent_number or "",
            result.status,
            result.aggregate.total_words,
            int((time.monotonic() - started_at) * 1000),
        )
        return result
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
    finally:
        for upload in uploaded_files:
            await upload.close()


async def _analyze_uploaded_files(
    files: list[UploadFile],
    settings: Settings,
    service: PatentAnalysisService,
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
        return await asyncio.wait_for(
            asyncio.to_thread(service.analyze_uploads, stored),
            timeout=settings.analysis_timeout_seconds,
        )


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
