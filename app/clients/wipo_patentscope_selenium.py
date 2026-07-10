from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse

from app.clients.wipo_patentscope_public import WipoPatentScopePublicClient
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentDrawingsInfo,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentReference,
)
from app.utils.text_metrics import count_words, extract_drawing_labels

_DETAIL_PATH = "detail.jsf"
_CAPTCHA_MARKERS = (
    'id="pscaptchaform"',
    'name="pscaptchaform"',
    "please select the picture with",
)
_FORBIDDEN_MARKERS = ("403 forbidden", "internal error reference p")
_BIBLIO_MARKERS = (
    "publication number",
    "international application no.",
    "title",
    "abstract",
)
_SHELL_MARKERS = ("please wait",)
_OCR_NOTICE_MARKERS = (
    "note: text based on automatic optical character recognition processes",
    "text based on automatic optical character recognition processes",
)
_DIMENSION_LINE_PATTERN = re.compile(r"^\d+\s*[xX]\s*\d+$")
_DATE_LINE_PATTERN = re.compile(r"^\d{4}\s+\d{2}\s+\d{2}$")
_SLASH_CODE_PATTERN = re.compile(r"^[A-Za-z]{1,4}/[A-Za-z]{1,4}$")
_UPPERCASE_HEADING_PATTERN = re.compile(r"^[A-Z][A-Z0-9\s./()_-]{4,}$")
_CLAIM_LINE_PATTERN = re.compile(
    r"^(?:\[(?P<bracket>\d+)\]|(?P<number>\d+)\s*[.)])\s+"
)
_DRAWING_FIGURE_PATTERN = re.compile(
    r"^(?:FIG(?:URE)?S?\.?\s*\d+|FIG(?:URE)?S?\.?)",
    re.IGNORECASE,
)
_TAB_SPECS = (
    ("description", "Description", ("PCTDESCRIPTION",)),
    ("claims", "Claims", ("PCTCLAIMS",)),
    ("drawings", "Drawings", ("DRAWINGS",)),
    ("documents", "Documents", ("PTDOCUMENTS", "PCTDOCUMENTS")),
)
_TAB_LABEL_LINES = {"description", "claims", "drawings", "documents"}


@dataclass(slots=True)
class SeleniumLink:
    text: str
    href: str


@dataclass(slots=True)
class SeleniumTabSnapshot:
    html: str = ""
    text: str = ""
    item_count: int = 0
    links: list[SeleniumLink] = field(default_factory=list)


@dataclass(slots=True)
class SeleniumFetchResult:
    state: str
    current_url: str
    title: str
    page_source: str
    tabs: dict[str, SeleniumTabSnapshot] = field(default_factory=dict)


def build_detail_url(*, base_url: str, doc_id: str, cid: str | None = None) -> str:
    query: dict[str, str] = {"docId": doc_id}
    if cid:
        query["_cid"] = cid
    return f"{base_url.rstrip('/')}/{_DETAIL_PATH}?{urlencode(query)}"


def classify_page_source(page_source: str) -> str:
    lowered = page_source.lower()
    if any(marker in lowered for marker in _CAPTCHA_MARKERS):
        return "captcha"
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        return "forbidden"
    if all(marker in lowered for marker in _BIBLIO_MARKERS):
        return "bibliographic"
    if any(marker in lowered for marker in _SHELL_MARKERS):
        return "shell"
    return "unknown"


