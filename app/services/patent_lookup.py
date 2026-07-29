import asyncio
import calendar
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, Protocol
from pathlib import Path

from app.cache.supabase import SupabasePatentCache
from app.clients.epo_ops import EpoClaimsContent, EpoDescriptionContent, EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentDrawingsInfo,
    PatentDesignatedStates,
    PatentLookupApiResponse,
    PatentLookupEpResponse,
    PatentLookupRequest,
    PatentLookupResponse,
    PatentLookupWarning,
    PatentOriginalFile,
    PatentReference,
    PatentSource,
)
from app.utils.patent_numbers import normalize_patent_number
from app.utils.text_metrics import count_words


class WipoLookupClient(Protocol):
    async def lookup_patent(
        self,
        reference: PatentReference,
        *,
        include_original_file: bool,
        storage_dir: Path | None = None,
    ) -> PatentLookupResponse: ...


logger = logging.getLogger("patent_service")


class PatentLookupService:
    def __init__(
        self,
        *,
        settings: Settings,
        epo_ops_client: EpoOpsClient,
        epo_publication_server_client: EpoPublicationServerClient,
        wipo_rest_client: WipoLookupClient,
        wipo_soap_client: WipoLookupClient,
        cache: SupabasePatentCache | None = None,
    ) -> None:
        self._settings = settings
        self._epo_ops_client = epo_ops_client
        self._epo_publication_server_client = epo_publication_server_client
        self._wipo_rest_client = wipo_rest_client
        self._wipo_soap_client = wipo_soap_client
        self._cache = cache

    async def lookup_patent(
        self, request: PatentLookupRequest, *, trace_id: str | None = None
    ) -> PatentLookupApiResponse:
        reference = normalize_patent_number(request.patent_number)
        lookup_trace_id = trace_id or uuid.uuid4().hex
        started_at = time.monotonic()
        logger.info(
            "quick lookup normalized trace_id=%s patent_number=%s source=%s normalized_number=%s",
            lookup_trace_id,
            request.patent_number,
            reference.source,
            reference.normalized_number,
        )
        try:
            response = await self._lookup_official_quick(reference)
        except PatentServiceError as exc:
            if exc.code != ErrorCode.SOURCE_NO_RESULT:
                await self._safe_record_lookup_event(
                    trace_id=lookup_trace_id,
                    query=request.patent_number,
                    reference=reference,
                    outcome="error",
                    started_at=started_at,
                    error=exc,
                )
                raise
            await self._safe_record_lookup_event(
                trace_id=lookup_trace_id,
                query=request.patent_number,
                reference=reference,
                outcome="not_found",
                started_at=started_at,
                error=exc,
            )
            official_elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.info(
                "quick lookup official no result trace_id=%s normalized_number=%s source=%s elapsed_ms=%s",
                lookup_trace_id,
                reference.normalized_number,
                reference.source.value,
                official_elapsed_ms,
            )
            cache_started_at = time.monotonic()
            fallback = await self._read_cache_fallback(reference)
            if fallback:
                patent_id, cached_response = fallback
                await self._safe_record_lookup_event(
                    trace_id=lookup_trace_id,
                    query=request.patent_number,
                    reference=reference,
                    outcome="success",
                    started_at=cache_started_at,
                    patent_id=patent_id,
                    cache_status="stale_fallback",
                )
                logger.info(
                    "quick lookup cache fallback trace_id=%s normalized_number=%s elapsed_ms=%s",
                    lookup_trace_id,
                    reference.normalized_number,
                    int((time.monotonic() - started_at) * 1000),
                )
                return cached_response
            await self._safe_record_lookup_event(
                trace_id=lookup_trace_id,
                query=request.patent_number,
                reference=reference,
                outcome="not_found",
                started_at=cache_started_at,
                cache_status="miss",
            )
            raise

        await self._safe_record_lookup_event(
            trace_id=lookup_trace_id,
            query=request.patent_number,
            reference=reference,
            outcome="success",
            started_at=started_at,
        )
        logger.info(
            "quick lookup official result trace_id=%s normalized_number=%s elapsed_ms=%s",
            lookup_trace_id,
            reference.normalized_number,
            int((time.monotonic() - started_at) * 1000),
        )
        return response

    async def lookup_patent_full(
        self,
        request: PatentLookupRequest,
        *,
        storage_dir: Path | None = None,
    ) -> PatentLookupApiResponse:
        reference = normalize_patent_number(request.patent_number)
        if reference.source is PatentSource.EPO:
            return await self._lookup_ep(reference)
        return await self._lookup_wo(
            reference,
            include_original_file=request.include_original_file,
            storage_dir=storage_dir,
        )

    async def resolve_ep_publication_reference(
        self, reference: PatentReference
    ) -> PatentReference:
        resolved, _ = await self._resolve_ep_publication_reference(reference)
        return resolved

    async def _lookup_official_quick(
        self, reference: PatentReference
    ) -> PatentLookupApiResponse:
        if reference.source is PatentSource.EPO:
            return await self._lookup_ep_quick(reference)
        lookup_bibliographic = getattr(
            self._wipo_rest_client, "lookup_bibliographic", None
        )
        if lookup_bibliographic is None:
            # Backward-compatible path for injected clients. The production REST
            # client always implements IASR-only lookup_bibliographic.
            return await self._lookup_wo(
                reference,
                include_original_file=False,
            )
        return await lookup_bibliographic(reference)

    async def _lookup_ep_quick(
        self, reference: PatentReference
    ) -> PatentLookupEpResponse:
        lookup_reference, application_register_refs = (
            await self._resolve_ep_publication_reference(reference)
        )
        biblio_xml = await self._epo_ops_client.fetch_bibliographic_data(
            lookup_reference
        )
        basic_info, refs = self._epo_ops_client.parse_bibliographic_data(biblio_xml)
        publication_reference = refs.get("publication_reference", {})
        application_reference = refs.get("application_reference", {})
        first_priority_date = refs.get("first_priority_date")
        raw_source_refs: dict[str, Any] = {
            "lookup_mode": "ops_biblio_quick",
            "ops_biblio": {
                "endpoint": self._epo_ops_client.build_biblio_path(lookup_reference),
                **refs,
            },
        }
        if application_register_refs:
            raw_source_refs["ops_application_register"] = application_register_refs
        return PatentLookupEpResponse(
            source=PatentSource.EPO,
            normalized_number=reference.normalized_number,
            display_number=reference.display_number,
            title=basic_info.title,
            abstract=basic_info.abstract,
            ipc=basic_info.ipc,
            cpc=basic_info.cpc,
            applicants=basic_info.applicants,
            inventors=basic_info.inventors,
            representatives=basic_info.representatives,
            agents=basic_info.representatives,
            priority_data=refs.get("priority_data", []),
            publication_language=refs.get("publication_language") or None,
            filing_language=refs.get("filing_language") or None,
            designated_states=refs.get("designated_states")
            or PatentDesignatedStates(),
            language=refs.get("title_language") or refs.get("abstract_language"),
            first_priority_date=first_priority_date,
            filing_deadline_30_months=_add_months(first_priority_date, 30),
            filing_deadline_31_months=_add_months(first_priority_date, 31),
            application_date=application_reference.get("selected_date") or None,
            application_no=application_reference.get("selected_number") or None,
            publication_date=publication_reference.get("selected_date")
            or basic_info.publication_date
            or None,
            publication_no=_resolve_publication_number(
                lookup_reference, publication_reference
            ),
            abstract_words=count_words(basic_info.abstract),
            raw_source_refs=raw_source_refs,
        )

    async def _read_cache_fallback(
        self, reference: PatentReference
    ) -> tuple[str, PatentLookupApiResponse] | None:
        if not self._cache or not self._cache.configured:
            return None
        try:
            return await self._cache.find_lookup_fallback(reference)
        except PatentServiceError as exc:
            logger.warning(
                "quick lookup cache fallback unavailable normalized_number=%s code=%s",
                reference.normalized_number,
                exc.code,
            )
            return None

    async def _safe_record_lookup_event(
        self,
        *,
        trace_id: str,
        query: str,
        reference: PatentReference,
        outcome: str,
        started_at: float,
        patent_id: str | None = None,
        cache_status: str | None = None,
        error: PatentServiceError | None = None,
    ) -> None:
        if not self._cache or not self._cache.configured:
            return
        try:
            await self._cache.record_lookup_event(
                trace_id=trace_id,
                query=query,
                normalized_number=reference.normalized_number,
                source=reference.source.value,
                outcome=outcome,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                patent_id=patent_id,
                cache_status=cache_status,
                error_code=error.code.value if error else None,
                error_message=error.message if error else None,
            )
        except PatentServiceError as exc:
            logger.warning(
                "lookup event write failed trace_id=%s code=%s",
                trace_id,
                exc.code,
            )

    async def _lookup_ep(self, reference: PatentReference) -> PatentLookupEpResponse:
        lookup_reference, application_register_refs = (
            await self._resolve_ep_publication_reference(reference)
        )
        biblio_xml = await self._epo_ops_client.fetch_bibliographic_data(
            lookup_reference
        )
        basic_info, biblio_refs = self._epo_ops_client.parse_bibliographic_data(
            biblio_xml
        )

        (
            description_result,
            claims_result,
            images_result,
            family_result,
            register_result,
        ) = await asyncio.gather(
            self._fetch_optional_ep_xml(
                lookup_reference, self._epo_ops_client.fetch_description_data
            ),
            self._fetch_optional_ep_xml(
                lookup_reference, self._epo_ops_client.fetch_claims_data
            ),
            self._fetch_optional_ep_xml(
                lookup_reference, self._epo_ops_client.fetch_images_metadata
            ),
            self._fetch_optional_ep_xml(
                lookup_reference,
                self._epo_ops_client.fetch_family_bibliographic_data,
            ),
            self._fetch_optional_ep_xml(
                lookup_reference,
                self._epo_ops_client.fetch_register_bibliographic_data,
            ),
        )

        warnings: list[PatentLookupWarning] = []
        raw_source_refs: dict[str, Any] = {
            "ops_biblio": {
                "endpoint": self._epo_ops_client.build_biblio_path(lookup_reference),
                **biblio_refs,
            }
        }
        if application_register_refs:
            raw_source_refs["ops_application_register"] = application_register_refs
        drawings = PatentDrawingsInfo()
        description_words: int | None = None
        claims_count: int | None = None
        claims_words: int | None = None
        original_file_download_url: str | None = None
        total_pages: int | None = None
        international_filing_date: str | None = None
        register_refs: dict[str, Any] = {}
        family_refs: dict[str, Any] = {}

        if register_result["xml_text"] is not None:
            register_refs = self._epo_ops_client.parse_register_bibliographic_data(
                register_result["xml_text"]
            )
            raw_source_refs["ops_register"] = {
                "endpoint": self._epo_ops_client.build_register_biblio_path(
                    lookup_reference
                ),
                **register_refs,
            }

        if family_result["xml_text"] is not None:
            international_filing_date, family_refs = (
                self._epo_ops_client.parse_family_international_filing_date(
                    family_result["xml_text"]
                )
            )
            raw_source_refs["ops_family"] = {
                "endpoint": self._epo_ops_client.build_family_biblio_path(
                    lookup_reference
                ),
                **family_refs,
            }

        if description_result["xml_text"] is None:
            warnings.extend(
                [
                    _build_warning(
                        code="source_no_result",
                        field="description_words",
                        message="EPO OPS did not return a description constituent for this publication.",
                    ),
                    _build_warning(
                        code="source_no_result",
                        field="drawings",
                        message="Drawing labels could not be extracted because the EPO description constituent is unavailable.",
                    ),
                ]
            )
        else:
            description_content, description_refs = (
                self._epo_ops_client.parse_description_data(description_result["xml_text"])
            )
            description_words = count_words(description_content.text)
            drawings = drawings.model_copy(
                update={"drawing_labels": description_content.drawing_labels}
            )
            raw_source_refs["ops_description"] = {
                "endpoint": self._epo_ops_client.build_description_path(
                    lookup_reference
                ),
                **description_refs,
            }

        if claims_result["xml_text"] is None:
            warnings.extend(
                [
                    _build_warning(
                        code="source_no_result",
                        field="claims_count",
                        message="EPO OPS did not return a claims constituent for this publication.",
                    ),
                    _build_warning(
                        code="source_no_result",
                        field="claims_words",
                        message="EPO OPS did not return a claims constituent for this publication.",
                    ),
                ]
            )
        else:
            claims_content, claims_refs = self._epo_ops_client.parse_claims_data(
                claims_result["xml_text"]
            )
            claims_count = claims_content.claims_count
            claims_words = count_words(" ".join(claims_content.claim_texts))
            raw_source_refs["ops_claims"] = {
                "endpoint": self._epo_ops_client.build_claims_path(lookup_reference),
                **claims_refs,
            }

        publication_reference = biblio_refs.get("publication_reference", {})
        image_refs: dict[str, Any] = {}
        if images_result["xml_text"] is None:
            warnings.extend(
                [
                    _build_warning(
                        code="source_no_result",
                        field="drawings",
                        message="EPO OPS did not return image metadata for this publication.",
                    ),
                    _build_warning(
                        code="source_no_result",
                        field="original_file_download_url",
                        message="The original publication file download URL could not be resolved from EPO.",
                    ),
                ]
            )
        else:
            original_file, image_refs = self._epo_ops_client.parse_original_file_availability(
                images_result["xml_text"]
            )
            drawings = drawings.model_copy(
                update={
                    "has_drawings": bool(image_refs.get("has_drawings"))
                    or bool(drawings.drawing_labels),
                    "drawing_page_count": image_refs.get("drawing_page_count"),
                }
            )
            total_pages = image_refs.get("page_count")
            raw_source_refs["ops_images"] = {
                "endpoint": self._epo_ops_client.build_images_path(lookup_reference),
                **image_refs,
            }
            original_file_download_url = self._build_ep_download_url(
                reference=lookup_reference,
                original_file=original_file,
                publication_reference=image_refs.get("publication_reference", {}),
            )
            if original_file_download_url:
                raw_source_refs["epo_publication_server"] = {
                    "download_url": original_file_download_url
                }
            else:
                warnings.append(
                    _build_warning(
                        code="original_file_not_available",
                        field="original_file_download_url",
                        message="The original publication file is not available from EPO for this publication.",
                    )
                )

        if drawings.drawing_labels and not drawings.has_drawings:
            drawings = drawings.model_copy(update={"has_drawings": True})

        publication_reference = image_refs.get("publication_reference") or publication_reference
        application_reference = biblio_refs.get("application_reference", {})
        publication_no = _resolve_publication_number(
            lookup_reference, publication_reference
        )
        first_priority_date = biblio_refs.get("first_priority_date")
        deadline_base_date = first_priority_date or international_filing_date
        register_designated_states = register_refs.get("designated_states")
        if register_designated_states and not any(
            (
                register_designated_states.regions,
                register_designated_states.countries,
                register_designated_states.protection_types,
            )
        ):
            register_designated_states = None

        return PatentLookupEpResponse(
            source=PatentSource.EPO,
            normalized_number=reference.normalized_number,
            display_number=reference.display_number,
            title=basic_info.title,
            abstract=basic_info.abstract,
            ipc=basic_info.ipc,
            cpc=basic_info.cpc,
            applicants=basic_info.applicants,
            inventors=basic_info.inventors,
            representatives=register_refs.get("agents")
            or basic_info.representatives,
            agents=register_refs.get("agents") or basic_info.representatives,
            priority_data=register_refs.get("priority_data")
            or biblio_refs.get("priority_data", []),
            publication_language=register_refs.get("publication_language")
            or biblio_refs.get("publication_language")
            or None,
            filing_language=register_refs.get("filing_language")
            or biblio_refs.get("filing_language")
            or None,
            designated_states=register_designated_states
            or biblio_refs.get("designated_states")
            or PatentDesignatedStates(),
            related_patent_documents=_related_family_documents(
                family_refs, lookup_reference
            ),
            language=biblio_refs.get("title_language")
            or biblio_refs.get("abstract_language"),
            first_priority_date=first_priority_date,
            international_filing_date=international_filing_date,
            filing_deadline_30_months=_add_months(deadline_base_date, 30),
            filing_deadline_31_months=_add_months(deadline_base_date, 31),
            application_date=application_reference.get("selected_date") or None,
            application_no=application_reference.get("selected_number") or None,
            publication_date=publication_reference.get("selected_date")
            or basic_info.publication_date
            or None,
            publication_no=publication_no,
            abstract_words=count_words(basic_info.abstract),
            description_words=description_words,
            claims_count=claims_count,
            claims_words=claims_words,
            total_pages=total_pages,
            drawings=drawings,
            original_file_download_url=original_file_download_url,
            warnings=warnings,
            raw_source_refs=raw_source_refs,
        )

    async def _resolve_ep_publication_reference(
        self, reference: PatentReference
    ) -> tuple[PatentReference, dict[str, Any]]:
        if reference.reference_type != "application":
            return reference, {}

        register_xml = await self._epo_ops_client.fetch_register_bibliographic_data(
            reference
        )
        register_refs = self._epo_ops_client.parse_register_bibliographic_data(
            register_xml
        )
        publication = register_refs.get("publication_reference", {})
        country_code = publication.get("country") or "EP"
        doc_number = publication.get("doc_number")
        kind_code = publication.get("kind")
        if not doc_number:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_NO_RESULT,
                status_code=404,
                message=(
                    "No published EP document was found for this European "
                    "application number."
                ),
                source="epo",
                details={"application_number": reference.display_number},
            )

        normalized_number = f"{country_code}{doc_number}{kind_code or ''}"
        lookup_number = (
            f"{country_code}{doc_number}.{kind_code}"
            if kind_code
            else f"{country_code}{doc_number}"
        )
        publication_reference = PatentReference(
            source=PatentSource.EPO,
            normalized_number=normalized_number,
            display_number=normalized_number,
            country_code=country_code,
            doc_number=doc_number,
            kind_code=kind_code or None,
            lookup_number=lookup_number,
            reference_type="publication",
        )
        return publication_reference, {
            "endpoint": self._epo_ops_client.build_register_biblio_path(reference),
            **register_refs,
        }

    async def _fetch_optional_ep_xml(
        self,
        reference: PatentReference,
        fetcher: Callable[[PatentReference], Awaitable[str]],
    ) -> dict[str, str | None]:
        try:
            return {"xml_text": await fetcher(reference)}
        except PatentServiceError as exc:
            if exc.code == ErrorCode.SOURCE_NO_RESULT:
                return {"xml_text": None}
            raise

    def _build_ep_download_url(
        self,
        *,
        reference: PatentReference,
        original_file: PatentOriginalFile,
        publication_reference: dict[str, Any],
    ) -> str | None:
        if not original_file.available:
            return None

        country_code = publication_reference.get("country") or reference.country_code
        doc_number = publication_reference.get("doc_number") or reference.doc_number
        kind_code = publication_reference.get("kind") or reference.kind_code
        if not country_code or not doc_number or not kind_code:
            return None

        return self._epo_publication_server_client.build_pdf_download_url(
            country_code=country_code,
            doc_number=doc_number,
            kind_code=kind_code,
        )

    async def _lookup_wo(
        self,
        reference: PatentReference,
        *,
        include_original_file: bool,
        storage_dir: Path | None = None,
    ) -> PatentLookupResponse:
        mode = self._settings.wipo_lookup_mode
        logger.info(
            "wo lookup dispatch normalized_number=%s mode=%s include_original_file=%s rest_configured=%s soap_configured=%s",
            reference.normalized_number,
            mode,
            include_original_file,
            self._settings.wipo_rest_configured,
            self._settings.wipo_soap_configured,
        )
        if mode == "rest":
            response = await _lookup_wipo_client(
                self._wipo_rest_client,
                reference,
                include_original_file=include_original_file,
                storage_dir=storage_dir,
            )
            return await self._complete_wo_response(response, reference)
        if mode == "soap":
            response = await _lookup_wipo_client(
                self._wipo_soap_client,
                reference,
                include_original_file=include_original_file,
                storage_dir=storage_dir,
            )
            return await self._complete_wo_response(response, reference)

        if not self._settings.wipo_rest_configured:
            if self._settings.wipo_soap_configured:
                response = await _lookup_wipo_client(
                    self._wipo_soap_client,
                    reference,
                    include_original_file=include_original_file,
                    storage_dir=storage_dir,
                )
                return await self._complete_wo_response(response, reference)
            raise PatentServiceError(
                code=ErrorCode.SOURCE_ACCESS_NOT_CONFIGURED,
                status_code=503,
                message="WIPO PATENTSCOPE REST or SOAP credentials are not configured.",
                source="wipo",
            )

        try:
            response = await _lookup_wipo_client(
                self._wipo_rest_client,
                reference,
                include_original_file=include_original_file,
                storage_dir=storage_dir,
            )
        except PatentServiceError as exc:
            logger.warning(
                "wo rest lookup failed normalized_number=%s code=%s status=%s soap_configured=%s",
                reference.normalized_number,
                exc.code,
                exc.status_code,
                self._settings.wipo_soap_configured,
            )
            if self._settings.wipo_soap_configured and exc.code in {
                ErrorCode.SOURCE_RATE_LIMIT,
                ErrorCode.SOURCE_UNAVAILABLE,
                ErrorCode.UPSTREAM_RESPONSE_INVALID,
            }:
                logger.info(
                    "wo REST lookup falling back to SOAP normalized_number=%s",
                    reference.normalized_number,
                )
                response = await _lookup_wipo_client(
                    self._wipo_soap_client,
                    reference,
                    include_original_file=include_original_file,
                    storage_dir=storage_dir,
                )
                return await self._complete_wo_response(response, reference)
            raise
        return await self._complete_wo_response(response, reference)

    async def _complete_wo_response(
        self, response: PatentLookupResponse, reference: PatentReference
    ) -> PatentLookupResponse:
        finalized = self._finalize_wo_response(response)
        if not self._settings.epo_ops_configured:
            if finalized.basic_info.cpc:
                return finalized
            return self._with_cpc_warning(
                finalized, "EPO OPS credentials are not configured."
            )

        try:
            family_xml = await self._epo_ops_client.fetch_family_bibliographic_data(
                reference
            )
            _, family_refs = (
                self._epo_ops_client.parse_family_international_filing_date(
                    family_xml
                )
            )
            raw_refs = dict(finalized.raw_source_refs)
            raw_refs["ops_family"] = {
                "endpoint": self._epo_ops_client.build_family_biblio_path(reference),
                **family_refs,
            }
            finalized = finalized.model_copy(
                update={
                    "related_patent_documents": _related_family_documents(
                        family_refs, reference
                    ),
                    "raw_source_refs": raw_refs,
                }
            )
        except PatentServiceError as exc:
            logger.warning(
                "wo related patent enrichment failed normalized_number=%s code=%s",
                reference.normalized_number,
                exc.code,
            )

        if finalized.basic_info.cpc:
            return finalized
        try:
            epo_xml = await self._epo_ops_client.fetch_bibliographic_data(reference)
            epo_info, _ = self._epo_ops_client.parse_bibliographic_data(epo_xml)
        except PatentServiceError as exc:
            logger.warning(
                "wo CPC enrichment failed normalized_number=%s code=%s",
                reference.normalized_number,
                exc.code,
            )
            return self._with_cpc_warning(finalized, "EPO OPS did not provide CPC data.")
        if not epo_info.cpc:
            return self._with_cpc_warning(finalized, "EPO OPS did not provide CPC data.")
        raw_refs = dict(finalized.raw_source_refs)
        field_sources = dict(_as_dict(raw_refs.get("field_sources")))
        field_sources["cpc"] = "epo_ops"
        raw_refs["field_sources"] = field_sources
        return finalized.model_copy(
            update={
                "basic_info": finalized.basic_info.model_copy(
                    update={"cpc": list(dict.fromkeys(epo_info.cpc))}
                ),
                "raw_source_refs": raw_refs,
            }
        )

    @staticmethod
    def _with_cpc_warning(
        response: PatentLookupResponse, message: str
    ) -> PatentLookupResponse:
        warning = PatentLookupWarning(
            code="cpc_unavailable", field="cpc", message=message, source="wipo"
        )
        return response.model_copy(update={"warnings": [*response.warnings, warning]})

    def _finalize_wo_response(self, response: PatentLookupResponse) -> PatentLookupResponse:
        raw_refs = response.raw_source_refs
        application_ref = _as_dict(raw_refs.get("application_reference"))
        publication_ref = _as_dict(raw_refs.get("publication_reference"))

        return response.model_copy(
            update={
                "application_date": response.application_date
                or _first_non_empty_value(
                    raw_refs.get("application_filing_date"),
                    application_ref.get("selected_date"),
                    application_ref.get("date"),
                ),
                "application_no": response.application_no
                or _first_non_empty_value(
                    response.basic_info.application_number,
                    application_ref.get("selected_number"),
                    application_ref.get("pct_number"),
                    application_ref.get("full_number"),
                ),
                "publication_date": response.publication_date
                or _first_non_empty_value(
                    response.basic_info.publication_date,
                    publication_ref.get("selected_date"),
                    publication_ref.get("date"),
                ),
                "publication_no": response.publication_no
                or _first_non_empty_value(
                    raw_refs.get("publication_number"),
                    publication_ref.get("selected_number"),
                    publication_ref.get("full_number"),
                ),
                "abstract_words": response.abstract_words
                if response.abstract_words is not None
                else count_words(response.basic_info.abstract),
            }
        )


