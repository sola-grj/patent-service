import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.clients.epo_ops import EpoClaimsContent, EpoDescriptionContent, EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentDrawingsInfo,
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
        self, reference: PatentReference, *, include_original_file: bool
    ) -> PatentLookupResponse: ...


logger = logging.getLogger("patent_service")


class PatentLookupService:
    def __init__(
        self,
        *,
        settings: Settings,
        epo_ops_client: EpoOpsClient,
        epo_publication_server_client: EpoPublicationServerClient,
        wipo_client: WipoLookupClient,
        wipo_public_client: WipoLookupClient,
    ) -> None:
        self._settings = settings
        self._epo_ops_client = epo_ops_client
        self._epo_publication_server_client = epo_publication_server_client
        self._wipo_client = wipo_client
        self._wipo_public_client = wipo_public_client

    async def lookup_patent(
        self, request: PatentLookupRequest
    ) -> PatentLookupApiResponse:
        reference = normalize_patent_number(request.patent_number)
        logger.info(
            "lookup normalized patent_number=%s source=%s normalized_number=%s include_original_file=%s",
            request.patent_number,
            reference.source,
            reference.normalized_number,
            request.include_original_file,
        )
        if reference.source is PatentSource.EPO:
            return await self._lookup_ep(reference)
        return await self._lookup_wo(
            reference, include_original_file=request.include_original_file
        )

    async def _lookup_ep(self, reference: PatentReference) -> PatentLookupEpResponse:
        biblio_xml = await self._epo_ops_client.fetch_bibliographic_data(reference)
        basic_info, biblio_refs = self._epo_ops_client.parse_bibliographic_data(
            biblio_xml
        )

        description_result, claims_result, images_result = await asyncio.gather(
            self._fetch_optional_ep_xml(
                reference, self._epo_ops_client.fetch_description_data
            ),
            self._fetch_optional_ep_xml(reference, self._epo_ops_client.fetch_claims_data),
            self._fetch_optional_ep_xml(
                reference, self._epo_ops_client.fetch_images_metadata
            ),
        )

        warnings: list[PatentLookupWarning] = []
        raw_source_refs: dict[str, Any] = {
            "ops_biblio": {
                "endpoint": self._epo_ops_client.build_biblio_path(reference),
                **biblio_refs,
            }
        }
        drawings = PatentDrawingsInfo()
        description_words: int | None = None
        claims_count: int | None = None
        claims_words: int | None = None
        original_file_download_url: str | None = None

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
                "endpoint": self._epo_ops_client.build_description_path(reference),
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
                "endpoint": self._epo_ops_client.build_claims_path(reference),
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
            raw_source_refs["ops_images"] = {
                "endpoint": self._epo_ops_client.build_images_path(reference),
                **image_refs,
            }
            original_file_download_url = self._build_ep_download_url(
                reference=reference,
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
        publication_no = _resolve_publication_number(reference, publication_reference)

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
            drawings=drawings,
            original_file_download_url=original_file_download_url,
            warnings=warnings,
            raw_source_refs=raw_source_refs,
        )

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
    ) -> PatentLookupResponse:
        mode = self._settings.wipo_lookup_mode
        logger.info(
            "wo lookup dispatch normalized_number=%s mode=%s include_original_file=%s soap_configured=%s",
            reference.normalized_number,
            mode,
            include_original_file,
            self._settings.wipo_patentscope_configured,
        )
        if mode == "soap":
            response = await self._wipo_client.lookup_patent(
                reference, include_original_file=include_original_file
            )
            return self._finalize_wo_response(response)
        if mode == "public_page":
            response = await self._wipo_public_client.lookup_patent(
                reference, include_original_file=include_original_file
            )
            return self._finalize_wo_response(response)

        try:
            response = await self._wipo_public_client.lookup_patent(
                reference, include_original_file=False
            )
        except PatentServiceError as exc:
            logger.warning(
                "wo public lookup failed normalized_number=%s code=%s status=%s soap_configured=%s",
                reference.normalized_number,
                exc.code,
                exc.status_code,
                self._settings.wipo_patentscope_configured,
            )
            if self._settings.wipo_patentscope_configured and exc.code in {
                ErrorCode.SOURCE_RATE_LIMIT,
                ErrorCode.SOURCE_UNAVAILABLE,
                ErrorCode.UPSTREAM_RESPONSE_INVALID,
            }:
                logger.info(
                    "wo lookup falling back to soap normalized_number=%s",
                    reference.normalized_number,
                )
                response = await self._wipo_client.lookup_patent(
                    reference, include_original_file=include_original_file
                )
                return self._finalize_wo_response(response)
            raise

        logger.info(
            "wo public lookup finished normalized_number=%s original_file_available=%s include_original_file=%s",
            reference.normalized_number,
            response.original_file.available,
            include_original_file,
        )
        if not include_original_file or response.original_file.available:
            return self._finalize_wo_response(response)

        if not self._settings.wipo_patentscope_configured:
            logger.info(
                "wo original file unavailable without soap fallback normalized_number=%s",
                reference.normalized_number,
            )
            return self._finalize_wo_response(response)

        try:
            logger.info(
                "wo original file retrying through soap normalized_number=%s",
                reference.normalized_number,
            )
            soap_response = await self._wipo_client.lookup_patent(
                reference, include_original_file=True
            )
        except PatentServiceError:
            logger.warning(
                "wo soap original file lookup failed normalized_number=%s",
                reference.normalized_number,
            )
            return self._finalize_wo_response(response)

        if not soap_response.original_file.available:
            return self._finalize_wo_response(response)

        merged_refs = dict(response.raw_source_refs)
        merged_refs["soap_original_file"] = soap_response.raw_source_refs
        merged_response = response.model_copy(
            update={
                "original_file": soap_response.original_file,
                "raw_source_refs": merged_refs,
            }
        )
        return self._finalize_wo_response(merged_response)

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


def _build_warning(*, code: str, field: str, message: str) -> PatentLookupWarning:
    return PatentLookupWarning(code=code, field=field, message=message, source="epo")


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
