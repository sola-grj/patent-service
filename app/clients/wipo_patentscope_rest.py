import asyncio
import logging
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentDesignatedStates,
    PatentDrawingsInfo,
    PatentLookupResponse,
    PatentLookupWarning,
    PatentOriginalFile,
    PatentPriorityData,
    PatentReference,
    PatentRepresentative,
)
from app.utils.text_metrics import count_words, extract_drawing_labels, normalize_text
from app.utils.wipo_pdf import convert_wipo_zip_to_pdf

logger = logging.getLogger("patent_service")


@dataclass(slots=True)
class WipoRestDocument:
    document_id: str
    document_type: str
    min_spec_code: str = ""
    gazette_number: str = ""


class WipoPatentScopeRestClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        storage_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._storage_dir = storage_dir or Path(
            settings.wipo_storage_dir
            or Path(tempfile.gettempdir()) / "patent-service" / "wipo"
        )

    async def lookup_patent(
        self, reference: PatentReference, *, include_original_file: bool
    ) -> PatentLookupResponse:
        self._ensure_configured()
        rest_number = to_wipo_rest_number(reference)

        iasr_response = await self._request(
            f"/pct-publications/{rest_number}/ia-status-report",
            accept="application/json",
        )
        iasr_payload = self._json_response(iasr_response)
        iasr_info, iasr_refs = parse_iasr_payload(iasr_payload)

        documents_response = await self._request(
            f"/pct-publications/{rest_number}", accept="application/json"
        )
        documents_payload = self._json_response(documents_response)
        documents = parse_available_documents(documents_payload)
        selected_document = select_publication_document(documents)

        pamphlet_info = PatentBasicInfo()
        content_metrics: dict[str, Any] = {}
        page_names: list[str] = []
        selected_xml_page = ""
        warnings: list[PatentLookupWarning] = []

        if selected_document is not None:
            pages_response = await self._request(
                f"/documents/{selected_document.document_id}/pages",
                accept="application/json",
            )
            pages_payload = self._json_response(pages_response)
            page_names = parse_document_pages(pages_payload)
            selected_xml_page = select_publication_xml(page_names)
            if selected_xml_page:
                xml_response = await self._request(
                    f"/documents/{selected_document.document_id}/pages/"
                    f"{selected_xml_page}",
                    accept="application/octet-stream",
                )
                pamphlet_info, content_metrics = parse_published_application_xml(
                    xml_response.content
                )
            else:
                warnings.append(
                    _warning(
                        code="wipo_publication_xml_unavailable",
                        field="basic_info",
                        message="WIPO publication package did not expose publication XML.",
                    )
                )
        else:
            warnings.append(
                _warning(
                    code="wipo_publication_document_unavailable",
                    field="original_file",
                    message="WIPO did not expose a published-application document.",
                )
            )

        basic_info = merge_basic_info(pamphlet_info, iasr_info)
        original_file = PatentOriginalFile()
        original_file_refs: dict[str, Any] = {}
        if include_original_file:
            if selected_document is None:
                raise PatentServiceError(
                    code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                    status_code=404,
                    message="WIPO PATENTSCOPE did not expose an original publication file.",
                    source="wipo",
                )
            original_file, original_file_refs = await self._download_original(
                reference, selected_document
            )

        publication_reference = content_metrics.get("publication_reference") or iasr_refs.get(
            "publication_reference", {}
        )
        application_reference = content_metrics.get("application_reference") or iasr_refs.get(
            "application_reference", {}
        )
        raw_source_refs: dict[str, Any] = {
            "lookup_mode": "rest",
            "rest_number": rest_number,
            "iasr_request": f"/pct-publications/{rest_number}/ia-status-report",
            "available_documents_request": f"/pct-publications/{rest_number}",
            "available_documents": [asdict(document) for document in documents],
            "selected_document_id": selected_document.document_id
            if selected_document
            else "",
            "document_pages": page_names,
            "selected_xml_page": selected_xml_page,
            "publication_reference": publication_reference,
            "application_reference": application_reference,
            "title_language": content_metrics.get("title_language")
            or iasr_refs.get("title_language"),
            "abstract_language": content_metrics.get("abstract_language")
            or iasr_refs.get("abstract_language"),
            "first_priority_date": iasr_refs.get("first_priority_date"),
            "representatives_raw": content_metrics.get("representatives_raw")
            or iasr_refs.get("representatives_raw", []),
            "field_sources": {
                "bibliographic": "wipo_pamphlet_xml"
                if selected_xml_page
                else "wipo_iasr"
            },
            **original_file_refs,
        }

        return PatentLookupResponse(
            source=reference.source,
            normalized_number=reference.normalized_number,
            display_number=reference.display_number,
            basic_info=basic_info,
            application_date=application_reference.get("date") or None,
            application_no=basic_info.application_number or None,
            publication_date=basic_info.publication_date or None,
            # PATENTSCOPE displays WO publication numbers without the kind code.
            # Keep the upstream kind (for example A1) in raw_source_refs, while
            # exposing the canonical display form at the API boundary.
            publication_no=reference.display_number,
            agents=basic_info.representatives,
            priority_data=iasr_refs.get("priority_data", []),
            publication_language=content_metrics.get("publication_language")
            or iasr_refs.get("publication_language")
            or None,
            filing_language=content_metrics.get("filing_language")
            or iasr_refs.get("filing_language")
            or None,
            designated_states=iasr_refs.get("designated_states")
            or content_metrics.get("designated_states")
            or PatentDesignatedStates(),
            abstract_words=count_words(basic_info.abstract),
            description_words=content_metrics.get("description_words"),
            claims_count=content_metrics.get("claims_count"),
            claims_words=content_metrics.get("claims_words"),
            drawings=content_metrics.get("drawings") or PatentDrawingsInfo(),
            original_file=original_file,
            warnings=warnings,
            raw_source_refs=raw_source_refs,
        )

    def _ensure_configured(self) -> None:
        if self._settings.wipo_rest_configured:
            return
        raise PatentServiceError(
            code=ErrorCode.SOURCE_ACCESS_NOT_CONFIGURED,
            status_code=503,
            message="WIPO PATENTSCOPE REST credentials are not configured.",
            source="wipo",
            details={
                "required_env": [
                    "PATENT_SERVICE_WIPO_PATENTSCOPE_USERNAME",
                    "PATENT_SERVICE_WIPO_PATENTSCOPE_PASSWORD",
                ]
            },
        )

    async def _request(self, path: str, *, accept: str) -> httpx.Response:
        headers = {"Accept": accept, "Cookie": "OBBasicAuth=fromDialog"}
        auth = httpx.BasicAuth(
            self._settings.wipo_patentscope_username or "",
            self._settings.wipo_patentscope_password or "",
        )
        last_response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.wipo_patentscope_rest_base_url.rstrip("/"),
                auth=auth,
                headers=headers,
                transport=self._transport,
                timeout=self._settings.request_timeout_seconds,
                follow_redirects=True,
            ) as client:
                for attempt in range(3):
                    response = await client.get(path)
                    last_response = response
                    if response.status_code not in {500, 502, 503} or attempt == 2:
                        break
                    await asyncio.sleep(0.25 * (2**attempt))
        except httpx.RequestError as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="WIPO PATENTSCOPE REST request failed.",
                source="wipo",
                details={"error": str(exc), "path": path},
            ) from exc

        assert last_response is not None
        if last_response.is_error:
            raise map_wipo_rest_error(last_response, path=path)
        logger.info(
            "wipo rest request completed path=%s status=%s rate_remaining=%s bytes=%s",
            path,
            last_response.status_code,
            last_response.headers.get("X-RateLimit-Remaining"),
            len(last_response.content),
        )
        return last_response

    @staticmethod
    def _json_response(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="WIPO PATENTSCOPE REST returned invalid JSON.",
                source="wipo",
            ) from exc
        if not isinstance(payload, dict):
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="WIPO PATENTSCOPE REST returned an unexpected JSON payload.",
                source="wipo",
            )
        return payload

    async def _download_original(
        self, reference: PatentReference, document: WipoRestDocument
    ) -> tuple[PatentOriginalFile, dict[str, Any]]:
        path = f"/documents/{document.document_id}"
        headers = {
            "Accept": "application/octet-stream",
            "Cookie": "OBBasicAuth=fromDialog",
        }
        auth = httpx.BasicAuth(
            self._settings.wipo_patentscope_username or "",
            self._settings.wipo_patentscope_password or "",
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.wipo_patentscope_rest_base_url.rstrip("/"),
                auth=auth,
                headers=headers,
                transport=self._transport,
                timeout=self._settings.request_timeout_seconds,
                follow_redirects=True,
            ) as client:
                async with client.stream("GET", path) as response:
                    if response.is_error:
                        body = await response.aread()
                        error_response = httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            content=body,
                            request=response.request,
                        )
                        raise map_wipo_rest_error(error_response, path=path)
                    archive_filename = _content_disposition_filename(
                        response.headers.get("Content-Disposition", "")
                    ) or f"{reference.normalized_number}_{document.document_type}.zip"
                    archive_filename = _safe_filename(archive_filename)
                    archive_content_type = response.headers.get(
                        "Content-Type", "application/zip"
                    ).split(";", 1)[0]
                    self._storage_dir.mkdir(parents=True, exist_ok=True)
                    archive_path = self._storage_dir / archive_filename
                    partial_path = archive_path.with_suffix(
                        archive_path.suffix + ".part"
                    )
                    try:
                        with partial_path.open("wb") as output:
                            async for chunk in response.aiter_bytes():
                                output.write(chunk)
                        if (
                            archive_content_type in {
                                "application/zip",
                                "application/octet-stream",
                            }
                            or archive_path.suffix.lower() == ".zip"
                        ):
                            _validate_zip_members(partial_path.read_bytes())
                        partial_path.replace(archive_path)
                    except Exception:
                        partial_path.unlink(missing_ok=True)
                        raise
        except httpx.RequestError as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="WIPO original publication download failed.",
                source="wipo",
                details={"error": str(exc), "path": path},
            ) from exc
        pdf_filename = f"{reference.normalized_number}.pdf"
        pdf_path = self._storage_dir / pdf_filename
        try:
            ordered_pages = await asyncio.to_thread(
                convert_wipo_zip_to_pdf, archive_path, pdf_path
            )
        except Exception as exc:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="WIPO publication ZIP could not be converted to PDF.",
                source="wipo",
                details={"archive_path": str(archive_path), "error": str(exc)},
            ) from exc
        file_info = PatentOriginalFile(
            available=True,
            content_type="application/pdf",
            filename=pdf_filename,
            download_url=(
                f"{self._settings.api_prefix}/patents/files/{quote(pdf_filename)}"
            ),
            storage_path=str(pdf_path),
        )
        return file_info, {
            "original_archive": {
                "content_type": archive_content_type,
                "filename": archive_filename,
                "storage_path": str(archive_path),
            },
            "generated_pdf": {
                "source": "wipo_tiff_pages",
                "official_pdf": False,
                "page_count": len(ordered_pages),
                "ordered_pages": ordered_pages,
            },
        }