async def _lookup_wipo_client(
    client: WipoLookupClient,
    reference: PatentReference,
    *,
    include_original_file: bool,
    storage_dir: Path | None,
) -> PatentLookupResponse:
    if storage_dir is None:
        return await client.lookup_patent(
            reference, include_original_file=include_original_file
        )
    return await client.lookup_patent(
        reference,
        include_original_file=include_original_file,
        storage_dir=storage_dir,
    )


def _build_warning(*, code: str, field: str, message: str) -> PatentLookupWarning:
    return PatentLookupWarning(code=code, field=field, message=message, source="epo")


def _add_months(value: str | None, months: int) -> str | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None
    try:
        source = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None
    month_index = source.month - 1 + months
    year = source.year + month_index // 12
    month = month_index % 12 + 1
    day = min(source.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).strftime("%Y%m%d")


def _resolve_publication_number(
    reference: PatentReference, publication_reference: dict[str, Any]
) -> str | None:
    if reference.kind_code:
        return reference.normalized_number

    selected_number = publication_reference.get("selected_number")
    if selected_number:
        return str(selected_number)

    country_code = publication_reference.get("country") or reference.country_code
    doc_number = publication_reference.get("doc_number") or reference.doc_number
    kind_code = publication_reference.get("kind")
    if country_code and doc_number and kind_code:
        return f"{country_code}{doc_number}{kind_code}"
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _related_family_documents(
    family_refs: dict[str, Any], reference: PatentReference
) -> list[str]:
    current_numbers = {
        _compact_patent_number(reference.normalized_number),
        _compact_patent_number(reference.display_number),
        _compact_patent_number(f"{reference.country_code}{reference.doc_number}"),
    }
    related: list[str] = []
    for publication in family_refs.get("family_publications", []):
        if not isinstance(publication, dict):
            continue
        number = str(publication.get("number") or "")
        if not number or _compact_patent_number(number) in current_numbers:
            continue
        if number not in related:
            related.append(number)
    return related


def _compact_patent_number(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _first_non_empty_value(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
            continue
        return str(value)
    return None
