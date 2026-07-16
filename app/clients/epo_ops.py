import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentBasicInfo, PatentOriginalFile, PatentReference
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

    async def fetch_bibliographic_data(self, reference: PatentReference) -> str:
        return await self._get_xml(
            path=self.build_biblio_path(reference),
            accept="application/exchange+xml",
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
        async with httpx.AsyncClient(
            base_url=self._settings.epo_ops_base_url,
            follow_redirects=True,
            timeout=self._settings.request_timeout_seconds,
        ) as client:
            response = await client.get(path, headers=headers)
        self._raise_for_error(response, source=source)
        return response.text

    async def _get_access_token(self) -> str:
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

        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self._settings.request_timeout_seconds
        ) as client:
            response = await client.post(
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
        self._access_token_expires_at = time.time() + max(int(expires_in) - 30, 30)
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
        title_node, title_language, _ = _select_language_node(
            _all_local(exchange_document.iter(), "invention-title")
        )
        abstract_node, abstract_language, _ = _select_language_node(
            _all_local(exchange_document.iter(), "abstract")
        )

        basic_info = PatentBasicInfo(
            title=_joined_text(title_node),
            abstract=_joined_text(abstract_node),
            publication_date=publication_doc.get("selected_date", ""),
            application_number=application_doc.get("selected_number", ""),
            applicants=_party_names(exchange_document, "applicants", "applicant"),
            inventors=_party_names(exchange_document, "inventors", "inventor"),
            ipc=_classification_values(exchange_document, "classification-ipcr"),
            cpc=_classification_values(exchange_document, "patent-classification"),
        )
        raw_refs = {
            "publication_reference": publication_doc,
            "application_reference": application_doc,
            "title_language": title_language,
            "abstract_language": abstract_language,
            "first_priority_date": _first_priority_date(exchange_document),
        }
        return basic_info, raw_refs

    @staticmethod
    def parse_family_international_filing_date(
        xml_text: str,
    ) -> tuple[str | None, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        wo_members: list[dict[str, str]] = []
        for family_member in _all_local(root.iter(), "family-member"):
            publication = _collect_document_references(
                family_member, "publication-reference", reference_kind="publication"
            )
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
        return (filing_dates[0] if filing_dates else None), {"wo_members": wo_members}

    @staticmethod
    def parse_description_data(
        xml_text: str,
    ) -> tuple[EpoDescriptionContent, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        description_node, language, available_languages = _select_language_node(
            _all_local(root.iter(), "description")
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
    ) -> tuple[EpoClaimsContent, dict[str, Any]]:
        root = _parse_xml(xml_text, source="epo")
        claims_node, language, available_languages = _select_language_node(
            _all_local(root.iter(), "claims")
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
            if desc == "FullDocument" and "application/pdf" in formats:
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
            content_type="application/pdf",
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
        or {}
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
    return ""


def _select_language_node(
    nodes: list[ET.Element],
) -> tuple[ET.Element | None, str | None, list[str]]:
    if not nodes:
        return None, None, []

    available_languages = [
        language for language in (_normalize_language(node.attrib.get("lang")) for node in nodes) if language
    ]
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