class WipoPatentScopeSeleniumClient:
    def __init__(
        self,
        settings: Settings,
        *,
        page_fetcher: Callable[[str], SeleniumFetchResult] | None = None,
    ) -> None:
        self._settings = settings
        self._page_fetcher = page_fetcher or self._fetch_detail_page

    async def lookup_patent(
        self, reference: PatentReference, *, include_original_file: bool
    ) -> PatentLookupResponse:
        doc_id = f"WO{reference.doc_number}"
        detail_url = build_detail_url(
            base_url=self._settings.wipo_public_base_url,
            doc_id=doc_id,
        )
        fetch_result = await asyncio.to_thread(self._page_fetcher, detail_url)

        if fetch_result.state == "captcha":
            raise PatentServiceError(
                code=ErrorCode.SOURCE_RATE_LIMIT,
                status_code=503,
                message="WIPO PATENTSCOPE public page returned a CAPTCHA challenge.",
                source="wipo",
                details={
                    "detail_url": detail_url,
                    "current_url": fetch_result.current_url,
                    "block_type": "captcha",
                    "fetch_strategy": "selenium",
                },
            )

        if fetch_result.state == "forbidden":
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="WIPO PATENTSCOPE public page rejected the Selenium browser session.",
                source="wipo",
                details={
                    "detail_url": detail_url,
                    "current_url": fetch_result.current_url,
                    "page_state": fetch_result.state,
                    "fetch_strategy": "selenium",
                },
            )

        if fetch_result.state != "bibliographic":
            raise PatentServiceError(
                code=ErrorCode.UPSTREAM_RESPONSE_INVALID,
                status_code=502,
                message="WIPO PATENTSCOPE Selenium fetch did not reach the bibliographic panel.",
                source="wipo",
                details={
                    "detail_url": detail_url,
                    "current_url": fetch_result.current_url,
                    "page_state": fetch_result.state,
                    "fetch_strategy": "selenium",
                },
            )

        parsed = WipoPatentScopePublicClient.parse_patent_details(fetch_result.page_source)
        description_text = _extract_panel_text(fetch_result.tabs.get("description"))
        claims_text = _extract_panel_text(fetch_result.tabs.get("claims"))
        description_lines = description_text.splitlines() if description_text else []
        claims_count = _count_claims(claims_text)
        drawings = _build_drawings_info(
            drawing_snapshot=fetch_result.tabs.get("drawings"),
            description_lines=description_lines,
        )
        original_file = (
            _select_original_file(
                doc_id=doc_id,
                current_url=fetch_result.current_url,
                document_snapshot=fetch_result.tabs.get("documents"),
            )
            if include_original_file
            else PatentOriginalFile()
        )

        raw_source_refs = {
            "lookup_mode": "public_page",
            "detail_url": detail_url,
            "current_url": fetch_result.current_url,
            "doc_id": doc_id,
            "fetch_strategy": "selenium",
            "publication_number": parsed["publication_number"],
            "application_filing_date": parsed["application_filing_date"],
            "agents": parsed["agents"],
            "applicants_raw_blocks": parsed["applicants_raw_blocks"],
            "inventors_raw_blocks": parsed["inventors_raw_blocks"],
            "description_tab": {
                "available": bool(description_text),
                "word_count": count_words(description_text) if description_text else 0,
            },
            "claims_tab": {
                "available": bool(claims_text),
                "claims_count": claims_count,
                "word_count": count_words(claims_text) if claims_text else 0,
            },
            "drawings_tab": {
                "has_drawings": drawings.has_drawings,
                "drawing_page_count": drawings.drawing_page_count,
                "drawing_labels": drawings.drawing_labels,
            },
            "documents_tab": {
                "links": _serialize_links(fetch_result.tabs.get("documents")),
            },
        }
        if original_file.download_url:
            raw_source_refs["published_application_download_url"] = (
                original_file.download_url
            )

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
            description_words=count_words(description_text) if description_text else None,
            claims_count=claims_count,
            claims_words=count_words(claims_text) if claims_text else None,
            drawings=drawings,
            original_file=original_file,
            raw_source_refs=raw_source_refs,
        )

    def _fetch_detail_page(self, detail_url: str) -> SeleniumFetchResult:
        try:
            from selenium import webdriver
            from selenium.common.exceptions import TimeoutException, WebDriverException
            from selenium.webdriver import ChromeOptions
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="Selenium runtime is not installed for WIPO public-page lookup.",
                source="wipo",
                details={"detail_url": detail_url, "dependency": "selenium"},
            ) from exc

        options = ChromeOptions()
        if self._settings.wipo_selenium_chrome_binary:
            options.binary_location = self._settings.wipo_selenium_chrome_binary
        if self._settings.wipo_selenium_headless:
            options.add_argument("--headless=new")
        else:
            options.add_argument("--start-minimized")
            options.add_argument("--window-position=-2400,0")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        options.add_argument("--remote-allow-origins=*")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1600,1200")
        options.add_argument("--lang=zh-CN")

        timeout_seconds = self._settings.wipo_selenium_timeout_seconds
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(), options=options)
            driver.set_page_load_timeout(timeout_seconds)
            wait = WebDriverWait(driver, timeout_seconds)
            if not self._settings.wipo_selenium_headless:
                try:
                    driver.set_window_position(-2400, 0)
                    driver.minimize_window()
                except Exception:
                    pass

            driver.get(detail_url)
            self._wait_for_terminal_state(wait, classify_page_source)

            state = classify_page_source(driver.page_source)
            if state == "shell":
                driver.refresh()
                self._wait_for_terminal_state(wait, classify_page_source)
                state = classify_page_source(driver.page_source)

            tabs: dict[str, SeleniumTabSnapshot] = {}
            if state == "bibliographic":
                tabs = self._capture_tabs(driver, by=By)

            return SeleniumFetchResult(
                state=state,
                current_url=driver.current_url,
                title=driver.title,
                page_source=driver.page_source,
                tabs=tabs,
            )
        except PatentServiceError:
            raise
        except (TimeoutException, WebDriverException) as exc:
            raise PatentServiceError(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                status_code=503,
                message="WIPO PATENTSCOPE Selenium fetch failed.",
                source="wipo",
                details={"detail_url": detail_url, "error": str(exc)},
            ) from exc
        finally:
            if driver is not None:
                driver.quit()

    def _capture_tabs(self, driver: Any, *, by: Any) -> dict[str, SeleniumTabSnapshot]:
        snapshots: dict[str, SeleniumTabSnapshot] = {}
        for key, label, panel_suffixes in _TAB_SPECS:
            snapshots[key] = self._capture_tab_snapshot(
                driver,
                by=by,
                label=label,
                panel_suffixes=panel_suffixes,
                current_url=driver.current_url,
            )
        return snapshots

    def _capture_tab_snapshot(
        self,
        driver: Any,
        *,
        by: Any,
        label: str,
        panel_suffixes: tuple[str, ...],
        current_url: str,
    ) -> SeleniumTabSnapshot:
        panel = self._activate_tab(
            driver,
            by=by,
            label=label,
            panel_suffixes=panel_suffixes,
        )
        if panel is None:
            return SeleniumTabSnapshot()

        deadline = time.monotonic() + min(self._settings.wipo_selenium_timeout_seconds, 6.0)
        snapshot = self._snapshot_panel(panel, by=by, current_url=current_url)
        while time.monotonic() < deadline:
            if _tab_snapshot_ready(label=label, snapshot=snapshot):
                break
            time.sleep(0.2)
            snapshot = self._snapshot_panel(panel, by=by, current_url=current_url)
        return snapshot

    @staticmethod
    def _activate_tab(
        driver: Any,
        *,
        by: Any,
        label: str,
        panel_suffixes: tuple[str, ...],
    ) -> Any | None:
        tab = WipoPatentScopeSeleniumClient._find_tab_control(driver, by=by, label=label)
        if tab is not None:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'nearest'});",
                tab,
            )
            driver.execute_script("arguments[0].click();", tab)
            time.sleep(0.8)
        return WipoPatentScopeSeleniumClient._find_panel(
            driver,
            by=by,
            panel_suffixes=panel_suffixes,
        )

    @staticmethod
    def _find_tab_control(driver: Any, *, by: Any, label: str) -> Any | None:
        candidates = driver.find_elements(
            by.XPATH,
            f"//*[self::a or self::span][normalize-space()='{label}']",
        )
        visible = next((element for element in candidates if element.is_displayed()), None)
        return visible or (candidates[0] if candidates else None)

    @staticmethod
    def _find_panel(driver: Any, *, by: Any, panel_suffixes: tuple[str, ...]) -> Any | None:
        candidates: list[Any] = []
        for suffix in panel_suffixes:
            candidates.extend(
                driver.find_elements(
                    by.CSS_SELECTOR,
                    f"[id$='{suffix}'][role='tabpanel']",
                )
            )
            candidates.extend(
                driver.find_elements(
                    by.CSS_SELECTOR,
                    f"[id$='{suffix}']",
                )
            )
        visible = next((element for element in candidates if element.is_displayed()), None)
        return visible or (candidates[0] if candidates else None)

    @staticmethod
    def _snapshot_panel(panel: Any, *, by: Any, current_url: str) -> SeleniumTabSnapshot:
        html = panel.get_attribute("innerHTML") or ""
        text = panel.text or ""
        links: list[SeleniumLink] = []
        for element in panel.find_elements(by.CSS_SELECTOR, "a[href]"):
            href = (element.get_attribute("href") or "").strip()
            if not href:
                continue
            links.append(
                SeleniumLink(
                    text=" ".join((element.text or "").split()),
                    href=urljoin(current_url, href),
                )
            )

        item_count = len(panel.find_elements(by.CSS_SELECTOR, ".ui-datalist-item"))
        if item_count == 0:
            item_count = len(panel.find_elements(by.CSS_SELECTOR, "img"))

        return SeleniumTabSnapshot(
            html=html,
            text=text,
            item_count=item_count,
            links=links,
        )

    @staticmethod
    def _wait_for_terminal_state(wait: Any, classifier: Callable[[str], str]) -> None:
        try:
            wait.until(
                lambda driver: classifier(driver.page_source)
                in {"bibliographic", "captcha", "forbidden", "unknown"}
            )
        except Exception:
            return


