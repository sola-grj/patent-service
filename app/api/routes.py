import logging
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.clients.epo_ops import EpoOpsClient
from app.clients.epo_publication_server import EpoPublicationServerClient
from app.clients.wipo_patentscope import WipoPatentScopeClient
from app.clients.wipo_patentscope_rest import WipoPatentScopeRestClient
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
        wipo_rest_client=WipoPatentScopeRestClient(settings),
        wipo_soap_client=WipoPatentScopeClient(settings),
    )


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "sources": {
            "epo_ops_configured": settings.epo_ops_configured,
            "wipo_rest_configured": settings.wipo_rest_configured,
            "wipo_soap_configured": settings.wipo_soap_configured,
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


@router.get("/patents/files/{filename}", response_class=FileResponse)
async def download_patent_file(
    filename: str, settings: Settings = Depends(get_settings)
) -> FileResponse:
    if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=404, detail="Patent file not found.")
    storage_dir = Path(
        settings.wipo_storage_dir
        or Path(tempfile.gettempdir()) / "patent-service" / "wipo"
    ).resolve()
    file_path = (storage_dir / filename).resolve()
    if file_path.parent != storage_dir or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Patent file not found.")
    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=filename,
    )
