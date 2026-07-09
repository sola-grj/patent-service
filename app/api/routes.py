from fastapi import APIRouter, Depends

from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.config import Settings, get_settings
from app.models.patents import PatentLookupApiResponse, PatentLookupRequest
from app.services.patent_lookup import PatentLookupService

router = APIRouter()


def get_lookup_service(
    settings: Settings = Depends(get_settings),
) -> PatentLookupService:
    return PatentLookupService(
        epo_ops_client=EpoOpsClient(settings),
        epo_publication_server_client=EpoPublicationServerClient(
            settings.epo_publication_server_url
        ),
        wipo_client=WipoPatentScopeClient(settings),
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
    return await service.lookup_patent(request)
