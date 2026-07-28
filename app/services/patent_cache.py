import asyncio
import hashlib
import logging
import tempfile
import uuid
from pathlib import Path

import httpx

from app.cache.supabase import SupabasePatentCache
from app.analysis.artifacts import AnalysisArtifactStore
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisResponse,
    PatentCacheAcceptedResponse,
    PatentLookupApiResponse,
    PatentLookupEpResponse,
    PatentLookupRequest,
)
from app.services.patent_lookup import PatentLookupService
from app.utils.patent_numbers import normalize_patent_number

logger = logging.getLogger("patent_service")


class PatentCacheService:
    def __init__(
        self,
        *,
        cache: SupabasePatentCache,
        lookup_service: PatentLookupService,
        artifact_store: AnalysisArtifactStore | None = None,
    ) -> None:
        self._cache = cache
        self._lookup_service = lookup_service
        self._artifact_store = artifact_store

    async def prepare(
        self,
        *,
        request_id: str,
        lookup: PatentLookupApiResponse,
        analysis: PatentAnalysisResponse,
    ) -> PatentCacheAcceptedResponse:
        request = await self._cache.get_formal_request(request_id)
        if not request:
            raise PatentServiceError(
                code=ErrorCode.CACHE_UNAVAILABLE,
                status_code=409,
                message="The patent cache can only be created for a submitted Request.",
                source="cache",
                details={"request_id": request_id},
            )
        reference = normalize_patent_number(lookup.normalized_number)
        if (
            analysis.input_mode != "patent_number"
            or not analysis.patent_number
            or normalize_patent_number(analysis.patent_number).normalized_number
            != reference.normalized_number
        ):
            raise PatentServiceError(
                code=ErrorCode.INVALID_RECEIPT,
                status_code=422,
                message="The lookup and analysis receipts refer to different patents.",
                source="service",
            )

        patent = await self._cache.upsert_patent(
            reference=reference,
            lookup=lookup,
            analysis=analysis,
            processing_status="pending",
        )
        patent_id = str(patent["id"])
        await self._cache.link_request_patent(request_id, patent_id)

        document = await self._cache.find_available_document(patent_id)
        if document:
            await self._cache.set_request_file_status(
                request_id,
                "parsed",
                document_id=str(document["id"]),
                document=document,
            )
            await self._cache.finish_processing(patent_id)
            if analysis.artifact and self._artifact_store:
                await asyncio.to_thread(
                    self._artifact_store.discard,
                    analysis.artifact.artifact_id,
                )
            return PatentCacheAcceptedResponse(
                request_id=request_id,
                patent_id=patent_id,
                status="completed",
            )

        await self._cache.set_request_file_status(request_id, "parsing")
        return PatentCacheAcceptedResponse(
            request_id=request_id,
            patent_id=patent_id,
            status="pending",
        )

    async def download_request_document(
        self,
        request_id: str,
    ) -> tuple[bytes, str, str]:
        request = await self._cache.get_formal_request(request_id)
        if not request:
            raise PatentServiceError(
                code=ErrorCode.CACHE_UNAVAILABLE,
                status_code=404,
                message="The submitted Request is unavailable.",
                source="cache",
            )
        document = await self._cache.get_request_document(request_id)
        if not document:
            raise _original_unavailable()
        status = document.get("status")
        if status == "parsing":
            raise PatentServiceError(
                code=ErrorCode.CACHE_UNAVAILABLE,
                status_code=409,
                message="The original patent file is still being prepared.",
                source="cache",
            )
        if status == "failed":
            raise PatentServiceError(
                code=ErrorCode.CACHE_UNAVAILABLE,
                status_code=409,
                message="Original patent file preparation failed. Retry it first.",
                source="cache",
            )
        if (
            status != "parsed"
            or not document.get("patent_document_id")
            or not document.get("storage_bucket")
            or not document.get("storage_path")
        ):
            raise _original_unavailable()
        content = await self._cache.download_document(
            str(document["storage_bucket"]),
            str(document["storage_path"]),
        )
        return (
            content,
            str(document.get("original_filename") or "patent-document.pdf"),
            str(document.get("mime_type") or "application/octet-stream"),
        )

    async def process(
        self,
        *,
        request_id: str,
        patent_id: str,
        lookup: PatentLookupApiResponse,
        analysis: PatentAnalysisResponse,
    ) -> None:
        if not await self._cache.claim_processing(patent_id):
            return
        trace_id = f"cache-{uuid.uuid4().hex}"
        reference = normalize_patent_number(lookup.normalized_number)
        try:
            prepared = await self._read_prepared_artifact(analysis)
            if prepared is None:
                with tempfile.TemporaryDirectory(
                    prefix="patent-cache-fallback-"
                ) as directory:
                    full = await self._lookup_service.lookup_patent_full(
                        PatentLookupRequest(
                            patent_number=reference.normalized_number,
                            include_original_file=True,
                        ),
                        storage_dir=Path(directory),
                    )
                    content, filename, mime_type, source_url = (
                        await _read_original_file(full)
                    )
            else:
                content, filename, mime_type, source_url = prepared
            document = await self._cache.save_document(
                patent_id=patent_id,
                source=reference.source.value,
                normalized_number=reference.normalized_number,
                filename=filename,
                mime_type=mime_type,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                source_url=source_url,
                kind_code=reference.kind_code,
            )
            await self._cache.set_request_file_status(
                request_id,
                "parsed",
                document_id=str(document["id"]),
                document=document,
            )
            await self._cache.finish_processing(patent_id)
            if analysis.artifact and self._artifact_store:
                await asyncio.to_thread(
                    self._artifact_store.discard,
                    analysis.artifact.artifact_id,
                )
            await self._cache.record_lookup_event(
                trace_id=trace_id,
                query=reference.normalized_number,
                normalized_number=reference.normalized_number,
                source=reference.source.value,
                outcome="success",
                elapsed_ms=0,
                patent_id=patent_id,
                document_id=str(document["id"]),
                request_id=request_id,
                cache_status="miss_fetched",
            )
        except Exception as exc:
            message = (
                exc.message if isinstance(exc, PatentServiceError) else str(exc)
            )
            logger.exception(
                "patent cache processing failed request_id=%s patent_id=%s",
                request_id,
                patent_id,
            )
            await self._cache.finish_processing(patent_id, error=message)
            await self._cache.set_request_file_status(request_id, "failed")
            try:
                await self._cache.record_lookup_event(
                    trace_id=trace_id,
                    query=reference.normalized_number,
                    normalized_number=reference.normalized_number,
                    source=reference.source.value,
                    outcome="error",
                    elapsed_ms=0,
                    patent_id=patent_id,
                    request_id=request_id,
                    error_code=(
                        exc.code.value
                        if isinstance(exc, PatentServiceError)
                        else type(exc).__name__
                    ),
                    error_message=message,
                )
            except PatentServiceError:
                pass

    async def _read_prepared_artifact(
        self,
        analysis: PatentAnalysisResponse,
    ) -> tuple[bytes, str, str, str | None] | None:
        artifact = analysis.artifact
        if not artifact or not self._artifact_store:
            return None
        try:
            content = await asyncio.to_thread(
                self._artifact_store.read_bytes,
                artifact,
            )
        except PatentServiceError as exc:
            if exc.code is not ErrorCode.ANALYSIS_ARTIFACT_UNAVAILABLE:
                raise
            logger.info(
                "prepared analysis artifact unavailable; official source fallback required artifact_id=%s",
                artifact.artifact_id,
            )
            return None
        return (
            content,
            artifact.filename,
            artifact.mime_type,
            None,
        )