def to_wipo_rest_number(reference: PatentReference) -> str:
    digits = reference.doc_number
    if len(digits) != 10 or not digits.isdigit():
        raise PatentServiceError(
            code=ErrorCode.INVALID_PATENT_NUMBER_FORMAT,
            status_code=422,
            message="WO publication number cannot be converted to WIPO REST format.",
            source="wipo",
        )
    return f"WO{digits[2:]}"


def parse_iasr_payload(
    payload: dict[str, Any],
) -> tuple[PatentBasicInfo, dict[str, Any]]:
    biblio = payload.get("wo-bibliographic-data")
    if not isinstance(biblio, dict):
        biblio = payload
    publication_ref = _json_document_reference(biblio.get("publication-reference"))
    application_ref = _json_document_reference(biblio.get("application-reference"))
    titles = _language_entries(biblio.get("invention-title"))
    abstracts = _language_entries(payload.get("abstract"))
    title, title_language = _select_language_entry(titles)
    abstract, abstract_language = _select_language_entry(abstracts)
    parties = biblio.get("parties") if isinstance(biblio.get("parties"), dict) else {}
    applicants = _json_party_names(parties, "applicants", "applicant")
    inventors = _json_party_names(parties, "inventors", "inventor")
    representatives, representatives_raw = _json_representatives(parties)
    priority = _nested_value(biblio, "date-of-earliest-priority")
    first_priority_date = ""
    if isinstance(priority, dict):
        first_priority_date = str(priority.get("date") or "")
    publication_language = _json_reference_language(
        biblio.get("publication-reference")
    )
    filing_language = _json_reference_language(biblio.get("application-reference"))
    priority_data = _json_priority_data(biblio.get("wo-priority-info"))
    designated_states = _json_designated_states(biblio.get("designation-of-states"))
    return (
        PatentBasicInfo(
            title=title,
            abstract=abstract,
            publication_date=publication_ref.get("date", ""),
            application_number=application_ref.get("full_number", ""),
            applicants=applicants,
            inventors=inventors,
            representatives=representatives,
        ),
        {
            "publication_reference": publication_ref,
            "application_reference": application_ref,
            "title_language": title_language,
            "abstract_language": abstract_language,
            "first_priority_date": first_priority_date,
            "priority_data": priority_data,
            "publication_language": publication_language,
            "filing_language": filing_language,
            "designated_states": designated_states,
            "representatives_raw": representatives_raw,
        },
    )


