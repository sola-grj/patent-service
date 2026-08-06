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


def test_generated_document_upload_uses_global_hash_upsert_key():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/storage/v1/object/patent-originals/" in request.url.path:
            return httpx.Response(200, json={})
        if request.url.path.endswith("/rest/v1/patent_documents"):
            payload = json.loads(request.content)
            return httpx.Response(201, json=[{"id": "document-id", **payload}])
        raise AssertionError(request.url)

    cache = SupabasePatentCache(
        Settings(
            supabase_url="https://example.supabase.co",
            supabase_secret_key="secret",
        ),
        transport=httpx.MockTransport(handler),
    )

    saved = asyncio.run(
        cache.save_document(
            patent_id="patent-id",
            source="wipo",
            normalized_number="WO2026044310A1",
            filename="WO2026044310A1.pdf",
            mime_type="application/pdf",
            content=b"%PDF-generated",
            sha256="a" * 64,
            source_url=None,
            kind_code="A1",
        )
    )

    insert = next(
        request
        for request in requests
        if request.url.path.endswith("/rest/v1/patent_documents")
    )
    payload = json.loads(insert.content)
    assert insert.url.params["on_conflict"] == (
        "patent_id,document_type,delivery_strategy,sha256"
    )
    assert insert.headers["prefer"] == (
        "resolution=merge-duplicates,return=representation"
    )
    assert payload["delivery_strategy"] == "generated_cache"
    assert saved["id"] == "document-id"


def test_external_document_reuses_existing_url_without_insert():
    requests: list[httpx.Request] = []
    source_url = "https://data.example/patents/EP1234567NWA1/document.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith(
            "/rest/v1/patent_documents"
        ):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "existing-document-id",
                        "delivery_strategy": "external_url",
                        "upstream_source_url": source_url,
                    }
                ],
            )
        raise AssertionError(request.url)

    cache = SupabasePatentCache(
        Settings(
            supabase_url="https://example.supabase.co",
            supabase_secret_key="secret",
        ),
        transport=httpx.MockTransport(handler),
    )

    saved = asyncio.run(
        cache.save_external_document(
            patent_id="patent-id",
            filename="EP1234567A1.pdf",
            mime_type="application/pdf",
            source_url=source_url,
            kind_code="A1",
        )
    )

    assert saved["id"] == "existing-document-id"
    assert len(requests) == 1
    assert requests[0].url.params["delivery_strategy"] == "eq.external_url"
    assert requests[0].url.params["upstream_source_url"] == f"eq.{source_url}"
