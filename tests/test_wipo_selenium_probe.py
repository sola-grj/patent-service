import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "wipo_selenium_probe.py"
_SPEC = spec_from_file_location("wipo_selenium_probe", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def test_build_detail_url_supports_optional_cid():
    assert (
        _MODULE.build_detail_url(doc_id="WO2026044310", cid=None)
        == "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310"
    )
    assert (
        _MODULE.build_detail_url(doc_id="WO2026044310", cid="P11-MREAVK-01901-1")
        == "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2026044310&_cid=P11-MREAVK-01901-1"
    )


def test_classify_page_source_detects_states():
    assert (
        _MODULE.classify_page_source(
            '<form id="psCaptchaForm">Please select the picture with snow</form>'
        )
        == "captcha"
    )
    assert (
        _MODULE.classify_page_source(
            "Publication Number International Application No. Title Abstract"
        )
        == "bibliographic"
    )
    assert _MODULE.classify_page_source("Please wait... sso='auth-basic'") == "shell"
    assert _MODULE.classify_page_source("<html><body>other</body></html>") == "unknown"


def test_ensure_credentials_only_applies_when_login_enabled():
    class Args:
        login = False
        username = ""
        password = ""

    _MODULE.ensure_credentials(Args())

    Args.login = True
    try:
        _MODULE.ensure_credentials(Args())
    except SystemExit as exc:
        assert "Username/password required when --login is enabled." in str(exc)
    else:
        raise AssertionError("expected credentials check to fail")


def test_parse_patent_details_is_exposed_for_bibliographic_pages():
    html_text = """
    <html>
      <body>
        <div>Publication Number</div>
        <div>WO/2026/044310</div>
        <div>Publication Date</div>
        <div>05.03.2026</div>
        <div>International Application No.</div>
        <div>PCT/AT2025/060321</div>
        <div>International Filing Date</div>
        <div>14.08.2025</div>
        <div>Title</div>
        <div>[EN] COVER FOR A CONTAINER</div>
        <div>IPC</div>
        <div>A47J 36/06 2006.1</div>
        <div>CPC</div>
        <div>A47J 36/06</div>
        <div>Applicants</div>
        <div>OSKARI GMBH [AT]/[AT]</div>
        <div>Inventors</div>
        <div>MAROLT, Oswald</div>
        <div>Agents</div>
        <div>BABELUK PATENTANWÄLTE GMBH</div>
        <div>Abstract</div>
        <div>[EN] The present invention relates to a cover for a container.</div>
      </body>
    </html>
    """

    parsed = _MODULE.WipoPatentScopePublicClient.parse_patent_details(html_text)

    assert parsed["publication_number"] == "WO/2026/044310"
    assert parsed["publication_date"] == "20260305"
    assert parsed["application_number"] == "PCT/AT2025/060321"
    assert parsed["title"] == "COVER FOR A CONTAINER"
    assert parsed["abstract"] == "The present invention relates to a cover for a container."
