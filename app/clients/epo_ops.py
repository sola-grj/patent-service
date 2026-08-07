import asyncio
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import fitz
import httpx

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentDesignatedStates,
    PatentOriginalFile,
    PatentPriorityData,
    PatentReference,
    PatentRepresentative,
)
from app.utils.text_metrics import extract_drawing_labels, normalize_text

_CLAIM_NUMBER_PATTERN = re.compile(r"^\d+\.")


@dataclass(slots=True)
class EpoDescriptionContent:
    text: str
    language: str | None
    paragraphs: list[str]
    drawing_labels: list[str]


@dataclass(slots=True)
class EpoClaimsContent:
    language: str | None
    claim_texts: list[str]
    claims_count: int


class EpoOpsClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._access_token_lock = asyncio.Lock()
        self._http_client = httpx.AsyncClient(
            base_url=self._settings.epo_ops_base_url,
            follow_redirects=True,
            timeout=self._settings.request_timeout_seconds,
        )

    async def warmup(self) -> None:
        await self._get_access_token()

    async def fetch_bibliographic_data(self, reference: PatentReference) -> str:
        return await self._get_xml(
            path=self.build_biblio_path(reference),
            accept="application/exchange+xml",
            source="epo",
        )

    async def fetch_resolved_bibliographic_data(
        self, reference: PatentReference
    ) -> tuple[PatentReference, str, dict[str, Any]]:
        attempted: list[str] = []
        last_error: PatentServiceError | None = None
        for path in self.build_biblio_candidate_paths(reference):
            attempted.append(path)
            try:
                xml_text = await self._get_xml(
                    path=path,
                    accept="application/exchange+xml",
                    source="epo",
                )
            except PatentServiceError as exc:
                if exc.code is ErrorCode.SOURCE_NO_RESULT:
                    last_error = exc
                    continue
                raise
            _, refs = self.parse_bibliographic_data(xml_text)
            resolved = self.reference_from_bibliographic_data(reference, refs)
            return resolved, xml_text, {
                "endpoint": path,
                "attempted_endpoints": attempted,
                "input_number": reference.display_number,
            }
        raise PatentServiceError(
            code=ErrorCode.SOURCE_NO_RESULT,
            status_code=404,
            message="No publication was found in EPO OPS.",
            source="epo",
            details={
                "input_number": reference.display_number,
                "attempted_endpoints": attempted,
                "last_error": last_error.message if last_error else "",
            },
        )

    async def fetch_register_bibliographic_data(
        self, reference: PatentReference
    ) -> str:
        return await self._get_xml(
            path=self.build_register_biblio_path(reference),
            accept="application/register+xml",
            source="epo",
        )

    async def fetch_description_data(self, reference: PatentReference) -> str:
        return await self._get_xml(
            path=self.build_description_path(reference),
            accept="application/exchange+xml",
            source="epo",
        )

    async def fetch_claims_data(self, reference: PatentReference) -> str:
        return await self._get_xml(
            path=self.build_claims_path(reference),
            accept="application/exchange+xml",
            source="epo",
        )

    async def fetch_images_metadata(self, reference: PatentReference) -> str:
        return await self._get_xml(
            path=self.build_images_path(reference),
            accept="application/ops+xml",
            source="epo",
        )

    async def fetch_family_bibliographic_data(self, reference: PatentReference) -> str:
        return await self._get_xml(
            path=self.build_family_biblio_path(reference),
            accept="application/ops+xml",
            source="epo",
        )

    def build_biblio_path(self, reference: PatentReference) -> str:
        return f"/published-data/publication/epodoc/{reference.lookup_number}/biblio"

    def build_biblio_candidate_paths(self, reference: PatentReference) -> list[str]:
        candidates = [self.build_biblio_path(reference)]
        if reference.kind_code:
            candidates.append(
                "/published-data/publication/docdb/"
                f"{reference.country_code}.{reference.doc_number}."
                f"{reference.kind_code}/biblio"
            )
        for number in (
            reference.normalized_number,
            f"{reference.country_code}{reference.doc_number}",
        ):
            query = quote(f"pn={number}", safe="")
            candidates.append(f"/published-data/search/biblio?q={query}")
        return list(dict.fromkeys(candidates))

    @staticmethod
    def reference_from_bibliographic_data(
        reference: PatentReference, refs: dict[str, Any]
    ) -> PatentReference:
        publication = refs.get("publication_reference", {})
        epodoc = publication.get("ids", {}).get("epodoc", {})
        country_code = str(
            epodoc.get("country")
            or publication.get("country")
            or reference.country_code
        )
        doc_number = str(
            epodoc.get("doc_number")
            or publication.get("doc_number")
            or reference.doc_number
        )
        if doc_number.startswith(country_code):
            doc_number = doc_number[len(country_code):]
        kind_code = str(
            epodoc.get("kind")
            or publication.get("kind")
            or reference.kind_code
            or ""
        ) or None
        normalized_number = f"{country_code}{doc_number}{kind_code or ''}"
        lookup_number = (
            f"{country_code}{doc_number}.{kind_code}"
            if country_code == "EP" and kind_code
            else f"{country_code}{doc_number}"
        )
        return reference.model_copy(
            update={
                "normalized_number": normalized_number,
                "country_code": country_code,
                "doc_number": doc_number,
                "kind_code": kind_code,
                "lookup_number": lookup_number,
                "reference_type": "publication",
            }
        )

    @staticmethod
    def build_register_biblio_path(reference: PatentReference) -> str:
        return (
            f"/register/{reference.reference_type}/epodoc/"
            f"{reference.country_code}{reference.doc_number}/biblio"
        )

    def build_description_path(self, reference: PatentReference) -> str:
        return f"/published-data/publication/epodoc/{reference.lookup_number}/description"

    def build_claims_path(self, reference: PatentReference) -> str:
        return f"/published-data/publication/epodoc/{reference.lookup_number}/claims"

    def build_images_path(self, reference: PatentReference) -> str:
        return f"/published-data/publication/epodoc/{reference.lookup_number}/images"

    def build_family_biblio_path(self, reference: PatentReference) -> str:
        kind_code = reference.kind_code or ""
        return (
            f"/family/publication/docdb/{reference.country_code}."
            f"{reference.doc_number}.{kind_code}/biblio"
        )

    async def _get_xml(self, *, path: str, accept: str, source: str) -> str:
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": accept}
        response = await self._http_client.get(path, headers=headers)
        self._raise_for_error(response, source=source)
        return response.text

    async def download_full_document(
        self,
        reference: PatentReference,
        *,
        images_xml: str,
        storage_dir: Path,
        max_pages: int,
        max_bytes: int,
        concurrency: int = 3,
    ) -> tuple[PatentOriginalFile, dict[str, Any]]:
        file_info, refs = self.parse_original_file_availability(images_xml)
        page_count = int(refs.get("page_count") or 0)
        link = str(refs.get("document_instance_link") or "").lstrip("/")
        full_document = next(
            (
                item
                for item in refs.get("available_document_instances", [])
                if item.get("desc") == "FullDocument"
                and item.get("link") == refs.get("document_instance_link")
            ),
            None,
        )
        formats = list((full_document or {}).get("formats") or [])
        content_type = (
            "application/pdf"
            if "application/pdf" in formats
            else "image/tiff"
            if "image/tiff" in formats
            else ""
        )
        if not file_info.available or not link or not page_count or not content_type:
            raise PatentServiceError(
                code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                status_code=404,
                message="EPO OPS did not expose a complete publication document.",
                source="epo",
                details={"normalized_number": reference.normalized_number},
            )
        if page_count > max_pages:
            raise PatentServiceError(
                code=ErrorCode.UPLOAD_TOO_LARGE,
                status_code=422,
                message="The EPO publication exceeds the configured page limit.",
                source="epo",
                details={"page_count": page_count, "max_pages": max_pages},
            )

        semaphore = asyncio.Semaphore(max(1, concurrency))
        byte_lock = asyncio.Lock()
        downloaded_bytes = 0

        async def fetch_page(page_number: int) -> bytes:
            nonlocal downloaded_bytes
            async with semaphore:
                payload = await self._get_document_page(
                    f"/{link}", page_number=page_number, accept=content_type
                )
                async with byte_lock:
                    downloaded_bytes += len(payload)
                    if downloaded_bytes > max_bytes:
                        raise PatentServiceError(
                            code=ErrorCode.UPLOAD_TOO_LARGE,
                            status_code=422,
                            message=(
                                "The EPO publication exceeds the configured "
                                "file-size limit."
                            ),
                            source="epo",
                            details={
                                "size": downloaded_bytes,
                                "max_bytes": max_bytes,
                            },
                        )
                return payload

        tasks = [
            asyncio.create_task(fetch_page(page_number))
            for page_number in range(1, page_count + 1)
        ]
        try:
            pages = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        storage_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{reference.normalized_number}.pdf"
        output_path = storage_dir / filename
        await asyncio.to_thread(
            _merge_ops_document_pages, pages, content_type, output_path
        )
        if output_path.stat().st_size > max_bytes:
            output_path.unlink(missing_ok=True)
            raise PatentServiceError(
                code=ErrorCode.UPLOAD_TOO_LARGE,
                status_code=422,
                message="The merged EPO publication exceeds the file-size limit.",
                source="epo",
            )
        return PatentOriginalFile(
            available=True,
            content_type="application/pdf",
            filename=filename,
            storage_path=str(output_path),
        ), {
            **refs,
            "page_content_type": content_type,
            "downloaded_page_count": len(pages),
            "downloaded_bytes": downloaded_bytes,
            "storage_path": str(output_path),
        }

    async def _get_document_page(
        self, path: str, *, page_number: int, accept: str
    ) -> bytes:
        for attempt in range(3):
            token = await self._get_access_token()
            try:
                response = await self._http_client.get(
                    path,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": accept,
                        "X-OPS-Range": str(page_number),
                    },
                )
            except httpx.RequestError as exc:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise PatentServiceError(
                    code=ErrorCode.SOURCE_UNAVAILABLE,
                    status_code=503,
                    message="EPO OPS page download failed.",
                    source="epo",
                    details={"page_number": page_number, "error": str(exc)},
                ) from exc
            if response.status_code == 429 and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * (2**attempt)
                await asyncio.sleep(delay)
                continue
            self._raise_for_error(response, source="epo")
            return response.content
        raise AssertionError("unreachable")

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._access_token_expires_at:
            return self._access_token
        async with self._access_token_lock:
            if self._access_token and time.time() < self._access_token_expires_at:
                return self._access_token

            if not self._settings.epo_ops_configured:
                raise PatentServiceError(
                    code=ErrorCode.SOURCE_ACCESS_NOT_CONFIGURED,
                    status_code=503,
                    message="EPO OPS credentials are not configured.",
                    source="epo",
                    details={
                        "required_env": [
                            "PATENT_SERVICE_EPO_OPS_CONSUMER_KEY",
                            "PATENT_SERVICE_EPO_OPS_CONSUMER_SECRET",
                        ]
                    },
                )

            response = await self._http_client.post(
                self._settings.epo_ops_token_url,
                data={"grant_type": "client_credentials"},
                auth=(
                    self._settings.epo_ops_consumer_key,
                    self._settings.epo_ops_consumer_secret,
                ),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

            if response.status_code in {401, 403}:
                raise PatentServiceError(
                    code=ErrorCode.SOURCE_AUTH_REQUIRED,
                    status_code=503,
                    message="EPO OPS authentication failed.",
                    source="epo",
                )
            if response.is_error:
                raise PatentServiceError(
                    code=ErrorCode.SOURCE_UNAVAILABLE,
                    status_code=503,
                    message="EPO OPS token request failed.",
                    source="epo",
                    details={"status_code": response.status_code},
                )

            payload = response.json()
            access_token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not access_token or not expires_in:
                raise PatentServiceError(
                    code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                    status_code=502,
                    message="EPO OPS token response is missing required fields.",
                    source="epo",
                )

            self._access_token = str(access_token)
            self._access_token_expires_at = time.time() + max(
                int(expires_in) - 30, 30
            )
            return self._access_token

    def _raise_for_error(self, response: httpx.Response, *, source: str) -> None:
        if response.status_code == 404:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_NO_RESULT,
                status_code=404,
                message="No publication was found in the upstream source.",
                source=source,
            )

        if response.status_code == 429 or "Fair Use policy" in response.text:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_RATE_LIMIT,
                status_code=503,
                message="Upstream source rate limit was reached.",
                source=source,
            )

        if response.status_code in {401, 403}:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_AUTH_REQUIRED,
                status_code=503,
                message="Upstream source rejected the request.",
                source=source,
                details={"status_code": response.status_code},
            )

        if response.is_error:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="Upstream source request failed.",
                source=source,
                details={"status_code": response.status_code},
            )

    @staticmethod
    def parse_bibliographic_data(
        xml_text: str,
    ) -> tuple[PatentBasicInfo, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        exchange_document = _first_local(root.iter(), "exchange-document")
        if exchange_document is None:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO bibliographic response did not contain an exchange document.",
                source="epo",
            )

        publication_doc = _collect_document_references(
            exchange_document, "publication-reference", reference_kind="publication"
        )
        application_doc = _collect_document_references(
            exchange_document, "application-reference", reference_kind="application"
        )
        publication_language = _epo_language(
            exchange_document, "language-of-publication", ""
        )
        title_node, title_language, _ = _select_language_node(
            _all_local(exchange_document.iter(), "invention-title"),
            preferred=publication_language,
        )
        abstract_node, abstract_language, _ = _select_language_node(
            _all_local(exchange_document.iter(), "abstract"),
            preferred=publication_language,
        )

        basic_info = PatentBasicInfo(
            title=_joined_text(title_node),
            abstract=_joined_text(abstract_node),
            publication_date=publication_doc.get("selected_date", ""),
            application_number=application_doc.get("selected_number", ""),
            applicants=_party_names(exchange_document, "applicants", "applicant"),
            inventors=_party_names(exchange_document, "inventors", "inventor"),
            representatives=_epo_representatives(exchange_document),
            ipc=_classification_values(exchange_document, "classification-ipcr"),
            cpc=_classification_values(exchange_document, "patent-classification"),
        )
        raw_refs = {
            "publication_reference": publication_doc,
            "application_reference": application_doc,
            "title_language": title_language,
            "abstract_language": abstract_language,
            "first_priority_date": _first_priority_date(exchange_document),
            "priority_data": _epo_priority_data(exchange_document),
            "publication_language": publication_language or title_language,
            "filing_language": _epo_language(
                exchange_document, "language-of-filing", ""
            ),
            "designated_states": _epo_designated_states(exchange_document),
        }
        return basic_info, raw_refs

    @staticmethod
    def parse_register_bibliographic_data(xml_text: str) -> dict[str, Any]:
        root = _parse_xml(xml_text, source="epo")
        register_document = _first_local(root.iter(), "register-document")
        if register_document is None:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO Register response did not contain a register document.",
                source="epo",
            )
        bibliographic = _first_local(register_document.iter(), "bibliographic-data")
        if bibliographic is None:
            bibliographic = register_document
        publication_reference = _first_local(
            bibliographic.iter(), "publication-reference"
        )
        publication_language = ""
        if publication_reference is not None:
            document_id = _first_local(publication_reference.iter(), "document-id")
            if document_id is not None:
                publication_language = (document_id.get("lang") or "").upper()
        return {
            "publication_reference": _collect_document_references(
                bibliographic,
                "publication-reference",
                reference_kind="publication",
            ),
            "application_reference": _collect_document_references(
                bibliographic,
                "application-reference",
                reference_kind="application",
            ),
            "agents": _epo_representatives(bibliographic),
            "priority_data": _epo_priority_data(bibliographic),
            "publication_language": publication_language
            or (bibliographic.get("lang") or "").upper(),
            "filing_language": _epo_language(
                bibliographic, "language-of-filing", ""
            ),
            "designated_states": _epo_designated_states(bibliographic),
        }

    @staticmethod
    def parse_family_international_filing_date(
        xml_text: str,
    ) -> tuple[str | None, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        wo_members: list[dict[str, str]] = []
        family_publications: list[dict[str, str]] = []
        for family_member in _all_local(root.iter(), "family-member"):
            publication = _collect_document_references(
                family_member, "publication-reference", reference_kind="publication"
            )
            publication_ids = publication.get("ids", {})
            epodoc_number = publication_ids.get("epodoc", {}).get("doc_number", "")
            display_number = epodoc_number or (
                f"{publication.get('country', '')}{publication.get('doc_number', '')}"
            )
            if display_number:
                family_publication = {
                    "number": display_number,
                    "country": publication.get("country", ""),
                    "doc_number": publication.get("doc_number", ""),
                    "kind": publication.get("kind", ""),
                    "date": publication.get("selected_date", ""),
                }
                if family_publication not in family_publications:
                    family_publications.append(family_publication)
            if publication.get("country") != "WO":
                continue
            application = _collect_document_references(
                family_member, "application-reference", reference_kind="application"
            )
            filing_date = application.get("selected_date", "")
            if filing_date:
                wo_members.append(
                    {
                        "publication_number": publication.get("selected_number", ""),
                        "application_number": application.get("selected_number", ""),
                        "filing_date": filing_date,
                    }
                )

        filing_dates = sorted(
            {member["filing_date"] for member in wo_members if member["filing_date"]}
        )
        return (filing_dates[0] if filing_dates else None), {
            "wo_members": wo_members,
            "family_publications": family_publications,
        }

    @staticmethod
    def parse_description_data(
        xml_text: str,
        *,
        preferred_language: str | None = None,
    ) -> tuple[EpoDescriptionContent, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        description_node, language, available_languages = _select_language_node(
            _all_local(root.iter(), "description"), preferred=preferred_language
        )
        if description_node is None:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO description response did not contain a description node.",
                source="epo",
            )

        paragraphs = _node_paragraphs(description_node)
        content = EpoDescriptionContent(
            text=" ".join(paragraphs),
            language=language,
            paragraphs=paragraphs,
            drawing_labels=extract_drawing_labels(paragraphs),
        )
        raw_refs = {
            "selected_language": language,
            "available_languages": available_languages,
            "paragraph_count": len(paragraphs),
        }
        return content, raw_refs

    @staticmethod
    def parse_claims_data(
        xml_text: str,
        *,
        preferred_language: str | None = None,
    ) -> tuple[EpoClaimsContent, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        claims_node, language, available_languages = _select_language_node(
            _all_local(root.iter(), "claims"), preferred=preferred_language
        )
        if claims_node is None:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO claims response did not contain a claims node.",
                source="epo",
            )

        claim_texts = [
            text
            for text in (_clean_node_text(node) for node in _all_local(claims_node.iter(), "claim-text"))
            if text
        ]
        if not claim_texts:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO claims response did not contain claim text.",
                source="epo",
            )

        numbered_claims = sum(
            1 for claim_text in claim_texts if _CLAIM_NUMBER_PATTERN.match(claim_text)
        )
        content = EpoClaimsContent(
            language=language,
            claim_texts=claim_texts,
            claims_count=numbered_claims or len(claim_texts),
        )
        raw_refs = {
            "selected_language": language,
            "available_languages": available_languages,
            "claim_text_count": len(claim_texts),
        }
        return content, raw_refs

    @staticmethod
    def parse_original_file_availability(
        xml_text: str,
    ) -> tuple[PatentOriginalFile, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        inquiry_result = _first_local(root.iter(), "inquiry-result")
        if inquiry_result is None:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO images response did not contain inquiry-result.",
                source="epo",
            )

        publication_doc = _collect_document_references(
            inquiry_result, "publication-reference", reference_kind="publication"
        )
        drawing_page_count: int | None = None
        has_drawings = False
        full_document: ET.Element | None = None
        document_refs: list[dict[str, Any]] = []

        for document_instance in _all_local(inquiry_result.iter(), "document-instance"):
            desc = document_instance.attrib.get("desc", "")
            formats = _texts_for_local(document_instance, "document-format")
            page_count = _safe_int(document_instance.attrib.get("number-of-pages"))
            document_refs.append(
                {
                    "desc": desc,
                    "link": document_instance.attrib.get("link", ""),
                    "page_count": page_count,
                    "formats": formats,
                }
            )
            if desc.lower() == "drawing":
                has_drawings = True
                drawing_page_count = page_count
            if desc == "FullDocument" and any(
                item in formats for item in ("application/pdf", "image/tiff")
            ):
                full_document = document_instance

        raw_refs: dict[str, Any] = {
            "publication_reference": publication_doc,
            "available_document_instances": document_refs,
            "has_drawings": has_drawings,
            "drawing_page_count": drawing_page_count,
        }
        if full_document is None:
            return PatentOriginalFile(), raw_refs

        doc_number = publication_doc.get("doc_number", "")
        kind_code = publication_doc.get("kind", "")
        country_code = publication_doc.get("country", "EP")
        filename = f"{country_code}{doc_number}{kind_code}.pdf"
        file_info = PatentOriginalFile(
            available=True,
            content_type=(
                "application/pdf"
                if "application/pdf" in _texts_for_local(
                    full_document, "document-format"
                )
                else "image/tiff"
            ),
            filename=filename,
        )
        raw_refs.update(
            {
                "document_instance_link": full_document.attrib.get("link", ""),
                "page_count": _safe_int(full_document.attrib.get("number-of-pages")),
            }
        )
        return file_info, raw_refs


