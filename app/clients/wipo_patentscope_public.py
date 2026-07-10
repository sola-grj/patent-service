import asyncio
import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

import requests
import xml.etree.ElementTree as ET

from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentReference,
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_BLOCK_TAG_PATTERN = re.compile(
    r"(?i)<\s*(?:br|/p|/div|/li|/tr|/td|/th|/h1|/h2|/h3|/h4|/section|/article)\s*/?>"
)
_SCRIPT_STYLE_PATTERN = re.compile(
    r"(?is)<(script|style)\b.*?>.*?</\1>"
)
_TAG_PATTERN = re.compile(r"(?is)<[^>]+>")
_VIEW_STATE_PATTERN = re.compile(
    r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"'
)
_FORM_ID_PATTERN = re.compile(
    r'<form\b[^>]*id="([^"]+)"[^>]*>.*?name="javax\.faces\.ViewState"',
    re.IGNORECASE | re.DOTALL,
)
_PRIMEFACES_CALL_PATTERN = re.compile(r"PrimeFaces\.ab\(\{(.*?)\}\)", re.DOTALL)
_JS_ARG_PATTERN = re.compile(r'([sfu]):(?:&quot;|")(.+?)(?:&quot;|")')
_LANG_PREFIX_PATTERN = re.compile(r"^\[(?P<lang>[A-Z]{2})\]\s*(?P<text>.+)$")
_LANG_PREFIX_PATTERN_ALT = re.compile(
    r"^\((?P<lang>[A-Z]{2})\)\s*(?P<text>.+)$"
)
_APPLICANT_SUFFIX_PATTERN = re.compile(r"\s*\[[A-Z]{2}\]/\[[A-Z]{2}\]\s*$")
_APPLICANT_SUFFIX_COMPACT_PATTERN = re.compile(r"(?<!\s)(\[[A-Z]{2}\]/\[[A-Z]{2}\])$")
_IPC_CODE_PATTERN = re.compile(r"\b[A-HY]\d{2}[A-Z]\s+\d+/\d+(?:\s+\d{4}\.\d+)?\b")
_CPC_CODE_PATTERN = re.compile(r"\b[A-HY]\d{2}[A-Z]\s+\d+/\d+\b")
_SECTION_LABELS = {
    "publication number": "Publication Number",
    "publication date": "Publication Date",
    "international application no.": "International Application No.",
    "international filing date": "International Filing Date",
    "title": "Title",
    "ipc": "IPC",
    "cpc": "CPC",
    "applicants": "Applicants",
    "inventors": "Inventors",
    "agents": "Agents",
    "abstract": "Abstract",
}
_CRITICAL_SECTION_NAMES = {
    "Publication Number",
    "International Application No.",
    "Title",
    "Abstract",
}
_NO_RESULT_MARKERS = (
    "no records found",
    "no result",
    "document not found",
    "record not found",
)
_RATE_LIMIT_MARKERS = (
    "access denied",
    "too many requests",
    "temporarily blocked",
)