def parse_available_documents(payload: dict[str, Any]) -> list[WipoRestDocument]:
    raw_documents = payload.get("availableDocuments") or payload.get(
        "available-documents"
    )
    if isinstance(raw_documents, dict):
        raw_documents = raw_documents.get("document") or raw_documents.get("documents")
    if not isinstance(raw_documents, list):
        raw_documents = []
    documents: list[WipoRestDocument] = []
    for item in raw_documents:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("docId") or item.get("document-id") or "")
        document_type = str(item.get("docType") or item.get("document-code") or "")
        if document_id and document_type:
            documents.append(
                WipoRestDocument(
                    document_id=document_id,
                    document_type=document_type,
                    min_spec_code=str(item.get("minSpecCode") or ""),
                    gazette_number=str(item.get("gazetteNumber") or ""),
                )
            )
    return documents


def select_publication_document(
    documents: list[WipoRestDocument],
) -> WipoRestDocument | None:
    for preferred in ("PAMPH", "APBDY", "PUB", "A1", "A2"):
        for document in documents:
            if document.document_type.upper() == preferred:
                return document
    return documents[0] if documents else None


def parse_document_pages(payload: dict[str, Any]) -> list[str]:
    content = payload.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [str(value) for value in content if value]
    return []


def select_publication_xml(page_names: list[str]) -> str:
    for preferred in ("wo-published-application.xml", "packagedata-pkda.xml"):
        for page_name in page_names:
            if page_name.lower() == preferred:
                return page_name
    for page_name in page_names:
        if page_name.lower().endswith(".xml"):
            return page_name
    return ""