def _parse_xml(xml_text: str, *, source: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="Upstream XML could not be parsed.",
            source=source,
        ) from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _merge_ops_document_pages(
    pages: list[bytes], content_type: str, output_path: Path
) -> None:
    merged = fitz.open()
    try:
        for payload in pages:
            if content_type == "application/pdf":
                source = fitz.open(stream=payload, filetype="pdf")
            else:
                image = fitz.open(stream=payload, filetype="tiff")
                try:
                    source = fitz.open("pdf", image.convert_to_pdf())
                finally:
                    image.close()
            try:
                merged.insert_pdf(source)
            finally:
                source.close()
        if merged.page_count != len(pages):
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="EPO OPS returned an unexpected number of document pages.",
                source="epo",
                details={
                    "expected_pages": len(pages),
                    "merged_pages": merged.page_count,
                },
            )
        merged.save(output_path)
    except (fitz.FileDataError, RuntimeError, ValueError) as exc:
        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="EPO OPS returned an invalid publication page.",
            source="epo",
            details={"error": str(exc)},
        ) from exc
    finally:
        merged.close()


def _first_local(nodes: Iterable[ET.Element], local_name: str) -> ET.Element | None:
    for node in nodes:
        if _local_name(node.tag) == local_name:
            return node
    return None


def _all_local(nodes: Iterable[ET.Element], local_name: str) -> list[ET.Element]:
    return [node for node in nodes if _local_name(node.tag) == local_name]


