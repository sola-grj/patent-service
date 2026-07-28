import asyncio
import base64
import binascii
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth
from zeep import Client
from zeep.exceptions import Fault, TransportError
from zeep.transports import Transport

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentReference,
)


@dataclass(slots=True)
class WipoDocumentRef:
    document_id: str
    document_code: str
    content_type: str
    filename: str
    title: str


class WipoPatentScopeClient:
    def __init__(
        self,
        settings: Settings,
        *,
        service_factory: Callable[[], Any] | None = None,
        storage_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._service_factory = service_factory
        self._service: Any | None = None
        self._storage_dir = storage_dir or (
            Path(tempfile.gettempdir()) / "patent-service" / "wipo"
        )

    async def lookup_patent(
        self,
        reference: PatentReference,
        *,
        include_original_file: bool,
        storage_dir: Path | None = None,
    ) -> PatentLookupResponse:
        self._ensure_configured()

        iasr_xml, iasr_call_ref = await self._fetch_iasr(reference)
        basic_info, biblio_refs = self.parse_bibliographic_data(iasr_xml)

        original_file = PatentOriginalFile()
        raw_source_refs: dict[str, Any] = {
            "iasr_request": iasr_call_ref,
            "publication_reference": biblio_refs["publication_reference"],
            "application_reference": biblio_refs["application_reference"],
        }

        if include_original_file:
            docs_xml, documents_call_ref = await self._fetch_available_documents(
                reference
            )
            selected_doc, documents_refs = self.parse_available_documents(docs_xml)
            if selected_doc is None:
                raise PatentServiceError(
                    code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                    status_code=404,
                    message="WIPO PATENTSCOPE did not expose an original publication file.",
                    source="wipo",
                    details={
                        "normalized_number": reference.normalized_number,
                        "display_number": reference.display_number,
                    },
                )

            content_payload = await self._fetch_document_content(selected_doc)
            original_file, file_refs = self.materialize_original_file(
                reference,
                selected_doc,
                content_payload,
                storage_dir=storage_dir,
            )
            raw_source_refs["available_documents_request"] = documents_call_ref
            raw_source_refs["available_documents"] = documents_refs
            raw_source_refs["selected_document"] = file_refs

        return PatentLookupResponse(
            source=reference.source,
            normalized_number=reference.normalized_number,
            display_number=reference.display_number,
            basic_info=basic_info,
            original_file=original_file,
            raw_source_refs=raw_source_refs,
        )

    def _ensure_configured(self) -> None:
        if self._settings.wipo_patentscope_configured:
            return

        raise PatentServiceError(
            code=ErrorCode.SOURCE_ACCESS_NOT_CONFIGURED,
            status_code=503,
            message="WIPO PATENTSCOPE SOAP credentials are not configured.",
            source="wipo",
            details={
                "required_env": [
                    "PATENT_SERVICE_WIPO_PATENTSCOPE_SERVICE_URL",
                    "PATENT_SERVICE_WIPO_PATENTSCOPE_USERNAME",
                    "PATENT_SERVICE_WIPO_PATENTSCOPE_PASSWORD",
                ]
            },
        )

    async def _fetch_iasr(
        self, reference: PatentReference
    ) -> tuple[str, dict[str, Any]]:
        response, call_ref = await self._call_identifier_operation(
            "getIASR", self._identifier_candidates(reference)
        )
        xml_text = self._coerce_xml_payload(response)
        return xml_text, call_ref

    async def _fetch_available_documents(
        self, reference: PatentReference
    ) -> tuple[str, dict[str, Any]]:
        response, call_ref = await self._call_identifier_operation(
            "getAvailableDocuments", self._identifier_candidates(reference)
        )
        xml_text = self._coerce_xml_payload(response)
        return xml_text, call_ref

    async def _fetch_document_content(self, document: WipoDocumentRef) -> Any:
        toc_response = await self._call_document_table_of_contents(document)
        page_ids = self._extract_page_ids(toc_response)

        candidate_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
            ((document.document_id,), {}),
            ((), {"documentId": document.document_id}),
        ]
        if page_ids:
            candidate_calls.extend(
                [
                    ((document.document_id, page_ids), {}),
                    (
                        (),
                        {"documentId": document.document_id, "pageIds": page_ids},
                    ),
                    (
                        (),
                        {
                            "documentId": document.document_id,
                            "pageIds": ",".join(page_ids),
                        },
                    ),
                ]
            )

        last_error: Exception | None = None
        for args, kwargs in candidate_calls:
            try:
                return await self._invoke_service_method(
                    "getDocumentContent", *args, **kwargs
                )
            except TypeError:
                continue
            except Fault as exc:
                last_error = exc
                continue

        details = {
            "document_id": document.document_id,
            "document_code": document.document_code,
            "page_ids": page_ids,
        }
        if last_error is not None:
            details["fault"] = str(last_error)

        raise PatentServiceError(
            code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
            status_code=404,
            message="WIPO PATENTSCOPE did not return the original publication payload.",
            source="wipo",
            details=details,
        )

    async def _call_document_table_of_contents(
        self, document: WipoDocumentRef
    ) -> Any | None:
        try:
            return await self._invoke_service_method(
                "getDocumentTableOfContents", document.document_id
            )
        except AttributeError:
            return None
        except TypeError:
            return None
        except Fault:
            return None

    async def _call_identifier_operation(
        self, method_name: str, identifiers: list[str]
    ) -> tuple[Any, dict[str, Any]]:
        last_fault: Fault | None = None
        for identifier in identifiers:
            candidate_calls = [
                ((identifier,), {}),
                ((), {"applicationNumber": identifier}),
                ((), {"publicationNumber": identifier}),
                ((), {"number": identifier}),
            ]
            for args, kwargs in candidate_calls:
                try:
                    response = await self._invoke_service_method(
                        method_name, *args, **kwargs
                    )
                except TypeError:
                    continue
                except Fault as exc:
                    last_fault = exc
                    continue

                return response, {
                    "method": method_name,
                    "identifier": identifier,
                    "args_style": "kwargs" if kwargs else "positional",
                }

        if last_fault is not None and _fault_says_no_result(last_fault):
            raise PatentServiceError(
                code=ErrorCode.SOURCE_NO_RESULT,
                status_code=404,
                message="No publication was found in WIPO PATENTSCOPE.",
                source="wipo",
                details={"fault": str(last_fault)},
            )

        raise PatentServiceError(
            code=ErrorCode.SOURCE_NO_RESULT,
            status_code=404,
            message="No publication was found in WIPO PATENTSCOPE.",
            source="wipo",
            details={"identifiers_tried": identifiers, "method": method_name},
        )

    async def _invoke_service_method(
        self, method_name: str, *args: Any, **kwargs: Any
    ) -> Any:
        method = await self._get_service_method(method_name)
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except Fault:
            raise
        except TransportError as exc:
            raise self._map_transport_error(exc) from exc
        except requests.RequestException as exc:
            raise self._map_request_error(exc) from exc

    async def _get_service_method(self, method_name: str) -> Callable[..., Any]:
        service = await self._get_service()
        method = getattr(service, method_name, None)
        if method is None:
            raise AttributeError(method_name)
        return method

    async def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        try:
            self._service = await asyncio.to_thread(self._build_service)
        except TransportError as exc:
            raise self._map_transport_error(exc) from exc
        except requests.RequestException as exc:
            raise self._map_request_error(exc) from exc

        return self._service

    def _build_service(self) -> Any:
        if self._service_factory is not None:
            return self._service_factory()

        session = requests.Session()
        session.auth = HTTPBasicAuth(
            self._settings.wipo_patentscope_username or "",
            self._settings.wipo_patentscope_password or "",
        )
        transport = Transport(
            session=session,
            timeout=self._settings.request_timeout_seconds,
        )
        client = Client(wsdl=self._resolve_wsdl_url(), transport=transport)
        return client.service

    def _resolve_wsdl_url(self) -> str:
        base_url = self._settings.wipo_patentscope_service_url or ""
        if "?wsdl" in base_url.lower():
            return base_url
        return f"{base_url}?wsdl"

    def _map_transport_error(self, exc: TransportError) -> PatentServiceError:
        status_code = getattr(exc, "status_code", None)
        if status_code in {401, 403}:
            return PatentServiceError(
                code=ErrorCode.SOURCE_AUTH_REQUIRED,
                status_code=503,
                message="WIPO PATENTSCOPE authentication failed.",
                source="wipo",
                details={"status_code": status_code},
            )
        if status_code == 404:
            return PatentServiceError(
                code=ErrorCode.SOURCE_NO_RESULT,
                status_code=404,
                message="No publication was found in WIPO PATENTSCOPE.",
                source="wipo",
            )
        return PatentServiceError(
            code=ErrorCode.SOURCE_UNAVAILABLE,
            status_code=503,
            message="WIPO PATENTSCOPE request failed.",
            source="wipo",
            details={"status_code": status_code, "error": str(exc)},
        )

    def _map_request_error(self, exc: requests.RequestException) -> PatentServiceError:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in {401, 403}:
            return PatentServiceError(
                code=ErrorCode.SOURCE_AUTH_REQUIRED,
                status_code=503,
                message="WIPO PATENTSCOPE authentication failed.",
                source="wipo",
                details={"status_code": status_code},
            )
        return PatentServiceError(
            code=ErrorCode.SOURCE_UNAVAILABLE,
            status_code=503,
            message="WIPO PATENTSCOPE request failed.",
            source="wipo",
            details={"status_code": status_code, "error": str(exc)},
        )

    @staticmethod
    def parse_bibliographic_data(
        xml_text: str,
    ) -> tuple[PatentBasicInfo, dict[str, dict[str, str]]]:
        root = _parse_xml(xml_text, source="wipo")
        publication_doc = _find_document_id(root, "publication-reference")
        application_doc = _find_document_id(root, "application-reference")
        title = _first_text(root, "invention-title")
        abstract = _joined_text(_first_local(root.iter(), "abstract"))
        applicants = _party_names(root, "applicants", "applicant")
        inventors = _party_names(root, "inventors", "inventor")

        basic_info = PatentBasicInfo(
            title=title,
            abstract=abstract,
            publication_date=publication_doc.get("date", ""),
            application_number=application_doc.get("pct_number", "")
            or application_doc.get("full_number", ""),
            applicants=applicants,
            inventors=inventors,
            ipc=_classification_values(root, "classification-ipc"),
            cpc=[],
        )
        raw_refs = {
            "publication_reference": publication_doc,
            "application_reference": application_doc,
        }
        return basic_info, raw_refs

    @staticmethod
    def parse_available_documents(
        xml_text: str,
    ) -> tuple[WipoDocumentRef | None, dict[str, Any]]:
        root = _parse_xml(xml_text, source="wipo")
        documents = _collect_documents(root)
        selected = _select_original_document(documents)
        raw_refs = {
            "documents": [asdict(document) for document in documents],
            "selected_document_id": selected.document_id if selected else "",
        }
        return selected, raw_refs

    def materialize_original_file(
        self,
        reference: PatentReference,
        document: WipoDocumentRef,
        payload: Any,
        *,
        storage_dir: Path | None = None,
    ) -> tuple[PatentOriginalFile, dict[str, Any]]:
        binary_payload = _extract_binary_payload(payload)
        if binary_payload is None:
            raise PatentServiceError(
                code=ErrorCode.ORIGINAL_FILE_NOT_AVAILABLE,
                status_code=404,
                message="WIPO PATENTSCOPE did not return binary document content.",
                source="wipo",
                details={
                    "document_id": document.document_id,
                    "document_code": document.document_code,
                },
            )

        content_type = (
            _extract_scalar(payload, {"mimeType", "contentType", "content_type"})
            or document.content_type
            or "application/octet-stream"
        )
        filename = (
            _extract_scalar(payload, {"fileName", "filename", "name"})
            or document.filename
            or _default_filename(reference, document, content_type)
        )

        output_dir = storage_dir or self._storage_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        storage_path = output_dir / filename
        storage_path.write_bytes(binary_payload)

        file_info = PatentOriginalFile(
            available=True,
            content_type=content_type,
            filename=filename,
            storage_path=str(storage_path),
        )
        raw_refs = {
            "document_id": document.document_id,
            "document_code": document.document_code,
            "content_type": content_type,
            "storage_path": str(storage_path),
            "byte_size": len(binary_payload),
        }
        return file_info, raw_refs

    @staticmethod
    def _coerce_xml_payload(payload: Any) -> str:
        if isinstance(payload, bytes):
            return payload.decode("utf-8")
        if isinstance(payload, str):
            return payload

        for key in ("return", "xml", "iasr", "content", "document"):
            value = _extract_value(payload, key)
            if isinstance(value, bytes):
                return value.decode("utf-8")
            if isinstance(value, str) and value.strip().startswith("<"):
                return value

        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="WIPO PATENTSCOPE response did not contain XML payload.",
            source="wipo",
        )

    @staticmethod
    def _identifier_candidates(reference: PatentReference) -> list[str]:
        values = [
            reference.normalized_number,
            f"WO{reference.doc_number}",
            reference.display_number,
        ]
        if reference.kind_code:
            values.append(f"{reference.display_number}{reference.kind_code}")

        ordered: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value and value not in seen:
                seen.add(value)
                ordered.append(value)
        return ordered

    @staticmethod
    def _extract_page_ids(payload: Any) -> list[str]:
        root: ET.Element | None = None
        try:
            root = _parse_xml(
                WipoPatentScopeClient._coerce_xml_payload(payload), source="wipo"
            )
        except PatentServiceError:
            root = None

        if root is None:
            return []

        page_ids: list[str] = []
        for node in root.iter():
            local_name = _local_name(node.tag)
            if local_name in {"page-id", "pageId", "page"}:
                value = (
                    node.attrib.get("id")
                    or node.attrib.get("page-id")
                    or _joined_text(node)
                )
                value = value.strip()
                if value:
                    page_ids.append(value)
        return _unique_texts(page_ids)


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


