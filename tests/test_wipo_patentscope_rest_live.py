import asyncio
import os

import pytest

from app.clients.wipo_patentscope_rest import WipoPatentScopeRestClient
from app.config import Settings
from app.utils.patent_numbers import normalize_patent_number


pytestmark = pytest.mark.skipif(
    os.getenv("PATENT_SERVICE_RUN_WIPO_LIVE_TESTS") != "1",
    reason="authenticated WIPO live tests are opt-in",
)


def test_live_wipo_rest_metadata_flow():
    settings = Settings()
    assert settings.wipo_rest_configured
    patent_number = os.getenv(
        "PATENT_SERVICE_WIPO_LIVE_PATENT_NUMBER", "WO2025078629A1"
    )
    response = asyncio.run(
        WipoPatentScopeRestClient(settings).lookup_patent(
            normalize_patent_number(patent_number), include_original_file=False
        )
    )

    assert response.basic_info.title
    assert response.basic_info.abstract
    assert response.basic_info.application_number
    assert response.basic_info.ipc
    assert response.raw_source_refs["selected_document_id"]
    assert response.raw_source_refs["selected_xml_page"]
