import logging
import time

from fastapi import APIRouter, Depends

from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.clients.wipo_patentscope_selenium import WipoPatentScopeSeleniumClient
from app.config import Settings, get_settings
from app.models.patents import PatentLookupApiResponse, PatentLookupRequest
from app.services.patent_lookup import PatentLookupService

router = APIRouter()
logger = logging.getLogger("patent_service")


def get_lookup_service(
    settings: Settings = Depends(get_settings),
) -> PatentLookupService:
    return PatentLookupService(
        settings=settings,
        epo_ops_client=EpoOpsClient(settings),
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url
        ),
        wipo_client=WipoPatentScopeClient(settings),
        wipo_public_client=WipoPatentScopeSeleniumClient(settings),
    )


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "sources": {
            "epo_ops_configured": settings.epo_ops_configured,
            "wipo_patentscope_configured": settings.wipo_patentscope_configured,
        },
    }


@router.post("/patents/lookup", response_model=PatentLookupApiResponse)
async def lookup_patent(
    request: PatentLookupRequest,
    service: PatentLookupService = Depends(get_lookup_service),
) -> PatentLookupApiResponse:
    started_at = time.monotonic()
    logger.info(
        "lookup request started patent_number=%s include_original_file=%s",
        request.patent_number,
        request.include_original_file,
    )
    response = await service.lookup_patent(request)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "lookup request finished patent_number=%s source=%s normalized_number=%s include_original_file=%s elapsed_ms=%s",
        request.patent_number,
        response.source,
        response.normalized_number,
        request.include_original_file,
        elapsed_ms,
    )
    return response
