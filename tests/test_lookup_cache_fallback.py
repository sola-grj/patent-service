import asyncio

import httpx
import pytest

from app.cache.supabase import SupabasePatentCache
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentBasicInfo,
    PatentLookupCacheInfo,
    PatentLookupRequest,
    PatentLookupResponse,
    PatentOriginalFile,
    PatentSource,
)
from app.services.patent_lookup import PatentLookupService
from app.utils.patent_numbers import normalize_patent_number


class OfficialWipoClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def lookup_bibliographic(self, reference):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class CacheStub:
    configured = True

    def __init__(self, fallback=None):
        self.fallback = fallback
        self.reads = 0
        self.events = []

    async def find_lookup_fallback(self, reference):
        self.reads += 1
        return self.fallback

    async def record_lookup_event(self, **event):
        self.events.append(event)


def response(origin="official"):
    return PatentLookupResponse(
        source=PatentSource.WIPO,
        normalized_number="WO2025078629A1",
        display_number="WO/2025/078629",
        data_origin=origin,
        cache=PatentLookupCacheInfo(
            is_cached=origin == "cache_fallback",
            reason=(
                "official_source_no_result"
                if origin == "cache_fallback"
                else None
            ),
            last_successful_fetch_at=(
                "2026-07-28T00:00:00Z"
                if origin == "cache_fallback"
                else None
            ),
        ),
        basic_info=PatentBasicInfo(title="Patent"),
        original_file=PatentOriginalFile(),
    )


def service(client, cache):
    return PatentLookupService(
        settings=Settings(),
        epo_ops_client=object(),
        epo_publication_server_client=object(),
        wipo_rest_client=client,
        wipo_soap_client=object(),
        cache=cache,
    )


def source_error(code, status):
    return PatentServiceError(
        code=code,
        status_code=status,
        message="upstream",
        source="wipo",
    )


def test_official_hit_does_not_read_cache():
    cache = CacheStub(fallback=("patent-id", response("cache_fallback")))
    result = asyncio.run(
        service(OfficialWipoClient(response()), cache).lookup_patent(
            PatentLookupRequest(patent_number="WO2025078629A1")
        )
    )

    assert result.data_origin == "official"
    assert cache.reads == 0


def test_definitive_no_result_uses_cache_fallback():
    cache = CacheStub(fallback=("patent-id", response("cache_fallback")))
    result = asyncio.run(
        service(
            OfficialWipoClient(source_error(ErrorCode.SOURCE_NO_RESULT, 404)),
            cache,
        ).lookup_patent(PatentLookupRequest(patent_number="WO2025078629A1"))
    )

    assert result.data_origin == "cache_fallback"
    assert result.cache.reason == "official_source_no_result"
    assert cache.reads == 1
    assert cache.events[-1]["cache_status"] == "stale_fallback"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.SOURCE_RATE_LIMIT, 429),
        (ErrorCode.SOURCE_AUTH_REQUIRED, 401),
        (ErrorCode.SOURCE_ACCESS_DENIED, 403),
        (ErrorCode.SOURCE_UNAVAILABLE, 503),
    ],
)
def test_upstream_errors_never_use_cache(code, status):
    cache = CacheStub(fallback=("patent-id", response("cache_fallback")))
    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(
            service(OfficialWipoClient(source_error(code, status)), cache).lookup_patent(
                PatentLookupRequest(patent_number="WO2025078629A1")
            )
        )

    assert excinfo.value.code == code
    assert cache.reads == 0


def test_definitive_no_result_without_valid_cache_stays_not_found():
    cache = CacheStub()
    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(
            service(
                OfficialWipoClient(source_error(ErrorCode.SOURCE_NO_RESULT, 404)),
                cache,
            ).lookup_patent(PatentLookupRequest(patent_number="WO2025078629A1"))
        )

    assert excinfo.value.code == ErrorCode.SOURCE_NO_RESULT
    assert cache.reads == 1


def test_incompatible_cache_snapshot_is_safely_ignored():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/rest/v1/patents"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "patent-id",
                        "source": "wipo",
                        "metadata_snapshot": {"obsolete_contract": True},
                        "last_successful_fetch_at": "2026-07-28T00:00:00Z",
                    }
                ],
            )
        if request.url.path.endswith("/rest/v1/request_patents"):
            return httpx.Response(200, json=[{"id": "relationship-id"}])
        raise AssertionError(request.url)

    cache = SupabasePatentCache(
        Settings(
            supabase_url="https://example.supabase.co",
            supabase_secret_key="secret",
        ),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        cache.find_lookup_fallback(normalize_patent_number("WO2025078629A1"))
    )

    assert result is None
    assert all(request.method == "GET" for request in requests)
