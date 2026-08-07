import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisResponse,
    PatentLookupApiResponse,
    PatentLookupCacheInfo,
    PatentLookupEpResponse,
    PatentLookupResponse,
    PatentReference,
)


class SupabasePatentCache:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = (settings.supabase_url or "").rstrip("/")
        self._key = settings.supabase_secret_key or ""
        self._timeout = settings.request_timeout_seconds
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._url and self._key)

    async def find_lookup_fallback(
        self, reference: PatentReference
    ) -> tuple[str, PatentLookupApiResponse] | None:
        if not self.configured:
            return None
        patent = await self._find_patent(reference)
        if not patent or not await self._has_formal_request_link(str(patent["id"])):
            return None
        snapshot = patent.get("metadata_snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        snapshot = dict(snapshot)
        snapshot.pop("lookup_receipt", None)
        snapshot["data_origin"] = "cache_fallback"
        snapshot["cache"] = PatentLookupCacheInfo(
            is_cached=True,
            reason="official_source_no_result",
            last_successful_fetch_at=patent.get("last_successful_fetch_at"),
        ).model_dump(mode="json")
        try:
            if patent.get("source") == "epo":
                response: PatentLookupApiResponse = (
                    PatentLookupEpResponse.model_validate(snapshot)
                )
            else:
                response = PatentLookupResponse.model_validate(snapshot)
        except ValidationError:
            return None
        return str(patent["id"]), response

    async def record_lookup_event(
        self,
        *,
        trace_id: str,
        query: str,
        normalized_number: str | None,
        source: str | None,
        outcome: str,
        elapsed_ms: int,
        patent_id: str | None = None,
        document_id: str | None = None,
        request_id: str | None = None,
        user_id: str | None = None,
        organization_id: str | None = None,
        cache_status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if not self.configured:
            return
        await self._rest(
            "POST",
            "patent_lookup_events",
            json={
                "patent_id": patent_id,
                "document_id": document_id,
                "user_id": user_id,
                "organization_id": organization_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "query": query,
                "normalized_number": normalized_number,
                "source": source,
                "cache_status": cache_status,
                "outcome": outcome,
                "elapsed_ms": max(elapsed_ms, 0),
                "error_code": error_code,
                "error_message": error_message,
            },
            prefer="return=minimal",
        )

    async def get_formal_request(self, request_id: str) -> dict[str, Any] | None:
        rows = await self._rest(
            "GET",
            "translation_requests",
            params={
                "id": f"eq.{request_id}",
                "submitted_at": "not.is.null",
                "select": "id,requester_id,organization_id,submitted_at,workflow_stage",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def get_request_document(
        self, request_id: str
    ) -> dict[str, Any] | None:
        rows = await self._rest(
            "GET",
            "request_files",
            params={
                "request_id": f"eq.{request_id}",
                "source": "eq.patent_search",
                "select": (
                    "status,storage_bucket,storage_path,original_filename,"
                    "mime_type,patent_document_id"
                ),
                "limit": "1",
            },
        )
        if not rows:
            return None
        request_document = rows[0]
        request_file_status = request_document.get("status")
        document_id = request_document.get("patent_document_id")
        if not document_id:
            return request_document
        documents = await self._rest(
            "GET",
            "patent_documents",
            params={
                "id": f"eq.{document_id}",
                "select": (
                    "id,delivery_strategy,storage_bucket,storage_path,"
                    "original_filename,mime_type,upstream_source_url,status"
                ),
                "limit": "1",
            },
        )
        if documents:
            request_document.update(documents[0])
            request_document["document_status"] = documents[0].get("status")
            request_document["status"] = request_file_status
        return request_document

    async def download_document(
        self,
        bucket: str,
        storage_path: str,
    ) -> bytes:
        if not self.configured:
            raise _cache_error("The Supabase patent cache is not configured.")
        encoded_path = "/".join(
            quote(part, safe="") for part in storage_path.split("/")
        )
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
        }
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 120),
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    f"{self._url}/storage/v1/object/{bucket}/{encoded_path}",
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise _cache_error("The patent document could not be downloaded.") from exc
        if response.is_error:
            raise _cache_error(
                "The patent document download failed.",
                details={"status_code": response.status_code},
            )
        return response.content

    async def upsert_patent(
        self,
        *,
        reference: PatentReference,
        lookup: PatentLookupApiResponse,
        analysis: PatentAnalysisResponse,
        processing_status: str,
    ) -> dict[str, Any]:
        values = _patent_values(reference, lookup)
        values.update(
            {
                "analysis_snapshot": analysis.model_dump(
                    mode="json",
                    exclude={"analysis_receipt", "artifact"},
                    exclude_none=False,
                ),
                "processing_status": processing_status,
                "processing_error": None,
            }
        )
        rows = await self._rest(
            "POST",
            "patents",
            params={"on_conflict": "source,normalized_number"},
            json=values,
            prefer="resolution=merge-duplicates,return=representation",
        )
        if not rows:
            raise _cache_error("The patent cache did not return the saved patent.")
        patent = rows[0]
        aliases_by_normalized: dict[str, dict[str, str]] = {}
        for alias in (
            lookup.display_number,
            reference.display_number,
            lookup.normalized_number,
            reference.normalized_number,
            f"{reference.country_code}{reference.doc_number}",
        ):
            normalized_alias = normalize_alias(alias)
            if normalized_alias and normalized_alias not in aliases_by_normalized:
                aliases_by_normalized[normalized_alias] = {
                    "patent_id": str(patent["id"]),
                    "alias_number": alias,
                    "normalized_alias": normalized_alias,
                }
        aliases = list(aliases_by_normalized.values())
        if aliases:
            await self._rest(
                "POST",
                "patent_lookup_aliases",
                params={"on_conflict": "normalized_alias"},
                json=aliases,
                prefer="resolution=merge-duplicates,return=minimal",
            )
        return patent

    async def link_request_patent(self, request_id: str, patent_id: str) -> None:
        await self._rest(
            "PATCH",
            "request_patents",
            params={"request_id": f"eq.{request_id}"},
            json={"patent_id": patent_id},
            prefer="return=minimal",
        )

    async def set_request_file_status(
        self,
        request_id: str,
        status: str,
        *,
        document_id: str | None = None,
        document: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if document_id:
            values["patent_document_id"] = document_id
        if document:
            values.update(
                {
                    "storage_bucket": document.get("storage_bucket"),
                    "storage_path": document.get("storage_path"),
                    "original_filename": document["original_filename"],
                    "mime_type": document["mime_type"],
                }
            )
        await self._rest(
            "PATCH",
            "request_files",
            params={
                "request_id": f"eq.{request_id}",
                "source": "eq.patent_search",
            },
            json=values,
            prefer="return=minimal",
        )

    async def find_available_document(
        self,
        patent_id: str,
        *,
        delivery_strategy: str | None = None,
        upstream_source_url: str | None = None,
        sha256: str | None = None,
    ) -> dict[str, Any] | None:
        params = {
            "patent_id": f"eq.{patent_id}",
            "status": "eq.available",
            "document_type": "eq.original_publication",
            "select": "*",
            "order": "fetched_at.desc",
            "limit": "1",
        }
        if delivery_strategy:
            params["delivery_strategy"] = f"eq.{delivery_strategy}"
        if upstream_source_url:
            params["upstream_source_url"] = f"eq.{upstream_source_url}"
        if sha256:
            params["sha256"] = f"eq.{sha256}"
        rows = await self._rest(
            "GET",
            "patent_documents",
            params=params,
        )
        return rows[0] if rows else None

    async def claim_processing(self, patent_id: str) -> bool:
        rows = await self._rest(
            "PATCH",
            "patents",
            params={
                "id": f"eq.{patent_id}",
                "processing_status": "in.(pending,failed)",
                "select": "id",
            },
            json={
                "processing_status": "processing",
                "processing_started_at": _now(),
                "processing_completed_at": None,
                "processing_error": None,
            },
            prefer="return=representation",
        )
        return bool(rows)

    async def save_document(
        self,
        *,
        patent_id: str,
        source: str,
        normalized_number: str,
        filename: str,
        mime_type: str,
        content: bytes,
        sha256: str,
        source_url: str | None,
        kind_code: str | None,
    ) -> dict[str, Any]:
        safe_filename = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")
        storage_path = (
            f"{source}/{normalized_number}/{sha256[:16]}-"
            f"{safe_filename or normalized_number + '.bin'}"
        )
        await self._storage_upload(
            "patent-originals", storage_path, content, mime_type
        )
        rows = await self._rest(
            "POST",
            "patent_documents",
            params={
                "on_conflict": (
                    "patent_id,document_type,delivery_strategy,sha256"
                )
            },
            json={
                "patent_id": patent_id,
                "document_type": "original_publication",
                "delivery_strategy": "generated_cache",
                "version_label": kind_code,
                "kind_code": kind_code,
                "original_filename": filename,
                "mime_type": mime_type,
                "byte_size": len(content),
                "sha256": sha256,
                "storage_bucket": "patent-originals",
                "storage_path": storage_path,
                "upstream_source_url": source_url,
                "status": "available",
                "fetched_at": _now(),
            },
            prefer="resolution=merge-duplicates,return=representation",
        )
        if not rows:
            raise _cache_error("The cached patent document could not be saved.")
        return rows[0]

    async def save_external_document(
        self,
        *,
        patent_id: str,
        filename: str,
        mime_type: str,
        source_url: str,
        kind_code: str | None,
    ) -> dict[str, Any]:
        existing = await self.find_available_document(
            patent_id,
            delivery_strategy="external_url",
            upstream_source_url=source_url,
        )
        if existing:
            return existing
        try:
            rows = await self._rest(
                "POST",
                "patent_documents",
                json={
                    "patent_id": patent_id,
                    "document_type": "original_publication",
                    "delivery_strategy": "external_url",
                    "version_label": kind_code,
                    "kind_code": kind_code,
                    "original_filename": filename,
                    "mime_type": mime_type,
                    "byte_size": None,
                    "sha256": None,
                    "storage_bucket": None,
                    "storage_path": None,
                    "upstream_source_url": source_url,
                    "status": "available",
                    "fetched_at": _now(),
                },
                prefer="return=representation",
            )
        except PatentServiceError as exc:
            if exc.details.get("status_code") != 409:
                raise
            existing = await self.find_available_document(
                patent_id,
                delivery_strategy="external_url",
                upstream_source_url=source_url,
            )
            if existing:
                return existing
            raise
        if not rows:
            raise _cache_error("The external patent document could not be saved.")
        return rows[0]

    async def finish_processing(
        self, patent_id: str, *, error: str | None = None
    ) -> None:
        await self._rest(
            "PATCH",
            "patents",
            params={"id": f"eq.{patent_id}"},
            json={
                "processing_status": "failed" if error else "completed",
                "processing_completed_at": _now(),
                "processing_error": error,
            },
            prefer="return=minimal",
        )

    async def _find_patent(
        self, reference: PatentReference
    ) -> dict[str, Any] | None:
        rows = await self._rest(
            "GET",
            "patents",
            params={
                "source": f"eq.{reference.source.value}",
                "normalized_number": f"eq.{reference.normalized_number}",
                "record_status": "eq.active",
                "select": "*",
                "limit": "1",
            },
        )
        if rows:
            return rows[0]
        aliases = await self._rest(
            "GET",
            "patent_lookup_aliases",
            params={
                "normalized_alias": f"eq.{normalize_alias(reference.display_number)}",
                "select": "patent_id",
                "limit": "1",
            },
        )
        if not aliases:
            return None
        rows = await self._rest(
            "GET",
            "patents",
            params={
                "id": f"eq.{aliases[0]['patent_id']}",
                "record_status": "eq.active",
                "select": "*",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def _has_formal_request_link(self, patent_id: str) -> bool:
        rows = await self._rest(
            "GET",
            "request_patents",
            params={
                "patent_id": f"eq.{patent_id}",
                "select": "id",
                "limit": "1",
            },
        )
        return bool(rows)

    async def _rest(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        prefer: str | None = None,
    ) -> Any:
        if not self.configured:
            raise _cache_error("The Supabase patent cache is not configured.")
        headers = {"apikey": self._key}
        if prefer:
            headers["Prefer"] = prefer
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.request(
                    method,
                    f"{self._url}/rest/v1/{table}",
                    params=params,
                    json=json,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise _cache_error("The patent cache could not be reached.") from exc
        if response.is_error:
            raise _cache_error(
                "The patent cache request failed.",
                details={
                    "table": table,
                    "status_code": response.status_code,
                    "response": response.text[:500],
                },
            )
        if not response.content:
            return []
        return response.json()

    async def _storage_upload(
        self,
        bucket: str,
        storage_path: str,
        content: bytes,
        mime_type: str,
    ) -> None:
        encoded_path = "/".join(quote(part, safe="") for part in storage_path.split("/"))
        headers = {
            "apikey": self._key,
            "Content-Type": mime_type,
            "x-upsert": "true",
        }
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 120),
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    f"{self._url}/storage/v1/object/{bucket}/{encoded_path}",
                    headers=headers,
                    content=content,
                )
        except httpx.HTTPError as exc:
            raise _cache_error("The patent document could not be uploaded.") from exc
        if response.is_error:
            raise _cache_error(
                "The patent document upload failed.",
                details={"status_code": response.status_code},
            )


def normalize_alias(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _patent_values(
    reference: PatentReference, response: PatentLookupApiResponse
) -> dict[str, Any]:
    snapshot = response.model_dump(
        mode="json", exclude={"lookup_receipt"}, exclude_none=False
    )
    if isinstance(response, PatentLookupEpResponse):
        application_no = response.application_no
        publication_no = response.publication_no
        title = response.title
        publication_date = response.publication_date
    else:
        application_no = (
            response.application_no or response.basic_info.application_number
        )
        publication_no = response.publication_no
        title = response.basic_info.title
        publication_date = (
            response.publication_date or response.basic_info.publication_date
        )
    now = datetime.now(UTC)
    return {
        "source": reference.source.value,
        "normalized_number": reference.normalized_number,
        "display_number": response.display_number,
        "jurisdiction": reference.country_code,
        "kind_code": reference.kind_code,
        "application_no": application_no or None,
        "publication_no": publication_no or None,
        "title": title or None,
        "publication_date": _database_date(publication_date),
        "metadata_snapshot": snapshot,
        "raw_source_refs": response.raw_source_refs,
        "record_status": "active",
        "last_successful_fetch_at": now.isoformat(),
    }


def _database_date(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _cache_error(
    message: str, *, details: dict[str, Any] | None = None
) -> PatentServiceError:
    return PatentServiceError(
        code=ErrorCode.CACHE_UNAVAILABLE,
        status_code=503,
        message=message,
        source="cache",
        details=details,
    )