def parse_published_application_xml(
    xml_payload: bytes | str,
) -> tuple[PatentBasicInfo, dict[str, Any]]:
    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as exc:
        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="WIPO published-application XML is invalid.",
            source="wipo",
        ) from exc

    publication_ref = _xml_document_reference(root, "publication-reference")
    application_ref = _xml_document_reference(root, "application-reference")
    title_node, title_language = _select_xml_language_node(root, "invention-title")
    abstract_node, abstract_language = _select_xml_language_node(root, "abstract")
    representatives, representatives_raw = _xml_representatives(root)
    ipc = _xml_classifications(root)
    description_node = _first_local(root.iter(), "description")
    description_text = _joined_xml_text(description_node)
    description_paragraphs = (
        [_joined_xml_text(node) for node in _all_local(description_node.iter(), "p")]
        if description_node is not None
        else []
    )
    claim_nodes = _all_local(root.iter(), "claim")
    claim_texts = [_joined_xml_text(node) for node in claim_nodes]
    claim_texts = [text for text in claim_texts if text]
    drawings_nodes = _all_local(root.iter(), "drawings")
    drawing_text = [_joined_xml_text(node) for node in drawings_nodes]
    labels = extract_drawing_labels([*description_paragraphs, *drawing_text])
    drawings = PatentDrawingsInfo(
        has_drawings=bool(drawings_nodes),
        drawing_labels=labels,
        drawing_page_count=None,
    )
    basic_info = PatentBasicInfo(
        title=_joined_xml_text(title_node),
        abstract=_joined_xml_text(abstract_node),
        publication_date=publication_ref.get("date", ""),
        application_number=application_ref.get("full_number", ""),
        applicants=_xml_party_names(root, "applicants", "applicant"),
        inventors=_xml_party_names(root, "inventors", "inventor"),
        representatives=representatives,
        ipc=ipc,
    )
    return basic_info, {
        "publication_reference": publication_ref,
        "application_reference": application_ref,
        "title_language": title_language,
        "abstract_language": abstract_language,
        "representatives_raw": representatives_raw,
        "publication_language": _xml_reference_language(
            root, "publication-reference"
        ),
        "filing_language": _xml_reference_language(root, "application-reference"),
        "designated_states": _xml_designated_states(root),
        "description_words": count_words(description_text)
        if description_text
        else None,
        "claims_count": len(claim_texts) if claim_texts else None,
        "claims_words": count_words(" ".join(claim_texts)) if claim_texts else None,
        "drawings": drawings,
    }