def _first_local(nodes: Iterable[ET.Element], local_name: str) -> ET.Element | None:
    for node in nodes:
        if _local_name(node.tag) == local_name:
            return node
    return None


def _all_local(nodes: Iterable[ET.Element], local_name: str) -> list[ET.Element]:
    return [node for node in nodes if _local_name(node.tag) == local_name]


def _joined_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    parts = [part.strip() for part in node.itertext() if part and part.strip()]
    return " ".join(parts)


def _first_text(root: ET.Element, local_name: str) -> str:
    return _joined_text(_first_local(root.iter(), local_name))


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
        fragments = [
            _first_text(item, "main-classification"),
            *_texts_for_local(item, "further-classification"),
            _first_text(item, "text"),
        ]
        for value in fragments:
            compact = " ".join(value.split())
            if compact:
                values.append(compact)
    return _unique_texts(values)


def _party_names(root: ET.Element, group_name: str, item_name: str) -> list[str]:
    group = _first_local(root.iter(), group_name)
    if group is None:
        return []

    values: list[str] = []
    for item in _all_local(group.iter(), item_name):
        addressbook = _first_local(item.iter(), "addressbook")
        if addressbook is None:
            value = _joined_text(item)
        else:
            value = (
                _first_text(addressbook, "orgname")
                or _person_name(addressbook)
                or _joined_text(addressbook)
            )
        value = " ".join(value.split())
        if value:
            values.append(value)
    return _unique_texts(values)


