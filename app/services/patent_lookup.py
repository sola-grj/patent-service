import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.clients.epo_ops import EpoClaimsContent, EpoDescriptionContent, EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
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


class PatentLookupService:
    def __init__(
        self,
        *,
        epo_ops_client: EpoOpsClient,
        epo_publication_server_client: EpoPublicationServerClient,
        wipo_client: WipoPatentScopeClient,
    ) -> None:
        self._epo_ops_client = epo_ops_client
        self._epo_publication_server_client = epo_publication_server_client
        self._wipo_client = wipo_client

    async def lookup_patent(
        self, request: PatentLookupRequest
    ) -> PatentLookupApiResponse:
        reference = normalize_patent_number(request.patent_number)
        if reference.source is PatentSource.EPO:
            return await self._lookup_ep(reference)
        return await self._wipo_client.lookup_patent(
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
