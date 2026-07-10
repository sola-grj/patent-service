import asyncio
from pathlib import Path

import pytest

from app.clients.wipo_patentscope_public import WipoPatentScopePublicClient
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import PatentReference, PatentSource

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, *, status_code: int, text: str, url: str) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeSession:
    def __init__(
        self,
        *,
        get_responses: list[FakeResponse],
        post_responses: list[FakeResponse] | None = None,
    ) -> None:
        self._get_responses = list(get_responses)
        self._post_responses = list(post_responses or [])

    def get(self, url: str, **_: object) -> FakeResponse:
        if not self._get_responses:
            raise AssertionError("unexpected GET request")
        response = self._get_responses.pop(0)
        assert response.url == url
        return response

    def post(self, url: str, **_: object) -> FakeResponse:
        if not self._post_responses:
            raise AssertionError("unexpected POST request")
        response = self._post_responses.pop(0)
        assert response.url == url
        return response

    def close(self) -> None:
        return None


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


def test_parse_patent_details_prefers_english_and_collects_raw_blocks():
    parsed = WipoPatentScopePublicClient.parse_patent_details(
        _fixture_text("wipo_public_second_get.html")
    )

    assert parsed["title"] == "COVER FOR A CONTAINER"
    assert parsed["abstract"] == "The present invention relates to a cover for a container."
    assert parsed["publication_date"] == "20260305"
    assert parsed["application_number"] == "PCT/AT2025/060321"
    assert parsed["application_filing_date"] == "20250814"
    assert parsed["applicants"] == [
        "OSKARI GMBH",
        "SECOND APPLICANT GMBH",
    ]
    assert parsed["inventors"] == ["MAROLT, Oswald", "DOE, Jane"]
    assert parsed["ipc"] == ["A47J 36/06 2006.1"]
    assert parsed["cpc"] == ["A47J 36/06", "A47J 36/12"]
    assert parsed["agents"] == ["BABELUK PATENTANWÄLTE GMBH Florianigasse 26/3 1080 Wien"]
    assert parsed["applicants_raw_blocks"][0].startswith("OSKARI GMBH [AT]/[AT]")


def test_parse_patent_details_handles_realistic_biblio_dom():
    parsed = WipoPatentScopePublicClient.parse_patent_details(
        _fixture_text("wipo_public_real_biblio.html")
    )

    assert parsed["publication_number"] == "WO/2026/044310"
    assert parsed["publication_date"] == "20260305"
    assert parsed["application_number"] == "PCT/AT2025/060321"
    assert parsed["application_filing_date"] == "20250814"
    assert parsed["title"] == "COVER FOR A CONTAINER"
    assert parsed["abstract"].startswith("The present invention relates to a cover")
    assert parsed["applicants"] == ["OSKARI GMBH"]
    assert parsed["inventors"] == ["MAROLT, Oswald"]
    assert parsed["agents"] == ["BABELUK PATENTANWÄLTE GMBH"]
    assert parsed["ipc"] == ["A47J 36/06 2006.1"]
    assert parsed["cpc"] == ["A47J 36/06", "A47J 36/12"]
    assert parsed["applicants_raw_blocks"] == ["OSKARI GMBH [AT]/[AT]"]


def test_lookup_patent_uses_second_get_when_bibliographic_panel_is_available():
    detail_url = "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310"
    session = FakeSession(
        get_responses=[
            FakeResponse(
                status_code=200,
                text=_fixture_text("wipo_public_first_shell.html"),
                url=detail_url,
            ),
            FakeResponse(
                status_code=200,
                text=_fixture_text("wipo_public_second_get.html"),
                url=detail_url,
            ),
        ]
    )
    client = WipoPatentScopePublicClient(
        _settings(),
        session_factory=lambda: session,
    )

    response = asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert response.basic_info.title == "COVER FOR A CONTAINER"
    assert response.basic_info.abstract == "The present invention relates to a cover for a container."
    assert response.original_file.available is False
    assert response.raw_source_refs["lookup_mode"] == "public_page"
    assert response.raw_source_refs["fetch_strategy"] == "second_get"
    assert response.raw_source_refs["publication_number"] == "WO/2026/044310"


def test_lookup_patent_falls_back_to_jsf_postback():
    detail_url = "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310"
    shell = _fixture_text("wipo_public_first_shell.html")
    postback = _fixture_text("wipo_public_postback.xml")
    session = FakeSession(
        get_responses=[
            FakeResponse(status_code=200, text=shell, url=detail_url),
            FakeResponse(status_code=200, text=shell, url=detail_url),
        ],
        post_responses=[
            FakeResponse(status_code=200, text=postback, url=detail_url),
        ],
    )
    client = WipoPatentScopePublicClient(
        _settings(),
        session_factory=lambda: session,
    )

    response = asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert response.basic_info.title == "COVER FOR A CONTAINER"
    assert response.raw_source_refs["fetch_strategy"] == "jsf_postback"


def test_lookup_patent_maps_rate_limit_status():
    detail_url = "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310"
    session = FakeSession(
        get_responses=[
            FakeResponse(status_code=403, text="access denied", url=detail_url),
        ]
    )
    client = WipoPatentScopePublicClient(
        _settings(),
        session_factory=lambda: session,
    )

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert excinfo.value.code == ErrorCode.SOURCE_RATE_LIMIT


def test_lookup_patent_maps_captcha_page_to_rate_limit():
    detail_url = "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310"
    captcha_html = """
    <html>
      <body>
        <form id="psCaptchaForm" name="psCaptchaForm">
          <span id="psCaptchaPanel">Please select the picture with snow</span>
        </form>
      </body>
    </html>
    """
    session = FakeSession(
        get_responses=[
            FakeResponse(status_code=200, text=captcha_html, url=detail_url),
        ]
    )
    client = WipoPatentScopePublicClient(
        _settings(),
        session_factory=lambda: session,
    )

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(client.lookup_patent(_reference(), include_original_file=False))

    assert excinfo.value.code == ErrorCode.SOURCE_RATE_LIMIT
    assert excinfo.value.details["block_type"] == "captcha"