def _person_name(root: ET.Element) -> str:
    first_name = _first_text(root, "first-name")
    last_name = _first_text(root, "last-name")
    return " ".join(part for part in [first_name, last_name] if part).strip()


def _find_document_id(root: ET.Element, container_name: str) -> dict[str, str]:
    container = _first_local(root.iter(), container_name)
    if container is None:
        return {}

    document_id = _first_local(container.iter(), "document-id")
    if document_id is None:
        return {}

    country = _first_text(document_id, "country")
    doc_number = _first_text(document_id, "doc-number")
    kind = _first_text(document_id, "kind")
    date = _first_text(document_id, "date")
    full_number = f"{country}{doc_number}{kind}"

    pct_number = ""
    if country and doc_number:
        if country == "WO" and len(doc_number) >= 10:
            pct_number = f"WO/{doc_number[:4]}/{doc_number[4:]}"
        else:
            pct_number = f"{country}/{doc_number}"

    return {
        "country": country,
        "doc_number": doc_number,
        "kind": kind,
        "date": date,
        "full_number": full_number,
        "pct_number": pct_number,
    }


def _texts_for_local(root: ET.Element, local_name: str) -> list[str]:
    return [_joined_text(node) for node in _all_local(root.iter(), local_name)]


def _collect_documents(root: ET.Element) -> list[WipoDocumentRef]:
    documents: list[WipoDocumentRef] = []
    for node in root.iter():
        field_names = {
            _local_name(child.tag) for child in node if isinstance(child.tag, str)
        }
        if "document-id" not in field_names and "document-code" not in field_names:
            continue

        document_id = _first_text(node, "document-id") or node.attrib.get(
            "document-id", ""
        )
        document_code = (
            _first_text(node, "document-code")
            or _first_text(node, "document-type")
            or _first_text(node, "type")
            or node.attrib.get("document-code", "")
        )
        if not document_id or not document_code:
            continue

        content_type = (
            _first_text(node, "mime-type")
            or _first_text(node, "content-type")
            or _first_text(node, "format")
        )
        filename = (
            _first_text(node, "file-name")
            or _first_text(node, "filename")
            or _first_text(node, "name")
        )
        title = _first_text(node, "title") or _first_text(node, "description")
        documents.append(
            WipoDocumentRef(
                document_id=document_id,
                document_code=document_code,
                content_type=content_type,
                filename=filename,
                title=title,
            )
        )

    unique: dict[tuple[str, str], WipoDocumentRef] = {}
    for document in documents:
        unique[(document.document_id, document.document_code)] = document
    return list(unique.values())