def _tab_snapshot_ready(*, label: str, snapshot: SeleniumTabSnapshot) -> bool:
    lowered_html = snapshot.html.lower()
    if label in {"Description", "Claims"}:
        return bool(snapshot.text.strip()) or "claims-description" in lowered_html
    if label == "Drawings":
        return snapshot.item_count > 0 or "ui-datalist" in lowered_html
    if label == "Documents":
        return bool(snapshot.links) or "download" in lowered_html or "datatable" in lowered_html
    return bool(snapshot.text.strip()) or bool(snapshot.html.strip())


def _extract_panel_text(snapshot: SeleniumTabSnapshot | None) -> str:
    if snapshot is None or not snapshot.text:
        return ""

    lines: list[str] = []
    for raw_line in snapshot.text.replace("\xa0", " ").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        lowered = line.lower()
        if any(lowered.startswith(marker) for marker in _OCR_NOTICE_MARKERS):
            continue
        if _DIMENSION_LINE_PATTERN.fullmatch(line):
            continue
        if _DATE_LINE_PATTERN.fullmatch(line):
            continue
        if _SLASH_CODE_PATTERN.fullmatch(line):
            continue
        lines.append(line)

    while lines and _UPPERCASE_HEADING_PATTERN.fullmatch(lines[0]):
        lines.pop(0)
    while lines and lines[0].strip().lower() in _TAB_LABEL_LINES:
        lines.pop(0)
    while lines and _SLASH_CODE_PATTERN.fullmatch(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _count_claims(claims_text: str) -> int | None:
    if not claims_text:
        return None

    claim_numbers: list[str] = []
    for line in claims_text.splitlines():
        match = _CLAIM_LINE_PATTERN.match(line)
        if not match:
            continue
        value = match.group("bracket") or match.group("number")
        if value and value not in claim_numbers:
            claim_numbers.append(value)

    return len(claim_numbers) if claim_numbers else None


def _build_drawings_info(
    *,
    drawing_snapshot: SeleniumTabSnapshot | None,
    description_lines: list[str],
) -> PatentDrawingsInfo:
    if drawing_snapshot is None:
        drawing_lines: list[str] = []
        drawing_count = 0
    else:
        drawing_lines = [
            " ".join(raw_line.split())
            for raw_line in drawing_snapshot.text.replace("\xa0", " ").splitlines()
            if raw_line.strip()
        ]
        drawing_count = drawing_snapshot.item_count

    labels = extract_drawing_labels(description_lines + drawing_lines)
    for line in drawing_lines:
        if _DRAWING_FIGURE_PATTERN.match(line) and line not in labels:
            labels.append(line)

    has_drawings = drawing_count > 0 or bool(labels)
    return PatentDrawingsInfo(
        has_drawings=has_drawings,
        drawing_page_count=drawing_count or None,
        drawing_labels=labels,
    )


def _serialize_links(snapshot: SeleniumTabSnapshot | None) -> list[dict[str, str]]:
    if snapshot is None:
        return []
    return [
        {"text": link.text, "href": link.href}
        for link in snapshot.links
        if link.href
    ]


def _select_original_file(
    *,
    doc_id: str,
    current_url: str,
    document_snapshot: SeleniumTabSnapshot | None,
) -> PatentOriginalFile:
    del current_url
    if document_snapshot is None:
        return PatentOriginalFile()

    ranked_links: list[tuple[int, SeleniumLink, str]] = []
    for link in document_snapshot.links:
        content_type = _infer_content_type(link)
        if not content_type:
            continue
        ranked_links.append((_content_priority(content_type), link, content_type))

    if not ranked_links:
        return PatentOriginalFile()

    ranked_links.sort(key=lambda item: item[0])
    _, selected_link, content_type = ranked_links[0]
    return PatentOriginalFile(
        available=True,
        content_type=content_type,
        filename=_infer_filename(doc_id=doc_id, link=selected_link, content_type=content_type),
        download_url=selected_link.href,
        storage_path="",
    )


def _infer_content_type(link: SeleniumLink) -> str:
    combined = f"{link.text} {link.href}".lower()
    if "pdf" in combined:
        return "application/pdf"
    if "zip" in combined:
        return "application/zip"
    if "xml" in combined:
        return "application/xml"
    return ""


def _content_priority(content_type: str) -> int:
    if content_type == "application/pdf":
        return 0
    if content_type == "application/zip":
        return 1
    if content_type == "application/xml":
        return 2
    return 99


def _infer_filename(*, doc_id: str, link: SeleniumLink, content_type: str) -> str:
    path = urlparse(link.href).path
    filename = path.rsplit("/", 1)[-1]
    if "." in filename:
        return filename
    suffix = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/xml": ".xml",
    }.get(content_type, "")
    return f"{doc_id}{suffix}"