class WipoPatentScopePublicClient:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: Callable[[], requests.Session] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory or requests.Session

    async def lookup_patent(
        self, reference: PatentReference, *, include_original_file: bool
    ) -> PatentLookupResponse:
        del include_original_file

        doc_id = f"WO{reference.doc_number}"
        detail_url = self._build_detail_url(doc_id)
        html_text, fetch_strategy = await asyncio.to_thread(
            self._fetch_detail_page, detail_url
        )
        parsed = self.parse_patent_details(html_text)

        raw_source_refs = {
            "lookup_mode": "public_page",
            "detail_url": detail_url,
            "doc_id": doc_id,
            "fetch_strategy": fetch_strategy,
            "publication_number": parsed["publication_number"],
            "application_filing_date": parsed["application_filing_date"],
            "agents": parsed["agents"],
            "applicants_raw_blocks": parsed["applicants_raw_blocks"],
            "inventors_raw_blocks": parsed["inventors_raw_blocks"],
        }

        return PatentLookupResponse(
            source=reference.source,
            normalized_number=reference.normalized_number,
            display_number=reference.display_number,
            basic_info=PatentBasicInfo(
                title=parsed["title"],
                abstract=parsed["abstract"],
                publication_date=parsed["publication_date"],
                application_number=parsed["application_number"],
                applicants=parsed["applicants"],
                inventors=parsed["inventors"],
                ipc=parsed["ipc"],
                cpc=parsed["cpc"],
            ),
            original_file=PatentOriginalFile(),
            raw_source_refs=raw_source_refs,
        )

    def _build_detail_url(self, doc_id: str) -> str:
        base_url = self._settings.wipo_public_base_url.rstrip("/")
        query = urlencode({"docId": doc_id})
        return f"{base_url}/detail.jsf?{query}"

    def _fetch_detail_page(self, detail_url: str) -> tuple[str, str]:
        session = self._session_factory()
        try:
            first_response = session.get(
                detail_url,
                headers=self._request_headers(),
                timeout=self._settings.request_timeout_seconds,
            )
            self._raise_for_response(first_response)
            if _is_no_result_page(first_response.text):
                raise _source_no_result(detail_url)

            second_response = session.get(
                detail_url,
                headers=self._request_headers(referer=detail_url),
                timeout=self._settings.request_timeout_seconds,
            )
            self._raise_for_response(second_response)
            if _is_no_result_page(second_response.text):
                raise _source_no_result(detail_url)
            if self._has_bibliographic_content(second_response.text):
                return second_response.text, "second_get"

            postback_html = self._fetch_biblio_via_postback(
                session=session,
                detail_url=detail_url,
                html_text=second_response.text,
            )
            if postback_html and self._has_bibliographic_content(postback_html):
                return postback_html, "jsf_postback"
        except requests.RequestException as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="WIPO PATENTSCOPE public page request failed.",
                source="wipo",
                details={"detail_url": detail_url, "error": str(exc)},
            ) from exc
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

        raise PatentServiceError(
            code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
            status_code=502,
            message="WIPO PATENTSCOPE public page did not expose a bibliographic panel.",
            source="wipo",
            details={"detail_url": detail_url},
        )

    def _fetch_biblio_via_postback(
        self,
        *,
        session: requests.Session,
        detail_url: str,
        html_text: str,
    ) -> str | None:
        view_state = _extract_view_state(html_text)
        form_id = _extract_form_id(html_text)
        if not view_state or not form_id:
            return None

        postback_target = _choose_postback_target(html_text)
        if postback_target is None:
            return None

        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": postback_target["source"],
            "javax.faces.partial.execute": postback_target["source"],
            "javax.faces.partial.render": postback_target["update"],
            postback_target["source"]: postback_target["source"],
            form_id: form_id,
            "javax.faces.ViewState": view_state,
        }

        response = session.post(
            detail_url,
            data=payload,
            headers=self._request_headers(
                referer=detail_url,
                extra={
                    "Faces-Request": "partial/ajax",
                    "X-Requested-With": "XMLHttpRequest",
                },
            ),
            timeout=self._settings.request_timeout_seconds,
        )
        self._raise_for_response(response)
        if _is_no_result_page(response.text):
            raise _source_no_result(detail_url)

        if "<partial-response" in response.text:
            return _extract_partial_response_html(response.text)
        return response.text

    def _request_headers(
        self,
        *,
        referer: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        if extra:
            headers.update(extra)
        return headers

    def _raise_for_response(self, response: requests.Response) -> None:
        if response.status_code in {403, 429}:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_RATE_LIMIT,
                status_code=503,
                message="WIPO PATENTSCOPE public page rejected the request.",
                source="wipo",
                details={"status_code": response.status_code, "url": response.url},
            )

        if _is_captcha_page(response.text):
            raise PatentServiceError(
                code=ErrorCode.SOURCE_RATE_LIMIT,
                status_code=503,
                message="WIPO PATENTSCOPE public page returned a CAPTCHA challenge.",
                source="wipo",
                details={
                    "status_code": response.status_code,
                    "url": response.url,
                    "block_type": "captcha",
                },
            )

        lowered = response.text.lower()
        if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
            raise PatentServiceError(
                code=ErrorCode.SOURCE_RATE_LIMIT,
                status_code=503,
                message="WIPO PATENTSCOPE public page appears to have blocked the request.",
                source="wipo",
                details={"status_code": response.status_code, "url": response.url},
            )

        if response.status_code == 404:
            raise _source_no_result(response.url)

        if response.status_code >= 400:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="WIPO PATENTSCOPE public page request failed.",
                source="wipo",
                details={"status_code": response.status_code, "url": response.url},
            )

    @staticmethod
    def _has_bibliographic_content(html_text: str) -> bool:
        structured_fields = _collect_structured_fields(html_text)
        if structured_fields:
            return len(_CRITICAL_SECTION_NAMES.intersection(structured_fields)) >= 3
        sections = _collect_sections(_html_to_lines(html_text))
        return len(_CRITICAL_SECTION_NAMES.intersection(sections)) >= 3

    @staticmethod
    def parse_patent_details(html_text: str) -> dict[str, Any]:
        structured_fields = _collect_structured_fields(html_text)
        if len(_CRITICAL_SECTION_NAMES.intersection(structured_fields)) >= 3:
            return _parse_structured_patent_details(
                structured_fields=structured_fields,
                html_text=html_text,
            )

        lines = _html_to_lines(html_text)
        sections = _collect_sections(lines)
        if len(_CRITICAL_SECTION_NAMES.intersection(sections)) < 3:
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="WIPO PATENTSCOPE public page is missing required bibliographic fields.",
                source="wipo",
            )

        title_lines = sections.get("Title", [])
        abstract_lines = sections.get("Abstract", [])
        applicants_raw_blocks = _group_party_blocks(
            sections.get("Applicants", []),
            kind="applicant",
        )
        inventors_raw_blocks = _group_party_blocks(
            sections.get("Inventors", []),
            kind="inventor",
        )
        agents_raw_blocks = _group_party_blocks(
            sections.get("Agents", []),
            kind="agent",
        )

        title = _select_preferred_language_text(title_lines)
        if not title:
            title = _extract_title_from_header(lines)

        return {
            "publication_number": _first_non_empty(sections.get("Publication Number", [])),
            "publication_date": _normalize_date(
                _first_non_empty(sections.get("Publication Date", []))
            ),
            "application_number": _first_non_empty(
                sections.get("International Application No.", [])
            ),
            "application_filing_date": _normalize_date(
                _first_non_empty(sections.get("International Filing Date", []))
            ),
            "title": title,
            "abstract": _select_preferred_language_text(abstract_lines),
            "applicants": _block_names(applicants_raw_blocks, strip_applicant_suffix=True),
            "inventors": _block_names(inventors_raw_blocks),
            "agents": [" ".join(block) for block in agents_raw_blocks if block],
            "ipc": _normalize_codes(sections.get("IPC", [])),
            "cpc": _normalize_codes(sections.get("CPC", [])),
            "applicants_raw_blocks": ["\n".join(block) for block in applicants_raw_blocks],
            "inventors_raw_blocks": ["\n".join(block) for block in inventors_raw_blocks],
        }