def _select_original_document(
    documents: list[WipoDocumentRef],
) -> WipoDocumentRef | None:
    preferred_codes = ["PAMPH", "PUB", "A1", "A2"]
    for code in preferred_codes:
        for document in documents:
            if document.document_code.upper() == code:
                return document
    return documents[0] if documents else None


def _extract_value(payload: Any, key: str) -> Any | None:
    if isinstance(payload, dict):
        if key in payload:
            return payload[key]
        for value in payload.values():
            nested = _extract_value(value, key)
            if nested is not None:
                return nested
        return None

    if hasattr(payload, key):
        return getattr(payload, key)

    if hasattr(payload, "__dict__"):
        for value in vars(payload).values():
            nested = _extract_value(value, key)
            if nested is not None:
                return nested
    return None


def _extract_scalar(payload: Any, candidate_keys: set[str]) -> str:
    for key in candidate_keys:
        value = _extract_value(payload, key)
        if value is None:
            continue
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        if isinstance(value, str):
            return value
    return ""


def _extract_binary_payload(payload: Any) -> bytes | None:
    if isinstance(payload, bytes):
        return payload

    if isinstance(payload, str):
        return _maybe_decode_base64(payload)

    for key in (
        "content",
        "documentContent",
        "document",
        "binaryData",
        "data",
        "return",
        "_value_1",
    ):
        value = _extract_value(payload, key)
        if value is None:
            continue
        binary = _extract_binary_payload(value)
        if binary is not None:
            return binary
    return None


def _maybe_decode_base64(value: str) -> bytes | None:
    compact = "".join(value.split())
    if not compact or compact.startswith("<"):
        return None
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        return None


def _fault_says_no_result(exc: Fault) -> bool:
    text = str(exc).lower()
    markers = ["not found", "no result", "does not exist", "unknown"]
    return any(marker in text for marker in markers)


def _default_filename(
    reference: PatentReference, document: WipoDocumentRef, content_type: str
) -> str:
    extension = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "image/tiff": ".tif",
    }.get(content_type, ".bin")
    return f"{reference.normalized_number}_{document.document_code}{extension}"
