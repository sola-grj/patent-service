import asyncio
import json

import httpx

from app.cache.supabase import SupabasePatentCache
from app.config import Settings
from app.models.patents import (
    PatentAnalysisAggregate,
    PatentAnalysisResponse,
    PatentBasicInfo,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentSource,
)
from app.utils.patent_numbers import normalize_patent_number


def test_upsert_patent_deduplicates_aliases_by_normalized_value():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/rest/v1/patents"):
            return httpx.Response(201, json=[{"id": "patent-id"}])
        if request.url.path.endswith("/rest/v1/patent_lookup_aliases"):
            return httpx.Response(201, json=[])
        raise AssertionError(request.url)

    cache = SupabasePatentCache(
        Settings(
            supabase_url="https://example.supabase.co",
            supabase_secret_key="secret",
        ),
        transport=httpx.MockTransport(handler),
    )
    lookup = PatentLookupResponse(
        source=PatentSource.WIPO,
        normalized_number="PCTAT2025060357",
        display_number="PCT/AT2025/060357",
        basic_info=PatentBasicInfo(title="Patent"),
        original_file=PatentOriginalFile(),
    )
    analysis = PatentAnalysisResponse(
        input_mode="patent_number",
        status="success",
        patent_number="PCT/AT2025/060357",
        aggregate=PatentAnalysisAggregate(total_words=100),
    )

    asyncio.run(
        cache.upsert_patent(
            reference=normalize_patent_number("PCT/AT2025/060357"),
            lookup=lookup,
            analysis=analysis,
            processing_status="pending",
        )
    )

    alias_request = next(
        request
        for request in requests
        if request.url.path.endswith("/rest/v1/patent_lookup_aliases")
    )
    aliases = json.loads(alias_request.content)
    normalized_aliases = [alias["normalized_alias"] for alias in aliases]

    assert normalized_aliases == ["PCTAT2025060357"]
    assert aliases[0]["alias_number"] == "PCT/AT2025/060357"