def _source_no_result(url: str) -> PatentServiceError:
    return PatentServiceError(
        code=ErrorCode.SOURCE_NO_RESULT,
        status_code=404,
        message="No publication was found in WIPO PATENTSCOPE.",
        source="wipo",
        details={"url": url},
    )


def _is_no_result_page(html_text: str) -> bool:
    lowered = html_text.lower()
    return any(marker in lowered for marker in _NO_RESULT_MARKERS)


def _is_captcha_page(html_text: str) -> bool:
    lowered = html_text.lower()
    return (
        "id=\"pscaptchaform\"" in lowered
        or "name=\"pscaptchaform\"" in lowered
        or "id=\"pscaptchapanel\"" in lowered
        or "please select the picture with" in lowered
    )


@dataclass(slots=True)
class _StructuredPartyItem:
    name: str
    raw_text: str


@dataclass(slots=True)
class _StructuredField:
    label_parts: list[str] = field(default_factory=list)
    value_parts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    parties: list[_StructuredPartyItem] = field(default_factory=list)
    _current_li_raw_parts: list[str] | None = None
    _current_li_name_parts: list[str] | None = None

    def label(self) -> str:
        return _normalize_inline_text("".join(self.label_parts))

    def lines(self) -> list[str]:
        return _text_to_lines("".join(self.value_parts))

    def full_text(self) -> str:
        return "\n".join(self.lines())

    def begin_party(self) -> None:
        self._current_li_raw_parts = []
        self._current_li_name_parts = []

    def append_party_text(self, value: str) -> None:
        if self._current_li_raw_parts is not None:
            self._current_li_raw_parts.append(value)

    def append_party_name_text(self, value: str) -> None:
        if self._current_li_name_parts is not None:
            self._current_li_name_parts.append(value)

    def finalize_party(self) -> None:
        if self._current_li_raw_parts is None:
            return

        raw_text = _normalize_party_raw_text("".join(self._current_li_raw_parts))
        name = _normalize_inline_text("".join(self._current_li_name_parts or []))
        if raw_text or name:
            self.parties.append(_StructuredPartyItem(name=name, raw_text=raw_text or name))
        self._current_li_raw_parts = None
        self._current_li_name_parts = None