def _first_text(root: ET.Element, local_name: str) -> str:
    node = _first_local(root.iter(), local_name)
    return _joined_text(node)


def _joined_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    parts = [part.strip() for part in node.itertext() if part and part.strip()]
    return " ".join(parts)


def _clean_node_text(node: ET.Element | None) -> str:
    return normalize_text(_joined_text(node))


def _node_paragraphs(node: ET.Element) -> list[str]:
    paragraphs = [_clean_node_text(paragraph) for paragraph in _all_local(node.iter(), "p")]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if paragraphs:
        return paragraphs
    text = _clean_node_text(node)
    return [text] if text else []


def _unique_texts(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _classification_values(root: ET.Element, item_name: str) -> list[str]:
    values: list[str] = []
    for item in _all_local(root.iter(), item_name):
        value = _first_text(item, "text")
        if not value:
            value = _joined_text(item)
        value = " ".join(value.split())
        if value:
            values.append(value)
    return _unique_texts(values)


def _party_names(root: ET.Element, group_name: str, item_name: str) -> list[str]:
    group = _first_local(root.iter(), group_name)
    if group is None:
        return []

    values: list[str] = []
    for item in _all_local(group.iter(), item_name):
        value = _first_text(item, "name")
        if not value:
            value = _joined_text(item)
        if value:
            values.append(value)
    return _unique_texts(values)


def _epo_representatives(root: ET.Element) -> list[PatentRepresentative]:
    representatives: list[PatentRepresentative] = []
    for group_name, item_name in (("agents", "agent"), ("representatives", "representative")):
        group = _first_local(root.iter(), group_name)
        if group is None:
            continue
        for item in _all_local(group.iter(), item_name):
            name = _first_text(item, "name")
            organization = _first_text(item, "orgname")
            address_node = _first_local(item.iter(), "address")
            address_parts: list[str] = []
            country = ""
            if address_node is not None:
                for child in address_node:
                    value = _joined_text(child)
                    if _local_name(child.tag) == "country":
                        country = value
                    elif value:
                        address_parts.append(value)
            representative = PatentRepresentative(
                name=name,
                organization=organization,
                address=normalize_text(" ".join(address_parts)),
                country=country,
            )
            if any(representative.model_dump().values()) and representative not in representatives:
                representatives.append(representative)
        break
    return representatives


def _epo_priority_data(root: ET.Element) -> list[PatentPriorityData]:
    priorities: list[PatentPriorityData] = []
    for claim in _all_local(root.iter(), "priority-claim"):
        document_id = _first_local(claim.iter(), "document-id")
        source = document_id if document_id is not None else claim
        priority = PatentPriorityData(
            number=_first_text(source, "doc-number"),
            date=_first_text(source, "date"),
            country=_first_text(source, "country"),
            kind=claim.attrib.get("kind", "") or _first_text(source, "kind"),
        )
        if any(priority.model_dump().values()) and priority not in priorities:
            priorities.append(priority)
    return priorities


def _epo_language(root: ET.Element, element_name: str, fallback: str) -> str:
    value = _first_text(root, element_name)
    if value:
        return value.upper()
    return (fallback or "").upper()


def _epo_designated_states(root: ET.Element) -> PatentDesignatedStates:
    designation = _first_local(root.iter(), "designation-of-states")
    if designation is None:
        return PatentDesignatedStates()
    regions: list[str] = []
    countries: list[str] = []
    protection_types: list[str] = []
    for region in _all_local(designation.iter(), "region"):
        code = _first_text(region, "country")
        if code and code not in regions:
            regions.append(code)
    for country_node in _all_local(designation.iter(), "country"):
        code = _joined_text(country_node)
        if code and code not in regions and code not in countries:
            countries.append(code)
    for protection in _all_local(designation.iter(), "kind-of-protection"):
        value = _joined_text(protection)
        if value and value not in protection_types:
            protection_types.append(value)
    return PatentDesignatedStates(
        regions=regions,
        countries=countries,
        protection_types=protection_types,
    )


def _first_priority_date(root: ET.Element) -> str | None:
    dates: list[str] = []
    for claim in _all_local(root.iter(), "priority-claim"):
        active = _first_text(claim, "priority-active-indicator").upper()
        if active in {"NO", "N", "FALSE", "0"}:
            continue
        for document_id in _all_local(claim.iter(), "document-id"):
            value = _first_text(document_id, "date")
            if re.fullmatch(r"\d{8}", value):
                dates.append(value)
    return min(dates) if dates else None


def _collect_document_references(
    root: ET.Element, container_name: str, *, reference_kind: str
) -> dict[str, Any]:
    container = _first_local(root.iter(), container_name)
    if container is None:
        return {}

    references: dict[str, dict[str, str]] = {}
    for index, candidate in enumerate(_all_local(container.iter(), "document-id")):
        key = candidate.attrib.get("document-id-type") or f"unknown_{index}"
        references[key] = _document_id_to_dict(candidate)

    primary = (
        references.get("docdb")
        or references.get("epodoc")
        or references.get("original")
        or next(iter(references.values()), {})
    )
    selected_number = _select_document_number(references, reference_kind=reference_kind)
    selected_date = _select_document_date(references, reference_kind=reference_kind)
    return {
        **primary,
        "selected_number": selected_number,
        "selected_date": selected_date,
        "ids": references,
    }


def _document_id_to_dict(node: ET.Element) -> dict[str, str]:
    country = _first_text(node, "country")
    doc_number = _first_text(node, "doc-number")
    kind = _first_text(node, "kind")
    date = _first_text(node, "date")
    return {
        "country": country,
        "doc_number": doc_number,
        "kind": kind,
        "date": date,
        "full_number": _compose_full_number(country, doc_number, kind),
    }


def _compose_full_number(country: str, doc_number: str, kind: str) -> str:
    if country and doc_number:
        return f"{country}{doc_number}{kind}"
    return doc_number


def _select_document_number(
    references: dict[str, dict[str, str]], *, reference_kind: str
) -> str:
    if reference_kind == "application":
        priorities = ("epodoc", "docdb", "original")
    else:
        priorities = ("docdb", "epodoc", "original")

    for priority in priorities:
        candidate = references.get(priority, {})
        if priority == "original":
            value = candidate.get("doc_number", "")
        else:
            value = candidate.get("full_number", "") or candidate.get("doc_number", "")
        if value:
            return value
    for candidate in references.values():
        value = candidate.get("full_number", "") or candidate.get("doc_number", "")
        if value:
            return value
    return ""


def _select_document_date(
    references: dict[str, dict[str, str]], *, reference_kind: str
) -> str:
    if reference_kind == "application":
        priorities = ("epodoc", "docdb", "original")
    else:
        priorities = ("docdb", "epodoc", "original")

    for priority in priorities:
        value = references.get(priority, {}).get("date", "")
        if value:
            return value
    for candidate in references.values():
        value = candidate.get("date", "")
        if value:
            return value
    return ""


def _select_language_node(
    nodes: list[ET.Element],
    *,
    preferred: str | None = None,
) -> tuple[ET.Element | None, str | None, list[str]]:
    if not nodes:
        return None, None, []

    available_languages = [
        language for language in (_normalize_language(node.attrib.get("lang")) for node in nodes) if language
    ]
    normalized_preferred = _normalize_language(preferred)
    if normalized_preferred:
        for node in nodes:
            if _normalize_language(node.attrib.get("lang")) == normalized_preferred:
                return node, normalized_preferred, _unique_texts(available_languages)
    for node in nodes:
        if _normalize_language(node.attrib.get("lang")) == "EN":
            return node, "EN", _unique_texts(available_languages)

    selected = nodes[0]
    return (
        selected,
        _normalize_language(selected.attrib.get("lang")),
        _unique_texts(available_languages),
    )


def _normalize_language(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().upper()


def _texts_for_local(root: ET.Element, local_name: str) -> list[str]:
    return [_joined_text(node) for node in _all_local(root.iter(), local_name)]


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