def merge_basic_info(primary: PatentBasicInfo, fallback: PatentBasicInfo) -> PatentBasicInfo:
    return PatentBasicInfo(
        title=primary.title or fallback.title,
        abstract=primary.abstract or fallback.abstract,
        publication_date=primary.publication_date or fallback.publication_date,
        application_number=primary.application_number or fallback.application_number,
        applicants=primary.applicants or fallback.applicants,
        inventors=primary.inventors or fallback.inventors,
        representatives=primary.representatives or fallback.representatives,
        ipc=primary.ipc or fallback.ipc,
        cpc=primary.cpc or fallback.cpc,
    )


def map_wipo_rest_error(response: httpx.Response, *, path: str) -> PatentServiceError:
    error_data = _error_payload(response)
    details = {
        "status_code": response.status_code,
        "path": path,
        **error_data,
    }
    for header in (
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "X-ErrorLimit-Remaining",
        "X-ErrorLimit-Reset",
    ):
        if header in response.headers:
            details[header] = response.headers[header]
    status = response.status_code
    message = error_data.get("wo-error-message") or "WIPO PATENTSCOPE REST request failed."
    if status == 400:
        code, api_status = ErrorCode.INVALID_PATENT_NUMBER_FORMAT, 422
    elif status == 401:
        code, api_status = ErrorCode.SOURCE_AUTH_REQUIRED, 503
    elif status == 403:
        code, api_status = ErrorCode.SOURCE_ACCESS_DENIED, 403
    elif status == 404:
        code, api_status = ErrorCode.SOURCE_NO_RESULT, 404
    elif status == 406:
        code, api_status = ErrorCode.UPSTREAM_RESPONSE_INVALID, 502
    elif status == 429:
        code, api_status = ErrorCode.SOURCE_RATE_LIMIT, 429
    else:
        code, api_status = ErrorCode.SOURCE_UNAVAILABLE, 503
    return PatentServiceError(
        code=code,
        status_code=api_status,
        message=message,
        source="wipo",
        details=details,
    )


def _error_payload(response: httpx.Response) -> dict[str, str]:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items() if value is not None}
    except ValueError:
        pass
    try:
        root = ET.fromstring(response.content)
        return {_local_name(node.tag): (node.text or "").strip() for node in root}
    except ET.ParseError:
        return {}


def _language_entries(value: Any) -> list[tuple[str, str]]:
    items = value if isinstance(value, list) else [value]
    entries: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _json_text(item)
        if text:
            entries.append((str(item.get("lang") or "").upper(), text))
    return entries


def _select_language_entry(entries: list[tuple[str, str]]) -> tuple[str, str]:
    for language, text in entries:
        if language == "EN":
            return text, language
    return (entries[0][1], entries[0][0]) if entries else ("", "")


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, list):
        return normalize_text(" ".join(_json_text(item) for item in value))
    if not isinstance(value, dict):
        return ""
    for key in ("value", "content", "p", "text"):
        if key in value:
            text = _json_text(value[key])
            if text:
                return text
    return ""


