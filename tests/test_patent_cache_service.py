import asyncio

import pytest
from pathlib import Path

from app.analysis.artifacts import AnalysisArtifactStore
from app.config import Settings
from app.errors import ErrorCode, PatentServiceError
from app.models.patents import (
    PatentAnalysisAggregate,
    PatentAnalysisResponse,
    PatentLookupEpResponse,
    PatentSource,
)
from app.services.patent_cache import PatentCacheService


class DraftOnlyCache:
    def __init__(self):
        self.writes = 0

    async def get_formal_request(self, request_id):
        return None

    async def upsert_patent(self, **kwargs):
        self.writes += 1
        raise AssertionError("Draft/non-submitted Requests must not write cache")


class PromotionCache:
    def __init__(self):
        self.saved_content = None
        self.statuses = []

    async def claim_processing(self, patent_id):
        return True

    async def save_document(self, **kwargs):
        self.saved_content = kwargs["content"]
        return {"id": "document-id"}

    async def set_request_file_status(self, request_id, status, **kwargs):
        self.statuses.append(status)

    async def finish_processing(self, patent_id, **kwargs):
        return None

    async def record_lookup_event(self, **kwargs):
        return None


class LookupMustNotRun:
    async def lookup_patent_full(self, request, **kwargs):
        raise AssertionError("A valid prepared artifact must avoid another source download")


class DownloadCache:
    async def get_formal_request(self, request_id):
        return {"id": request_id, "submitted_at": "2026-07-28T00:00:00Z"}

    async def get_request_document(self, request_id):
        return {
            "status": "parsed",
            "storage_bucket": "patent-originals",
            "storage_path": "epo/EP1234567A1/document.pdf",
            "original_filename": "EP1234567A1.pdf",
            "mime_type": "application/pdf",
            "patent_document_id": "document-id",
        }

    async def download_document(self, bucket, storage_path):
        assert bucket == "patent-originals"
        assert storage_path == "epo/EP1234567A1/document.pdf"
        return b"%PDF-stored"


class ConcurrentPrepareCache:
    def __init__(self):
        self.link_started = asyncio.Event()
        self.find_started = asyncio.Event()
        self.status_started = asyncio.Event()
        self.finish_started = asyncio.Event()

    async def get_formal_request(self, request_id):
        return {"id": request_id, "submitted_at": "2026-07-28T00:00:00Z"}

    async def upsert_patent(self, **kwargs):
        return {"id": "patent-id"}

    async def link_request_patent(self, request_id, patent_id):
        self.link_started.set()
        await self.find_started.wait()

    async def find_available_document(self, patent_id):
        self.find_started.set()
        await self.link_started.wait()
        return {"id": "document-id"}

    async def set_request_file_status(self, request_id, status, **kwargs):
        assert status == "parsed"
        self.status_started.set()
        await self.finish_started.wait()

    async def finish_processing(self, patent_id):
        self.finish_started.set()
        await self.status_started.wait()


def test_cache_prepare_rejects_non_submitted_request_before_writing():
    cache = DraftOnlyCache()
    service = PatentCacheService(cache=cache, lookup_service=object())
    lookup = PatentLookupEpResponse(
        source=PatentSource.EPO,
        normalized_number="EP1234567A1",
        display_number="EP1234567A1",
        title="Example",
    )
    analysis = PatentAnalysisResponse(
        input_mode="patent_number",
        status="success",
        patent_number="EP1234567A1",
        aggregate=PatentAnalysisAggregate(total_words=100),
    )

    with pytest.raises(PatentServiceError) as excinfo:
        asyncio.run(
            service.prepare(
                request_id="draft-request",
                lookup=lookup,
                analysis=analysis,
            )
        )

    assert excinfo.value.code == ErrorCode.CACHE_UNAVAILABLE
    assert cache.writes == 0


def test_cache_prepare_runs_independent_cache_operations_concurrently():
    cache = ConcurrentPrepareCache()
    service = PatentCacheService(cache=cache, lookup_service=object())
    lookup = PatentLookupEpResponse(
        source=PatentSource.EPO,
        normalized_number="EP1234567A1",
        display_number="EP1234567A1",
        title="Example",
    )
    analysis = PatentAnalysisResponse(
        input_mode="patent_number",
        status="success",
        patent_number="EP1234567A1",
        aggregate=PatentAnalysisAggregate(total_words=100),
    )

    result = asyncio.run(
        asyncio.wait_for(
            service.prepare(
                request_id="request-id",
                lookup=lookup,
                analysis=analysis,
            ),
            timeout=1,
        )
    )

    assert result.status == "completed"


def test_cache_process_promotes_analysis_artifact_without_redownloading(
    tmp_path: Path,
):
    store = AnalysisArtifactStore(
        Settings(analysis_artifact_dir=str(tmp_path / "artifacts"))
    )
    source = tmp_path / "prepared.pdf"
    source.write_bytes(b"%PDF-prepared")
    artifact = store.create_from_path(
        source,
        filename="EP1234567A1.pdf",
        mime_type="application/pdf",
    )
    analysis = PatentAnalysisResponse(
        input_mode="patent_number",
        status="success",
        patent_number="EP1234567A1",
        artifact=artifact,
        aggregate=PatentAnalysisAggregate(total_words=100),
    )
    lookup = PatentLookupEpResponse(
        source=PatentSource.EPO,
        normalized_number="EP1234567A1",
        display_number="EP1234567A1",
        title="Example",
    )
    cache = PromotionCache()
    service = PatentCacheService(
        cache=cache,
        lookup_service=LookupMustNotRun(),
        artifact_store=store,
    )

    asyncio.run(
        service.process(
            request_id="request-id",
            patent_id="patent-id",
            lookup=lookup,
            analysis=analysis,
        )
    )

    assert cache.saved_content == b"%PDF-prepared"
    assert cache.statuses == ["parsed"]
    assert not (store.root / artifact.artifact_id).exists()


def test_submitted_request_download_reads_private_cached_document():
    service = PatentCacheService(
        cache=DownloadCache(),
        lookup_service=object(),
    )

    content, filename, mime_type = asyncio.run(
        service.download_request_document("request-id")
    )

    assert content == b"%PDF-stored"
    assert filename == "EP1234567A1.pdf"
    assert mime_type == "application/pdf"