class _BiblioFieldHtmlParser(HTMLParser):
    _BLOCK_TAGS = {"br", "div", "li", "p", "tr", "td", "th", "ul", "ol", "table"}
    _VOID_TAGS = {"br", "hr", "img", "input", "link", "meta"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, _StructuredField] = {}
        self._current_field: _StructuredField | None = None
        self._field_depth = 0
        self._in_label_depth = 0
        self._in_value_depth = 0
        self._in_name_depth = 0
        self._in_anchor_depth = 0
        self._current_link_parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        class_name = attributes.get("class", "")

        if self._current_field is None and tag == "div" and _is_biblio_field_class(class_name):
            self._current_field = _StructuredField()
            self._field_depth = 1
            return

        if self._current_field is None:
            return

        is_void_tag = tag in self._VOID_TAGS
        if not is_void_tag:
            self._field_depth += 1
            if self._in_label_depth > 0:
                self._in_label_depth += 1
            if self._in_value_depth > 0:
                self._in_value_depth += 1
            if self._in_name_depth > 0:
                self._in_name_depth += 1
            if self._in_anchor_depth > 0:
                self._in_anchor_depth += 1

        if tag == "span" and _is_biblio_label_class(class_name):
            self._in_label_depth = 1
            return

        if tag == "span" and _is_biblio_value_class(class_name):
            self._in_value_depth = 1
            return

        if self._in_value_depth > 0 and tag in self._BLOCK_TAGS:
            self._current_field.value_parts.append("\n")
            self._current_field.append_party_text("\n")

        if self._in_value_depth > 0 and tag == "li":
            self._current_field.begin_party()
            return

        if self._in_value_depth > 0 and tag == "span" and "biblio-person-list--name" in class_name:
            self._in_name_depth = 1
            return

        if self._in_value_depth > 0 and tag == "a":
            self._in_anchor_depth = 1
            self._current_link_parts = []

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._current_field is None:
            return

        if self._in_anchor_depth > 0:
            self._in_anchor_depth -= 1
            if self._in_anchor_depth == 0 and self._current_link_parts is not None:
                link_text = _normalize_inline_text("".join(self._current_link_parts))
                if link_text:
                    self._current_field.links.append(link_text)
                self._current_link_parts = None

        if self._in_name_depth > 0:
            self._in_name_depth -= 1

        if self._in_value_depth > 0 and tag in self._BLOCK_TAGS:
            self._current_field.value_parts.append("\n")
            self._current_field.append_party_text("\n")

        if self._in_value_depth > 0 and tag == "li":
            self._current_field.finalize_party()

        if self._in_value_depth > 0:
            self._in_value_depth -= 1

        if self._in_label_depth > 0:
            self._in_label_depth -= 1

        self._field_depth -= 1
        if self._field_depth == 0:
            self._current_field.finalize_party()
            label = self._current_field.label()
            if label:
                self.fields[label] = self._current_field
            self._current_field = None

    def handle_data(self, data: str) -> None:
        if self._current_field is None:
            return

        if self._in_label_depth > 0:
            self._current_field.label_parts.append(data)

        if self._in_value_depth > 0:
            self._current_field.value_parts.append(data)
            self._current_field.append_party_text(data)
            if self._in_name_depth > 0:
                self._current_field.append_party_name_text(data)
            if self._in_anchor_depth > 0 and self._current_link_parts is not None:
                self._current_link_parts.append(data)