def _json_document_reference(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    document_id = value.get("document-id")
    if isinstance(document_id, list):
        document_id = document_id[0] if document_id else {}
    if not isinstance(document_id, dict):
        document_id = value
    country = str(document_id.get("country") or "")
    number = str(document_id.get("doc-number") or "")
    kind = str(document_id.get("kind") or "")
    if number.upper().startswith("PCT/"):
        full_number = number
    elif country == "PCT" and number:
        full_number = f"PCT/{number}"
    elif country and number and not number.upper().startswith(country.upper()):
        full_number = f"{country}{number}{kind}"
    else:
        full_number = f"{number}{kind}"
    return {
        "country": country,
        "doc_number": number,
        "kind": kind,
        "date": str(document_id.get("date") or ""),
        "full_number": full_number,
    }


def _json_reference_language(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    document_id = value.get("document-id")
    if isinstance(document_id, list):
        document_id = document_id[0] if document_id else {}
    if not isinstance(document_id, dict):
        return ""
    return str(document_id.get("lang") or "").upper()


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _json_priority_data(value: Any) -> list[PatentPriorityData]:
    priorities: list[PatentPriorityData] = []
    for item in _walk_json(value):
        claim = item.get("priority-claim")
        if not isinstance(claim, dict):
            continue
        priority = PatentPriorityData(
            number=str(claim.get("doc-number") or ""),
            date=str(claim.get("date") or ""),
            country=str(claim.get("country") or ""),
            kind=str(claim.get("kind") or ""),
        )
        if any(priority.model_dump().values()) and priority not in priorities:
            priorities.append(priority)
    return priorities


def _json_designated_states(value: Any) -> PatentDesignatedStates:
    regions: list[str] = []
    countries: list[str] = []
    protection_types: list[str] = []
    for item in _walk_json(value):
        region = item.get("region")
        if isinstance(region, dict):
            code = str(region.get("country") or "")
            if code and code not in regions:
                regions.append(code)
        protection = item.get("kind-of-protection")
        if protection:
            code = str(protection)
            if code not in protection_types:
                protection_types.append(code)
        country = item.get("country")
        if isinstance(country, str) and country and country not in countries:
            countries.append(country)
        mixed_states = item.get("countryAndProtectionRequest")
        if isinstance(mixed_states, list):
            for state in mixed_states:
                if isinstance(state, str) and state and state not in countries:
                    countries.append(state)
    countries = [country for country in countries if country not in regions]
    return PatentDesignatedStates(
        regions=regions,
        countries=countries,
        protection_types=protection_types,
    )


def _nested_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _nested_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_value(child, key)
            if found is not None:
                return found
    return None


def _json_party_names(parties: dict[str, Any], group: str, item: str) -> list[str]:
    group_value = parties.get(group)
    if not isinstance(group_value, dict):
        return []
    values = group_value.get(item)
    values = values if isinstance(values, list) else [values]
    names: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        for addressbook in _addressbooks(value):
            name = _addressbook_name(addressbook)
            if name and name not in names:
                names.append(name)
    return names


def _json_representatives(
    parties: dict[str, Any],
) -> tuple[list[PatentRepresentative], list[dict[str, Any]]]:
    candidates: list[Any] = []
    for group_name, item_name in (
        ("agents", "agent"),
        ("representatives", "representative"),
    ):
        group = parties.get(group_name)
        if isinstance(group, dict):
            values = group.get(item_name)
            candidates.extend(values if isinstance(values, list) else [values])
    representatives: list[PatentRepresentative] = []
    raw: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw.append(candidate)
        for addressbook in _addressbooks(candidate):
            representative = _representative_from_addressbook(addressbook)
            if representative and representative not in representatives:
                representatives.append(representative)
    return representatives, raw


def _addressbooks(value: dict[str, Any]) -> list[dict[str, Any]]:
    raw = value.get("addressbook") or value.get("address-book") or []
    raw = raw if isinstance(raw, list) else [raw]
    return [item for item in raw if isinstance(item, dict)]


def _addressbook_name(addressbook: dict[str, Any]) -> str:
    for key in ("orgname", "name"):
        value = addressbook.get(key)
        if isinstance(value, dict):
            value = value.get("value")
        if value:
            return normalize_text(str(value))
    parts = [addressbook.get("first-name"), addressbook.get("last-name")]
    return normalize_text(" ".join(str(part) for part in parts if part))


def _representative_from_addressbook(
    addressbook: dict[str, Any],
) -> PatentRepresentative | None:
    organization = addressbook.get("orgname") or ""
    if isinstance(organization, dict):
        organization = organization.get("value") or ""
    name_value = addressbook.get("name") or ""
    if isinstance(name_value, dict):
        name_value = name_value.get("value") or ""
    if not name_value:
        name_value = " ".join(
            str(addressbook.get(key) or "") for key in ("first-name", "last-name")
        )
    address_value = addressbook.get("address")
    address_parts: list[str] = []
    country = ""
    if isinstance(address_value, dict):
        for key, value in address_value.items():
            if not value:
                continue
            if key == "country":
                country = str(value)
            else:
                address_parts.append(str(value))
    representative = PatentRepresentative(
        name=normalize_text(str(name_value)),
        organization=normalize_text(str(organization)),
        address=normalize_text(" ".join(address_parts)),
        country=country,
    )
    return representative if any(representative.model_dump().values()) else None


def _xml_document_reference(root: ET.Element, local_name: str) -> dict[str, str]:
    reference = _first_local(root.iter(), local_name)
    document_id = _first_local(reference.iter(), "document-id") if reference is not None else None
    if document_id is None:
        return {}
    country = _first_text(document_id, "country")
    number = _first_text(document_id, "doc-number")
    kind = _first_text(document_id, "kind")
    application_type = (reference.get("appl-type") or "").strip().lower()
    is_international_application = local_name == "application-reference" and (
        application_type in {"international", "pct"}
    )
    if number.upper().startswith("PCT/"):
        full_number = number
    elif is_international_application and number:
        full_number = f"PCT/{number}"
    elif country.upper() == "PCT" and number:
        full_number = f"PCT/{number}"
    elif country and number and not number.upper().startswith(country.upper()):
        full_number = f"{country}{number}{kind}"
    else:
        full_number = f"{number}{kind}"
    return {
        "country": country,
        "doc_number": number,
        "kind": kind,
        "date": _first_text(document_id, "date"),
        "full_number": full_number,
    }


def _xml_reference_language(root: ET.Element, local_name: str) -> str:
    reference = _first_local(root.iter(), local_name)
    document_id = (
        _first_local(reference.iter(), "document-id")
        if reference is not None
        else None
    )
    if document_id is None:
        return ""
    return (document_id.get("lang") or "").upper()


def _xml_designated_states(root: ET.Element) -> PatentDesignatedStates:
    designation = _first_local(root.iter(), "designation-of-states")
    if designation is None:
        return PatentDesignatedStates()
    regions: list[str] = []
    countries: list[str] = []
    protection_types: list[str] = []
    for region_node in _all_local(designation.iter(), "region"):
        code = _first_text(region_node, "country")
        if code and code not in regions:
            regions.append(code)
    for country_node in _all_local(designation.iter(), "country"):
        code = _joined_xml_text(country_node)
        if code and code not in regions and code not in countries:
            countries.append(code)
    for protection_node in _all_local(designation.iter(), "kind-of-protection"):
        value = _joined_xml_text(protection_node)
        if value and value not in protection_types:
            protection_types.append(value)
    return PatentDesignatedStates(
        regions=regions,
        countries=countries,
        protection_types=protection_types,
    )


def _select_xml_language_node(
    root: ET.Element, name: str
) -> tuple[ET.Element | None, str]:
    nodes = _all_local(root.iter(), name)
    for node in nodes:
        language = (node.attrib.get("lang") or "").upper()
        if language == "EN":
            return node, language
    if nodes:
        return nodes[0], (nodes[0].attrib.get("lang") or "").upper()
    return None, ""


def _xml_party_names(root: ET.Element, group: str, item: str) -> list[str]:
    names: list[str] = []
    for group_node in _all_local(root.iter(), group):
        for item_node in _all_local(group_node.iter(), item):
            addressbook = _first_local(item_node.iter(), "addressbook")
            if addressbook is None:
                continue
            name = (
                _first_text(addressbook, "orgname")
                or _first_text(addressbook, "name")
                or normalize_text(
                    " ".join(
                        value
                        for value in (
                            _first_text(addressbook, "first-name"),
                            _first_text(addressbook, "last-name"),
                        )
                        if value
                    )
                )
            )
            if name and name not in names:
                names.append(name)
        break
    return names


def _xml_representatives(
    root: ET.Element,
) -> tuple[list[PatentRepresentative], list[dict[str, str]]]:
    representatives: list[PatentRepresentative] = []
    raw: list[dict[str, str]] = []
    for group_name, item_name in (("agents", "agent"), ("representatives", "representative")):
        for group in _all_local(root.iter(), group_name):
            for item in _all_local(group.iter(), item_name):
                addressbook = _first_local(item.iter(), "addressbook")
                if addressbook is None:
                    continue
                address_node = _first_local(addressbook.iter(), "address")
                address_parts: list[str] = []
                country = ""
                if address_node is not None:
                    for child in address_node:
                        value = _joined_xml_text(child)
                        if _local_name(child.tag) == "country":
                            country = value
                        elif value:
                            address_parts.append(value)
                name = _first_text(addressbook, "name")
                if not name:
                    name = normalize_text(
                        " ".join(
                            value
                            for value in (
                                _first_text(addressbook, "first-name"),
                                _first_text(addressbook, "last-name"),
                            )
                            if value
                        )
                    )
                representative = PatentRepresentative(
                    name=name,
                    organization=_first_text(addressbook, "orgname"),
                    address=normalize_text(" ".join(address_parts)),
                    country=country,
                )
                if any(representative.model_dump().values()) and representative not in representatives:
                    representatives.append(representative)
                raw.append(
                    {
                        "name": representative.name,
                        "organization": representative.organization,
                        "address": representative.address,
                        "country": representative.country,
                    }
                )
            break
    return representatives, raw


def _xml_classifications(root: ET.Element) -> list[str]:
    values: list[str] = []
    for node_name in ("classification-ipcr", "classification-ipc"):
        for node in _all_local(root.iter(), node_name):
            candidates = [
                _first_text(node, "text"),
                _first_text(node, "main-classification"),
                *[
                    _joined_xml_text(child)
                    for child in _all_local(node.iter(), "further-classification")
                ],
            ]
            if not any(candidates):
                candidates = [_joined_xml_text(node)]
            for value in candidates:
                value = normalize_text(value)
                if value and value not in values:
                    values.append(value)
    return values


def _content_disposition_filename(value: str) -> str:
    match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', value, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _safe_filename(value: str) -> str:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="WIPO returned an unsafe original-file name.",
            source="wipo",
        )
    filename = candidate.name
    if not filename or re.match(r"^[A-Za-z]:", filename):
        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="WIPO returned an unsafe original-file name.",
            source="wipo",
        )
    return filename