async def _read_original_file(
    response: PatentLookupApiResponse,
) -> tuple[bytes, str, str, str | None]:
    if isinstance(response, PatentLookupEpResponse):
        if not response.original_file_download_url:
            raise _original_unavailable()
        content, mime_type = await _download(response.original_file_download_url)
        return (
            content,
            f"{response.normalized_number}.pdf",
            mime_type or "application/pdf",
            response.original_file_download_url,
        )

    original = response.original_file
    if original.storage_path:
        path = Path(original.storage_path)
        if path.is_file():
            return (
                await asyncio.to_thread(path.read_bytes),
                original.filename or path.name,
                original.content_type or "application/octet-stream",
                original.download_url or None,
            )
    if original.download_url.startswith(("http://", "https://")):
        content, mime_type = await _download(original.download_url)
        return (
            content,
            original.filename or f"{response.normalized_number}.pdf",
            original.content_type or mime_type or "application/octet-stream",
            original.download_url,
        )
    raise _original_unavailable()


async def _download(url: str) -> tuple[bytes, str | None]:
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise _original_unavailable() from exc
    return response.content, response.headers.get("content-type")


def _original_unavailable() -> PatentServiceError:
    return PatentServiceError(
        code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
        status_code=502,
        message="The original patent file could not be prepared for caching.",
        source="cache",
    )