def _html_to_lines(html_text: str) -> list[str]:
    text = _SCRIPT_STYLE_PATTERN.sub("\n", html_text)
    text = _BLOCK_TAG_PATTERN.sub("\n", text)
    text = _TAG_PATTERN.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\r", "")
    lines = []
    for raw_line in text.split("\n"):
        cleaned = " ".join(raw_line.replace("\xa0", " ").split())
        lines.append(cleaned)
    return lines


def _collect_sections(lines: list[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current_label: str | None = None
    for line in lines:
        lowered = line.lower().strip()
        if lowered in _SECTION_LABELS:
            current_label = _SECTION_LABELS[lowered]
            sections.setdefault(current_label, [])
            continue

        if current_label is None:
            continue
        sections[current_label].append(line)
    return sections


def _collect_structured_fields(html_text: str) -> dict[str, _StructuredField]:
    parser = _BiblioFieldHtmlParser()
    parser.feed(html_text)
    parser.close()
    return parser.fields


def _parse_structured_patent_details(
    *,
    structured_fields: dict[str, _StructuredField],
    html_text: str,
) -> dict[str, Any]:
    title = _select_preferred_language_text(_field_lines(structured_fields, "Title"))
    if not title:
        title = _extract_title_from_header(_html_to_lines(html_text))

    abstract = _select_preferred_language_text(_field_lines(structured_fields, "Abstract"))
    applicants_raw_blocks = _party_raw_blocks(structured_fields.get("Applicants"))
    inventors_raw_blocks = _party_raw_blocks(structured_fields.get("Inventors"))
    agents = _party_raw_blocks(structured_fields.get("Agents"))

    return {
        "publication_number": _field_first_line(structured_fields, "Publication Number"),
        "publication_date": _normalize_date(
            _field_first_line(structured_fields, "Publication Date")
        ),
        "application_number": _field_first_line(
            structured_fields, "International Application No."
        ),
        "application_filing_date": _normalize_date(
            _field_first_line(structured_fields, "International Filing Date")
        ),
        "title": title,
        "abstract": abstract,
        "applicants": _party_names(
            structured_fields.get("Applicants"),
            strip_applicant_suffix=True,
        ),
        "inventors": _party_names(structured_fields.get("Inventors")),
        "agents": agents,
        "ipc": _extract_codes_from_field(
            structured_fields.get("IPC"),
            pattern=_IPC_CODE_PATTERN,
        ),
        "cpc": _extract_codes_from_field(
            structured_fields.get("CPC"),
            pattern=_CPC_CODE_PATTERN,
        ),
        "applicants_raw_blocks": applicants_raw_blocks,
        "inventors_raw_blocks": inventors_raw_blocks,
    }


def _split_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)

    if current:
        blocks.append(current)

    if blocks:
        return blocks
    if lines:
        compact = [line for line in lines if line]
        return [compact] if compact else []
    return []


def _group_party_blocks(lines: list[str], *, kind: str) -> list[list[str]]:
    blocks = _split_blocks(lines)
    if len(blocks) <= 1:
        compact_lines = [line for line in lines if line]
        return _merge_party_lines(compact_lines, kind=kind)

    flattened = [block for block in blocks if block]
    if flattened and all(len(block) == 1 for block in flattened):
        compact_lines = [block[0] for block in flattened]
        return _merge_party_lines(compact_lines, kind=kind)
    return flattened


def _merge_party_lines(lines: list[str], *, kind: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if not current:
            current = [line]
            continue
        if _starts_new_party_block(line, current=current, kind=kind):
            blocks.append(current)
            current = [line]
            continue
        current.append(line)

    if current:
        blocks.append(current)
    return blocks


def _starts_new_party_block(
    line: str,
    *,
    current: list[str],
    kind: str,
) -> bool:
    if kind == "inventor":
        return bool("," in line and not any(char.isdigit() for char in line))
    if kind == "applicant":
        if "[" in line and "]" in line:
            return True
        if line.isupper() and not any(char.isdigit() for char in line):
            return True
        return False
    if kind == "agent":
        return line.isupper() and not any(char.isdigit() for char in line) and bool(current)
    return False


def _block_names_impl(
    blocks: list[list[str]], *, strip_applicant_suffix: bool
) -> list[str]:
    names: list[str] = []
    for block in blocks:
        if block:
            value = block[0]
            if strip_applicant_suffix:
                value = _strip_applicant_suffix(value)
            names.append(value)
    return names


def _block_names(
    blocks: list[list[str]], *, strip_applicant_suffix: bool = False
) -> list[str]:
    return _block_names_impl(blocks, strip_applicant_suffix=strip_applicant_suffix)


def _normalize_codes(lines: list[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        compact = " ".join(line.split())
        if compact and compact not in values:
            values.append(compact)
    return values


def _field_lines(fields: dict[str, _StructuredField], label: str) -> list[str]:
    field = fields.get(label)
    return field.lines() if field else []


def _field_first_line(fields: dict[str, _StructuredField], label: str) -> str:
    return _first_non_empty(_field_lines(fields, label))


def _party_names(
    field: _StructuredField | None,
    *,
    strip_applicant_suffix: bool = False,
) -> list[str]:
    if field is None:
        return []

    values: list[str] = []
    for party in field.parties:
        value = party.name or party.raw_text
        value = _normalize_inline_text(value)
        if strip_applicant_suffix:
            value = _strip_applicant_suffix(value)
        if value and value not in values:
            values.append(value)
    return values


def _party_raw_blocks(field: _StructuredField | None) -> list[str]:
    if field is None:
        return []

    values: list[str] = []
    for party in field.parties:
        value = _normalize_inline_text(party.raw_text)
        if value and value not in values:
            values.append(value)
    return values


def _extract_codes_from_field(
    field: _StructuredField | None,
    *,
    pattern: re.Pattern[str],
) -> list[str]:
    if field is None:
        return []

    text = field.full_text()
    values: list[str] = []
    for match in pattern.finditer(text):
        value = " ".join(match.group(0).split())
        _append_code_value(values, value)
    for link in field.links:
        value = " ".join(link.split())
        if value and pattern.fullmatch(value):
            _append_code_value(values, value)
    return values


def _first_non_empty(lines: list[str]) -> str:
    for line in lines:
        if line:
            return line
    return ""


def _select_preferred_language_text(lines: list[str]) -> str:
    english_fallback = ""
    first_value = ""
    for line in lines:
        match = _LANG_PREFIX_PATTERN.match(line)
        if not match:
            match = _LANG_PREFIX_PATTERN_ALT.match(line)
        if match:
            if match.group("lang") == "EN":
                return match.group("text").strip()
            if not first_value:
                first_value = match.group("text").strip()
            continue
        if line and not first_value:
            first_value = line
        if line and not english_fallback:
            english_fallback = line
    return first_value or english_fallback


def _extract_title_from_header(lines: list[str]) -> str:
    for line in lines:
        if " - " not in line:
            continue
        prefix, title = line.split(" - ", 1)
        if "WO" not in prefix:
            continue
        return title.strip()
    return ""


def _normalize_date(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"\d{8}", value):
        return value
    match = re.fullmatch(r"(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})", value)
    if match:
        return f"{match.group('year')}{match.group('month')}{match.group('day')}"
    return value


def _is_biblio_field_class(class_name: str) -> bool:
    classes = set(class_name.split())
    return "ps-field" in classes and "ps-biblio-field" in classes


def _is_biblio_label_class(class_name: str) -> bool:
    return "ps-field--label" in class_name.split()


def _is_biblio_value_class(class_name: str) -> bool:
    return "ps-field--value" in class_name.split()


def _normalize_inline_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _normalize_party_raw_text(value: str) -> str:
    normalized = _normalize_inline_text(value)
    return _APPLICANT_SUFFIX_COMPACT_PATTERN.sub(r" \1", normalized)


def _text_to_lines(value: str) -> list[str]:
    text = html.unescape(value).replace("\r", "")
    lines: list[str] = []
    for raw_line in text.split("\n"):
        cleaned = _normalize_inline_text(raw_line)
        if cleaned:
            lines.append(cleaned)
    return lines


def _strip_applicant_suffix(value: str) -> str:
    return _APPLICANT_SUFFIX_PATTERN.sub("", value).strip()


def _append_code_value(values: list[str], value: str) -> None:
    if not value:
        return

    for index, existing in enumerate(values):
        if existing == value:
            return
        if existing.startswith(f"{value} "):
            return
        if value.startswith(f"{existing} "):
            values[index] = value
            return
    values.append(value)


def _extract_view_state(html_text: str) -> str:
    match = _VIEW_STATE_PATTERN.search(html_text)
    return html.unescape(match.group(1)) if match else ""


def _extract_form_id(html_text: str) -> str:
    match = _FORM_ID_PATTERN.search(html_text)
    return match.group(1) if match else ""


def _choose_postback_target(html_text: str) -> dict[str, str] | None:
    best_match: tuple[int, dict[str, str]] | None = None
    for match in _PRIMEFACES_CALL_PATTERN.finditer(html_text):
        argument_text = match.group(1)
        args = {name: value for name, value in _JS_ARG_PATTERN.findall(argument_text)}
        if not {"s", "f", "u"} <= args.keys():
            continue

        source = html.unescape(args["s"])
        form = html.unescape(args["f"])
        update = html.unescape(args["u"])
        score = 0
        candidate_text = f"{source} {form} {update}".lower()
        if "biblio" in candidate_text:
            score += 5
        if "detail" in candidate_text:
            score += 3
        if "tab" in candidate_text:
            score += 2

        nearby = html_text[max(0, match.start() - 400) : match.end() + 400].lower()
        if "pct biblio" in nearby:
            score += 6
        if "publication number" in nearby:
            score += 4
        if best_match is None or score > best_match[0]:
            best_match = (
                score,
                {
                    "source": source,
                    "form": form,
                    "update": update,
                },
            )

    return best_match[1] if best_match else None


def _extract_partial_response_html(response_text: str) -> str:
    try:
        root = ET.fromstring(response_text)
    except ET.ParseError:
        return response_text

    parts: list[str] = []
    for update in root.iter():
        if update.tag.endswith("update"):
            if update.text and update.text.strip():
                parts.append(update.text)
    return "\n".join(parts) if parts else response_text