def _validate_zip_members(payload: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            for member in archive.infolist():
                normalized = member.filename.replace("\\", "/")
                candidate = PurePosixPath(normalized)
                if (
                    candidate.is_absolute()
                    or ".." in candidate.parts
                    or re.match(r"^[A-Za-z]:", normalized)
                ):
                    raise PatentServiceError(
                        code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                        status_code=502,
                        message="WIPO returned an unsafe ZIP member path.",
                        source="wipo",
                        details={"member": member.filename},
                    )
    except zipfile.BadZipFile as exc:
        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="WIPO original publication is not a valid ZIP file.",
            source="wipo",
        ) from exc


def _warning(*, code: str, field: str, message: str) -> PatentLookupWarning:
    return PatentLookupWarning(code=code, field=field, message=message, source="wipo")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_local(nodes: Any, name: str) -> ET.Element | None:
    for node in nodes:
        if _local_name(node.tag) == name:
            return node
    return None


def _all_local(nodes: Any, name: str) -> list[ET.Element]:
    return [node for node in nodes if _local_name(node.tag) == name]


def _first_text(root: ET.Element, name: str) -> str:
    node = _first_local(root.iter(), name)
    return _joined_xml_text(node)


def _joined_xml_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return normalize_text(" ".join(part for part in node.itertext() if part))
