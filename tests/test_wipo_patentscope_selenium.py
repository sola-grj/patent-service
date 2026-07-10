import asyncio
from pathlib import Path

import pytest

from app.clients.wipo_patentscope_selenium import (
    SeleniumFetchResult,
    SeleniumLink,
    SeleniumTabSnapshot,
    WipoPatentScopeSeleniumClient,
    _count_claims,
    build_detail_url,
    classify_page_source,
)
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentReference, PatentSource

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _settings() -> Settings:
    return Settings(wipo_lookup_mode="public_page")


def _reference() -> PatentReference:
    return PatentReference(
        source=PatentSource.WIPO,
        normalized_number="WO2026044310A1",
        display_number="WO/2026/044310",
        country_code="WO",
        doc_number="2026044310",
        kind_code="A1",
        lookup_number="WO2026044310A1",
    )


def test_build_detail_url_supports_optional_cid():
    assert (
        build_detail_url(
            base_url="https://patentscope.wipo.int/search/en",
            doc_id="WO2026044310",
            cid=None,
        )
        == "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310"
    )
    assert (
        build_detail_url(
            base_url="https://patentscope.wipo.int/search/en",
            doc_id="WO2026044310",
            cid="P11-MREAVK-01901-1",
        )
        == "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310&_cid=P11-MREAVK-01901-1"
    )


def test_classify_page_source_detects_states():
    assert (
        classify_page_source(
            '<form id="psCaptchaForm">Please select the picture with snow</form>'
        )
        == "captcha"
    )
    assert (
        classify_page_source(
            "Publication Number International Application No. Title Abstract"
        )
        == "bibliographic"
    )
    assert classify_page_source("Please wait...") == "shell"
    assert classify_page_source("<html><body><h1>403 FORBIDDEN</h1></body></html>") == "forbidden"
    assert classify_page_source("<html><body>other</body></html>") == "unknown"


def test_lookup_patent_uses_selenium_fetcher_result():
    detail_html = _fixture_text("wipo_public_real_biblio.html")
    client = WipoPatentScopeSeleniumClient(
        _settings(),
        page_fetcher=lambda _: SeleniumFetchResult(
            state="bibliographic",
            current_url="https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310",
            title="WO2026044310 COVER FOR A CONTAINER",
            page_source=detail_html,
            tabs={
                "description": SeleniumTabSnapshot(
                    text=(
                        "Note: Text based on automatic Optical Character Recognition processes.\n"
                        "Description\n"
                        "The present invention relates to a cover for a container.\n"
                        "FIG. 1 is a sectional view."
                    )
                ),
                "claims": SeleniumTabSnapshot(
                    text=(
                        "ATTENSPRUECHE\n"
                        "1. A cover for a container comprising a flexible rim.\n"
                        "2. The cover of claim 1 wherein the rim tapers inward."
                    )
                ),
                "drawings": SeleniumTabSnapshot(
                    text="FIG. 1\nFIG. 2",
                    item_count=2,
                ),
                "documents": SeleniumTabSnapshot(
                    links=[
                        SeleniumLink(
                            text="PDF 14p",
                            href=(
                                "https://patentscope.wipo.int/search/en/download/"
                                "WO2026044310-PAMPH.pdf"
                            ),
                        ),
                        SeleniumLink(
                            text="XML",
                            href="https://patentscope.wipo.int/search/en/download/WO2026044310.xml",
                        ),
                    ]
                ),
            },
        ),
    )

    response = asyncio.run(client.lookup_patent(_reference(), include_original_file=True))

    assert response.basic_info.title == "COVER FOR A CONTAINER"
    assert response.basic_info.applicants == ["OSKARI GMBH"]
    assert response.description_words == 16
    assert response.claims_count == 2
    assert response.claims_words == 21
    assert response.drawings.has_drawings is True
    assert response.drawings.drawing_page_count == 2
    assert response.drawings.drawing_labels == ["FIG. 1 is a sectional view.", "FIG. 1", "FIG. 2"]
    assert response.original_file.available is True
    assert response.original_file.content_type == "application/pdf"
    assert response.original_file.filename == "WO2026044310-PAMPH.pdf"
    assert response.raw_source_refs["lookup_mode"] == "public_page"
    assert response.raw_source_refs["fetch_strategy"] == "selenium"
    assert response.raw_source_refs["documents_tab"]["links"][0]["text"] == "PDF 14p"


def test_lookup_patent_maps_captcha_to_rate_limit():
    client = WipoPatentScopeSeleniumClient(
        _settings(),
        page_fetcher=lambda _: SeleniumFetchResult(
            state="captcha",
            current_url="https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310",
            title="blocked",
            page_source="<form id='psCaptchaForm'></form>",
        ),
    )

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert excinfo.value.code == ErrorCode.SOURCE_RATE_LIMIT
    assert excinfo.value.details["fetch_strategy"] == "selenium"


def test_lookup_patent_keeps_original_file_empty_when_flag_is_false():
    detail_html = _fixture_text("wipo_public_real_biblio.html")
    client = WipoPatentScopeSeleniumClient(
        _settings(),
        page_fetcher=lambda _: SeleniumFetchResult(
            state="bibliographic",
            current_url="https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310",
            title="WO2026044310 COVER FOR A CONTAINER",
            page_source=detail_html,
            tabs={
                "documents": SeleniumTabSnapshot(
                    links=[
                        SeleniumLink(
                            text="PDF 14p",
                            href=(
                                "https://patentscope.wipo.int/search/en/download/"
                                "WO2026044310-PAMPH.pdf"
                            ),
                        )
                    ]
                )
            },
        ),
    )

    response = asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert response.original_file.available is False
    assert response.raw_source_refs["documents_tab"]["links"][0]["href"].endswith(
        "WO2026044310-PAMPH.pdf"
    )


def test_count_claims_handles_ocr_spacing_before_period():
    claims_text = "\n".join(
        [
            "1 . First claim text.",
            "2. Second claim text.",
            "11 . Eleventh claim text.",
            "21 . Twenty-first claim text.",
            "31 . Thirty-first claim text.",
            "41 . Forty-first claim text.",
            "51 . Fifty-first claim text.",
        ]
    )

    assert _count_claims(claims_text) == 7
